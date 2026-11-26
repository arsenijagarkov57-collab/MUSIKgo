"""features.py — тексты песен (LRCLIB) и рекомендации «Моя волна».

Все внешние запросы выполняются асинхронно через httpx и не требуют ключей
API. Сеть считается «лучшим усилием»: если сервис недоступен или вернул
мусор, функция возвращает пустой результат, не роняя сервер.
"""
from __future__ import annotations

import random
import re

import httpx

import database
import downloader

LRCLIB_URL = 'https://lrclib.net/api/search'
DEEZER_URL = 'https://api.deezer.com'
ITUNES_URL = 'https://itunes.apple.com/search'
USER_AGENT = 'LocalMusicService/1.0'
TIMEOUT = httpx.Timeout(12.0, connect=5.0)


class WaveError(Exception):
    """Не удалось подобрать или скачать трек для волны."""


# --------------------------------------------------------------------------
# Очистка названий
# --------------------------------------------------------------------------

# Квадратные скобки почти всегда содержат тех. мусор: [320 kbps], [yt_xxx],
# [Official Video] и т.п. Круглые — «(Official Audio)», «(feat. ...)».
_BRACKETS = re.compile(r'[\[\(][^\]\)]*[\]\)]')
# «ft. Pharrell Williams, Nile Rodgers» и похожие вставки без скобок:
# убираем всё после ft./feat. — в поисковом запросе эти имена только мешают.
_FEAT = re.compile(r'\s*\b(?:ft\.?|feat\.?|featuring)\b\.?\s+.*$', re.IGNORECASE)
_WHITESPACE = re.compile(r'\s+')


def clean_text(value: str | None) -> str:
    """Убирает из строки типичный мусор — для поисковых запросов."""
    text = str(value or '')
    text = _BRACKETS.sub(' ', text)
    text = _FEAT.sub(' ', text)
    text = _WHITESPACE.sub(' ', text).strip()
    return text.strip(' \t-–—')


def clean_artist_title(track: dict) -> tuple[str, str]:
    """Возвращает (название, исполнитель), очищенные для поиска текстов.

    В локальной БД название нередко выглядит как
    «Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers»,
    а исполнитель — «Daft Punk». Если же в названии есть «Исполнитель - ...»,
    лишнюю часть аккуратно отделяем.
    """
    title = clean_text(track.get('title'))
    artist = clean_text(track.get('artist'))

    if ' - ' in title:
        head, tail = (part.strip() for part in title.split(' - ', 1))
        if artist and head.casefold() in artist.casefold():
            title = tail  # «Daft Punk - Get Lucky» → «Get Lucky»
        elif not artist:
            artist, title = head, tail
    if ' - ' in artist:
        artist = artist.split(' - ', 1)[0].strip()

    return title, artist


# --------------------------------------------------------------------------
# Тексты песен (LRCLIB)
# --------------------------------------------------------------------------

_EMPTY_LYRICS = {'synced_lyrics': '', 'plain_lyrics': ''}


async def fetch_lyrics(track: dict) -> dict:
    """Ищет текст трека в lrclib.net.

    Возвращает {"synced_lyrics": str, "plain_lyrics": str}. При любом
    сбое сети/формата ответа возвращает пустые строки (эндпоинт при этом
    отвечает статусом 200).
    """
    title, artist = clean_artist_title(track)
    if not title:
        return dict(_EMPTY_LYRICS)

    params = {'track_name': title}
    if artist:
        params['artist_name'] = artist

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                LRCLIB_URL, params=params, headers={'User-Agent': USER_AGENT}
            )
            response.raise_for_status()
            results = response.json()
    except Exception:
        return dict(_EMPTY_LYRICS)

    if not isinstance(results, list):
        return dict(_EMPTY_LYRICS)

    # «Первый подходящий результат» — первый, у которого есть текст
    # (синхронизированный или обычный).
    for item in results:
        if not isinstance(item, dict):
            continue
        synced = item.get('syncedLyrics') or ''
        plain = item.get('plainLyrics') or ''
        if isinstance(synced, str) and isinstance(plain, str) and (synced or plain):
            return {'synced_lyrics': synced, 'plain_lyrics': plain}
    return dict(_EMPTY_LYRICS)


# --------------------------------------------------------------------------
# Рекомендации (Deezer → iTunes)
# --------------------------------------------------------------------------


def _parse_deezer(payload: dict) -> list[tuple[str, str]]:
    """Deezer/search → список (исполнитель, название)."""
    result: list[tuple[str, str]] = []
    for item in (payload.get('data') or []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict) or not item.get('title'):
            continue
        artist_obj = item.get('artist') or {}
        artist = artist_obj.get('name') if isinstance(artist_obj, dict) else artist_obj
        result.append((str(artist or '').strip(), str(item['title']).strip()))
    return result


def _parse_itunes(payload: dict) -> list[tuple[str, str]]:
    """iTunes/search → список (исполнитель, название)."""
    result: list[tuple[str, str]] = []
    for item in (payload.get('results') or []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict) or not item.get('trackName'):
            continue
        result.append((
            str(item.get('artistName') or '').strip(),
            str(item['trackName']).strip(),
        ))
    return result


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    response = await client.get(
        url, params=params, headers={'User-Agent': USER_AGENT}
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def fetch_artist_id(client: httpx.AsyncClient, artist: str) -> int | None:
    """Достаёт id исполнителя из Deezer (для /artist/{id}/related)."""
    try:
        payload = await _get_json(client, f'{DEEZER_URL}/search', {
            'q': f'artist:"{artist}"', 'limit': 1,
        })
        items = payload.get('data') or []
        if items and isinstance(items[0], dict):
            artist_obj = items[0].get('artist') or {}
            if isinstance(artist_obj, dict):
                return int(artist_obj['id'])
    except Exception:
        return None
    return None


async def fetch_related(client: httpx.AsyncClient, artist_id: int) -> list[str]:
    """Имена похожих исполнителей через /artist/{id}/related."""
    try:
        payload = await _get_json(client, f'{DEEZER_URL}/artist/{artist_id}/related')
        return [
            str(item['name']).strip()
            for item in (payload.get('data') or [])
            if isinstance(item, dict) and item.get('name')
        ]
    except Exception:
        return []


async def fetch_recommendations(artist: str, use_related: bool = False) -> list[tuple[str, str]]:
    """Возвращает кандидатов для волны: сначала Deezer, при ошибке — iTunes.

    При use_related=True дополнительно опрашивается /artist/{id}/related —
    это позволяет расширить волну за счёт близких исполнителей.
    """
    candidates: list[tuple[str, str]] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            payload = await _get_json(client, f'{DEEZER_URL}/search', {
                'q': f'artist:"{artist}"', 'limit': 15,
            })
            candidates = _parse_deezer(payload)
        except Exception:
            candidates = []

        if not candidates and use_related:
            artist_id = await fetch_artist_id(client, artist)
            if artist_id:
                for related_name in (await fetch_related(client, artist_id))[:3]:
                    try:
                        related_payload = await _get_json(client, f'{DEEZER_URL}/search', {
                            'q': f'artist:"{related_name}"', 'limit': 5,
                        })
                        candidates.extend(_parse_deezer(related_payload))
                    except Exception:
                        continue

        if not candidates:
            try:
                payload = await _get_json(client, ITUNES_URL, {
                    'term': artist, 'entity': 'song', 'limit': 15,
                })
                candidates = _parse_itunes(payload)
            except Exception:
                candidates = []

    return candidates


# --------------------------------------------------------------------------
# «Моя волна»
# --------------------------------------------------------------------------


def _norm(artist: str, title: str) -> str:
    """Ключ для сравнения треков «исполнитель - название» без регистра."""
    return clean_text(f'{artist} - {title}').casefold()


async def pick_seed_track(
    seed_track_id: int | None, exclude_youtube_ids: list[str] | None = None
) -> dict | None:
    """Выбирает опорной трек: указанный → случайный лайкнутый → любой."""
    exclude = set(exclude_youtube_ids or [])

    if seed_track_id:
        return await database.get_track(seed_track_id)

    tracks = await database.get_all_tracks()
    liked = [track for track in tracks if track.get('is_liked')
             and track.get('youtube_id') not in exclude]
    if liked:
        return random.choice(liked)

    fresh = [track for track in tracks if track.get('youtube_id') not in exclude]
    return random.choice(fresh) if fresh else None


async def next_wave_track(
    seed_track_id: int | None = None,
    exclude_youtube_ids: list[str] | None = None,
) -> dict:
    """Подбирает, скачивает и возвращает следующий трек волны.

    Бросает WaveError, если библиотека пуста, рекомендаций нет или ни один
    трек не удалось скачать.
    """
    exclude = [str(value).strip() for value in (exclude_youtube_ids or []) if value]
    exclude_keys = {value.casefold() for value in exclude}

    seed = await pick_seed_track(seed_track_id, exclude)
    if seed is None:
        raise WaveError('Нет треков, на которых можно построить волну')

    _, seed_artist = clean_artist_title(seed)
    if not seed_artist:
        seed_artist = clean_text(seed.get('title') or '')
    if not seed_artist:
        raise WaveError('Не удалось определить исполнителя опорного трека')

    recommendations = await fetch_recommendations(seed_artist, use_related=True)
    if not recommendations:
        raise WaveError('Не удалось получить рекомендации')

    local_keys = {
        _norm(item.get('artist') or '', item.get('title') or '')
        for item in await database.get_all_tracks()
    }

    # Перемешиваем, чтобы волна не повторяла порядок чарта.
    shuffled = list(recommendations)
    random.shuffle(shuffled)
    errors: list[str] = []

    for rec_artist, rec_title in shuffled:
        if not rec_artist or not rec_title:
            continue
        key = _norm(rec_artist, rec_title)
        if not key or key in local_keys or key in exclude_keys:
            continue
        if rec_title.casefold() in exclude_keys:
            continue

        try:
            track = await downloader.search_and_download(f'{rec_artist} - {rec_title}')
        except Exception as exc:
            errors.append(str(exc))
            continue

        if track.get('youtube_id') in exclude:
            continue
        return track

    raise WaveError(
        'Не удалось подобрать новый трек для волны'
        + (f': {errors[0]}' if errors else '')
    )
