"""downloader.py — поиск и скачивание треков через yt-dlp (YouTube + SoundCloud).

Алгоритм `search_and_download(query)`:
   1. Поиск: `ytsearch1:<запрос> audio` → берём первый результат.
   2. Если YouTube недоступен (ошибка, таймаут, бот-детект) или ничего не
      нашёл — повторяем поиск через SoundCloud: `scsearch1:<запрос>`.
   3. Проверка БД по `youtube_id` — если трек уже скачан, возвращаем запись.
      В колонку `youtube_id` сохраняется ID с префиксом источника:
      `yt_<id>` для YouTube и `sc_<id>` для SoundCloud.
   4. Скачивание лучшего аудио и конвертация в MP3 (preferredquality=0).
   5. Сохранение обложки в /storage/covers и вшивание ID3-метаданных
      (название, исполнитель, обложка) в MP3 через mutagen.

Для быстрого выбора трека в интерфейсе есть `search_candidates(query)` —
он возвращает до 5 вариантов с каждого источника без скачивания, а
`download_by_url(url, source)` скачивает конкретно выбранный вариант.

Требования:
   - Python 3.10+ и зависимости из requirements.txt;
   - FFmpeg: либо системный в PATH, либо `ffmpeg.exe` рядом с main.py
     (в корне проекта). Локальный файл имеет приоритет.
"""
from __future__ import annotations

import asyncio
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TPE1, TIT2

import database

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / 'storage'
TRACKS_DIR = STORAGE_DIR / 'tracks'   # сюда сохраняются готовые .mp3
COVERS_DIR = STORAGE_DIR / 'covers'   # сюда сохраняются обложки

# Папки должны существовать до первого скачивания.
TRACKS_DIR.mkdir(parents=True, exist_ok=True)
COVERS_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_YOUTUBE = 'youtube'
SOURCE_SOUNDCLOUD = 'soundcloud'

# Префиксы для колонки youtube_id (защита от коллизий между источниками).
SOURCE_PREFIX = {SOURCE_YOUTUBE: 'yt', SOURCE_SOUNDCLOUD: 'sc'}
# Префиксы поисковых запросов yt-dlp.
SEARCH_PREFIX = {SOURCE_YOUTUBE: 'ytsearch', SOURCE_SOUNDCLOUD: 'scsearch'}
SOURCE_TITLE = {SOURCE_YOUTUBE: 'YouTube', SOURCE_SOUNDCLOUD: 'SoundCloud'}

# --- Настройки для облачных серверов ---------------------------------------
# yt-dlp по умолчанию отправляет свой собственный User-Agent — с IP дата-центров
# YouTube часто отвечает бот-детектом. Обычный браузерный UA и короткие таймауты
# снижают число ложных блокировок и гарантируют быстрый переход к SoundCloud.
HTTP_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)
HTTP_HEADERS = {
    'User-Agent': HTTP_USER_AGENT,
    'Accept-Language': 'en-US,en;q=0.9',
}
SOCKET_TIMEOUT = 15      # ожидание ответа от одного сокета, сек
SEARCH_TIMEOUT = 20      # верхняя граница поиска на одном источнике, сек
DOWNLOAD_TIMEOUT = 180   # верхняя граница скачивания одного трека, сек

# Локальный FFmpeg рядом с main.py, если он есть.
LOCAL_FFMPEG = BASE_DIR / 'ffmpeg.exe'

# Символы, запрещённые Windows в именах файлов, плюс управляющие символы.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Типовые «хвосты» названий на YouTube: "(Official Video)", "[Lyrics]" и т.п.
_YOUTUBE_SUFFIX = re.compile(
    r'\s*[\(\[]\s*(?:official\s+)?(?:music\s+)?(?:video|audio|lyrics?)\s*[\)\]]\s*$',
    re.IGNORECASE,
)
_YOUTUBE_ID = re.compile(r'(?:v=|youtu\.be/|/vi/|/embed/)([A-Za-z0-9_-]{11})')

# Защита от одновременного скачивания одного и того же трека.
_download_lock = asyncio.Lock()


class DownloadError(Exception):
    """Ошибка поиска или скачивания трека (превращается в HTTP 502)."""


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    """Делает из строки безопасное имя файла для Windows."""
    name = _INVALID_FILENAME_CHARS.sub('_', name)
    name = name.strip().rstrip('.')  # точка в конце имени недопустима в Windows
    return name


def _base_ydl_opts() -> dict:
    """Общие настройки yt-dlp.

    Если рядом с main.py лежит ffmpeg.exe, yt-dlp будет использовать его,
    не обращаясь к системному PATH.
    """
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        # Таймауты/повторы: в облаке медленный или заблокированный YouTube не
        # должен задерживать фоллбэк на SoundCloud.
        'socket_timeout': SOCKET_TIMEOUT,
        'retries': 2,
        'fragment_retries': 2,
        'extractor_retries': 1,
        'http_headers': HTTP_HEADERS,
    }
    if LOCAL_FFMPEG.is_file():
        opts['ffmpeg_location'] = str(BASE_DIR)
    return opts


def _track_key(source: str, source_id: str) -> str:
    """Ключ трека в БД: `yt_<id>` или `sc_<id>`."""
    return f'{SOURCE_PREFIX[source]}_{source_id}'


def _normalize_thumbnail(url: str | None, source: str) -> str | None:
    """Для SoundCloud заменяет `-large` в URL обложки на `-t500x500`."""
    if not url:
        return url
    if source == SOURCE_SOUNDCLOUD and '-large' in url:
        return url.replace('-large', '-t500x500')
    return url


def _pick_thumbnail(info: dict, source: str) -> str | None:
    """Выбирает лучший URL обложки из info-словаря yt-dlp."""
    thumbnails = [
        item for item in (info.get('thumbnails') or [])
        if isinstance(item, dict) and item.get('url')
    ]
    if thumbnails:
        best = max(
            thumbnails,
            key=lambda item: (item.get('width') or 0) * (item.get('height') or 0),
        )
        return _normalize_thumbnail(best['url'], source)
    return _normalize_thumbnail(info.get('thumbnail'), source)


def _source_id(info: dict, source: str, url: str) -> str:
    """Достаёт ID результата для YouTube или SoundCloud."""
    candidate = str(info.get('id') or '').strip()
    if candidate:
        return candidate

    if source == SOURCE_YOUTUBE:
        match = _YOUTUBE_ID.search(url)
        if match:
            return match.group(1)
    else:
        match = re.search(r'/tracks/(\d+)', url)
        if match:
            return match.group(1)

    raise DownloadError(f'yt-dlp не вернул ID результата ({SOURCE_TITLE[source]}).')


def _clean_title(title: str) -> str:
    """Убирает из названия видео типовые суффиксы YouTube."""
    return _YOUTUBE_SUFFIX.sub('', title).strip()


def _split_artist_title(text: str) -> tuple[str, str]:
    """Делит строку «Исполнитель - Название» на две части."""
    if ' - ' in text:
        artist, track = text.split(' - ', 1)
        return artist.strip(), track.strip()
    return 'Unknown Artist', text.strip()


def _extract_metadata(info: dict, query: str) -> tuple[str, str]:
    """Определяет исполнителя и название трека из info-словаря yt-dlp.

    Приоритет: явные поля yt-dlp (artist/track), затем разбор заголовка видео
    вида «Исполнитель - Название».
    """
    raw_title = info.get('title') or query
    artist = (info.get('artist') or info.get('creator') or info.get('uploader') or '').strip()
    track = (info.get('track') or '').strip()

    parsed_artist, parsed_track = _split_artist_title(_clean_title(raw_title))

    if not track:
        track = parsed_track
    if not artist:
        artist = parsed_artist
    if not artist or artist.casefold() == track.casefold():
        artist = 'Unknown Artist'
    return track, artist


def _guess_image_mime(path: Path) -> str:
    """Определяет MIME-тип изображения по «магическим байтам» файла."""
    with open(path, 'rb') as f:
        header = f.read(12)
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if header.startswith(b'\x89PNG'):
        return 'image/png'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def _embed_metadata(
    mp3_path: Path,
    title: str,
    artist: str,
    cover_path: Path | None,
    album: str | None = None,
) -> None:
    """Вшивает ID3-теги (название, исполнитель, альбом, обложка) в MP3."""
    try:
        tags = ID3(str(mp3_path))
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall('TIT2')
    tags.delall('TPE1')
    tags.delall('TALB')
    tags.delall('APIC')

    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TPE1(encoding=3, text=[artist]))
    if album:
        tags.add(TALB(encoding=3, text=[album]))
    if cover_path and cover_path.is_file():
        with open(cover_path, 'rb') as f:
            cover_data = f.read()
        tags.add(
            APIC(
                encoding=3,
                mime=_guess_image_mime(cover_path),
                type=3,  # 3 = фронтальная обложка
                desc='Cover',
                data=cover_data,
            )
        )

    tags.save(str(mp3_path))


def _validate_url(url: str, source: str) -> None:
    """Пускаем только http(s)-URL нужного источника (защита от file:// и т.п.)."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise DownloadError(f'Некорректный URL: {url}')

    host = (parsed.hostname or '').lower()
    if source == SOURCE_YOUTUBE:
        allowed = {'youtube.com', 'www.youtube.com', 'm.youtube.com',
                   'music.youtube.com', 'youtu.be', 'www.youtu.be'}
    else:
        allowed = {'soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com',
                   'on.soundcloud.com', 'api.soundcloud.com'}
    if host not in allowed:
        raise DownloadError(f'URL не принадлежит {SOURCE_TITLE[source]}: {url}')


def _fetch_cover(url: str, dest_stem: Path) -> Path | None:
    """Скачивает обложку по прямому URL и определяет её расширение по байтам."""
    try:
        request = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(request, timeout=SOCKET_TIMEOUT) as response:
            data = response.read(10 * 1024 * 1024)
    except (OSError, ValueError):
        return None

    if data.startswith(b'\xff\xd8\xff'):
        suffix = '.jpg'
    elif data.startswith(b'\x89PNG'):
        suffix = '.png'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        suffix = '.webp'
    else:
        return None

    path = dest_stem.with_suffix(suffix)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# Поиск (синхронные функции — запускаются в отдельном потоке)
# --------------------------------------------------------------------------


def _search_entries(query: str, source: str, limit: int) -> list[dict]:
    """Ищет до `limit` вариантов без скачивания (extract_flat=True).

    Возвращает список словарей для интерфейса: source, source_id, url,
    title, artist, duration, thumbnail.
    """
    opts = _base_ydl_opts()
    opts.update({
        'extract_flat': True,
        'ignoreerrors': True,
        'skip_download': True,
    })
    search = f'{SEARCH_PREFIX[source]}{limit}:{query}'
    if source == SOURCE_YOUTUBE and limit == 1:
        # Для основного сценария добавляем «audio», чтобы получить аудиоверсию.
        search = f'ytsearch1:{query} audio'

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(search, download=False)
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError,
            OSError, TimeoutError) as exc:
        raise DownloadError(
            f'Не удалось выполнить поиск на {SOURCE_TITLE[source]}: {exc}'
        ) from exc

    entries = (result or {}).get('entries') or []
    candidates: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get('webpage_url') or entry.get('url') or ''
        if source == SOURCE_YOUTUBE and not url.startswith('http'):
            url = f'https://www.youtube.com/watch?v={entry.get("id") or ""}'
        if not url.startswith('http'):
            continue

        try:
            entry_id = _source_id(entry, source, url)
        except DownloadError:
            continue

        artist = (entry.get('artist') or entry.get('uploader')
                  or entry.get('creator') or entry.get('channel') or 'Unknown Artist')
        candidates.append({
            'source': source,
            'source_id': entry_id,
            'url': url,
            'title': entry.get('title') or query,
            'artist': artist,
            'duration': int(entry.get('duration') or 0) or None,
            'thumbnail': _pick_thumbnail(entry, source),
        })
    return candidates


def _search_one(query: str, source: str) -> dict:
    """Ищет один результат и возвращает минимальные данные для скачивания."""
    candidates = _search_entries(query, source, 1)
    if not candidates:
        raise DownloadError(f'По запросу «{query}» ничего не найдено на {SOURCE_TITLE[source]}.')
    return candidates[0]


def _probe_url(url: str, source: str) -> dict:
    """Быстро получает id/url/title по уже выбранному пользователем URL."""
    opts = _base_ydl_opts()
    opts.update({'extract_flat': True, 'skip_download': True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError,
            OSError, TimeoutError) as exc:
        raise DownloadError(f'Не удалось открыть URL: {exc}') from exc

    if not info:
        raise DownloadError('yt-dlp не вернул информацию о выбранном треке.')
    return {
        'source': source,
        'source_id': _source_id(info, source, url),
        'url': info.get('webpage_url') or url,
        'title': info.get('title') or url,
        'thumbnail': _pick_thumbnail(info, source),
    }


# --------------------------------------------------------------------------
# Скачивание
# --------------------------------------------------------------------------


def _prepare_cover(
    info: dict,
    source: str,
    video_id: str,
    stem: str,
    cover_src: Path | None,
) -> Path | None:
    """Переименовывает обложку в красивое имя и улучшает её у SoundCloud.

    У SoundCloud обложки часто приходят с суффиксом `-large`; заменяем его
    на `-t500x500` и скачиваем более крупную версию.
    """
    if source == SOURCE_SOUNDCLOUD:
        original = info.get('thumbnail') or _pick_thumbnail(info, source)
        upgraded = _normalize_thumbnail(original, source)
        if upgraded and upgraded != original:
            high_res = _fetch_cover(upgraded, COVERS_DIR / f'{stem}_hires')
            if high_res is not None:
                if cover_src is not None and cover_src != high_res:
                    cover_src.unlink(missing_ok=True)
                cover_src = high_res
        elif cover_src is None and original:
            cover_src = _fetch_cover(original, COVERS_DIR / f'{stem}_hires')

    if cover_src is None:
        return None
    cover_path = COVERS_DIR / f'{stem}{cover_src.suffix.lower()}'
    cover_src.replace(cover_path)
    return cover_path


def _download_audio(track: dict, query: str, source: str) -> tuple[dict, Path, Path | None]:
    """Скачивает аудио, конвертирует в MP3, сохраняет обложку и метаданные.

    Возвращает (info, путь_к_mp3, путь_к_обложке_или_None).
    """
    video_id = track['source_id']
    # Красивое имя файла строим заранее и сами (не полагаясь на переименование
    # от yt-dlp): сначала качаем во временные имена по id, затем переименовываем.
    title_hint = _clean_title(track.get('title') or query)
    stem = f"{sanitize_filename(title_hint) or 'unknown'} [{_track_key(source, video_id)}]"

    opts = _base_ydl_opts()
    opts.update({
        'format': 'bestaudio/best',
        # Отдельные шаблоны для аудио и обложки: yt-dlp поддерживает ключ
        # 'thumbnail' в outtmpl (устаревший thumb_outtmpl игнорируется).
        'outtmpl': {
            'default': str(TRACKS_DIR / '%(id)s.%(ext)s'),
            'thumbnail': str(COVERS_DIR / '%(id)s.%(ext)s'),
        },
        'writethumbnail': True,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        # socket_timeout / retries / User-Agent уже заданы в _base_ydl_opts().
        'postprocessors': [
            # Конвертация в MP3: 0 = лучшее качество (VBR ~245–320 kbps).
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '0'},
            # Обложку приводим к JPG (если исходная webp/png).
            {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
        ],
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track['url'], download=True)
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError,
            OSError, TimeoutError) as exc:
        raise DownloadError(f'Не удалось скачать аудио: {exc}') from exc

    if not info:
        raise DownloadError('yt-dlp не вернул информацию о скачанном треке.')

    # --- Аудио: находим готовый MP3 по id и переименовываем в красивое имя ---
    mp3_src = TRACKS_DIR / f'{video_id}.mp3'
    if not mp3_src.is_file():
        raise DownloadError(
            'Файл MP3 не был создан. Проверьте, что FFmpeg доступен в PATH '
            'или лежит в корне проекта рядом с main.py.'
        )
    mp3_path = TRACKS_DIR / f'{stem}.mp3'
    mp3_src.replace(mp3_path)

    # --- Обложка: ищем по id (jpg после конвертации, webp/png — запасные) ---
    cover_src = None
    for ext in ('.jpg', '.webp', '.png'):
        candidate = COVERS_DIR / f'{video_id}{ext}'
        if candidate.is_file():
            cover_src = candidate
            break
    cover_path = _prepare_cover(info, source, video_id, stem, cover_src)

    # --- Метаданные: вшиваем теги в MP3 ---
    title, artist = _extract_metadata(info, query)
    _embed_metadata(mp3_path, title, artist, cover_path, album=info.get('album'))

    return info, mp3_path, cover_path


async def _save_downloaded(track: dict, query: str, source: str) -> dict:
    """Скачивает трек (с защитой от дублей) и добавляет запись в БД."""
    track_key = _track_key(source, track['source_id'])

    # Дешёвая проверка БД вне блокировки.
    existing = await database.get_track_by_youtube_id(track_key)
    if existing:
        return existing

    # Полное скачивание — под блокировкой, чтобы два одновременных запроса
    # на один и тот же трек не скачали его дважды.
    async with _download_lock:
        existing = await database.get_track_by_youtube_id(track_key)
        if existing:
            return existing

        try:
            info, mp3_path, cover_path = await asyncio.to_thread(
                _download_audio, track, query, source
            )
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(f'Ошибка при скачивании трека: {exc}') from exc

        title, artist = _extract_metadata(info, query)
        return await database.add_track(
            title=title,
            artist=artist,
            duration=int(info.get('duration') or 0),
            file_path=str(mp3_path),
            cover_path=str(cover_path) if cover_path else None,
            youtube_id=track_key,
        )


# --------------------------------------------------------------------------
# Публичный API модуля
# --------------------------------------------------------------------------


async def search_and_download(query: str) -> dict:
    """Ищет трек на YouTube, при ошибке — на SoundCloud, и скачивает его.

    Возвращает запись о треке из БД. Если трек с таким ключом
    (`yt_<id>` / `sc_<id>`) уже скачан, дубликат не создаётся.
    """
    query = query.strip()
    if not query:
        raise DownloadError('Пустой поисковый запрос.')

    errors: list[str] = []
    for source in (SOURCE_YOUTUBE, SOURCE_SOUNDCLOUD):
        try:
            # yt-dlp — синхронная библиотека: тяжёлую работу выполняем
            # в отдельном потоке, чтобы не блокировать event loop FastAPI.
            # wait_for — страховка от «зависшего» YouTube: по истечении
            # таймаута фоллбэк на SoundCloud срабатывает так же, как при
            # бот-детекте или любой другой ошибке.
            video = await asyncio.wait_for(
                asyncio.to_thread(_search_one, query, source),
                timeout=SEARCH_TIMEOUT,
            )
            return await asyncio.wait_for(
                _save_downloaded(video, query, source),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            errors.append(f'{SOURCE_TITLE[source]}: таймаут')
        except DownloadError as exc:
            errors.append(str(exc))
        except Exception as exc:  # сетевые и прочие неожиданные ошибки
            errors.append(f'{SOURCE_TITLE[source]}: {exc}')

    raise DownloadError(
        'Не удалось найти и скачать трек ни на YouTube, ни на SoundCloud. '
        + '; '.join(errors)
    )


async def search_candidates(query: str) -> list[dict]:
    """Быстрый поиск до 5 вариантов для интерфейса (без скачивания).

    Сначала YouTube (`ytsearch5:`), при ошибке или пустом результате —
    SoundCloud (`scsearch5:`).
    """
    query = query.strip()
    if not query:
        raise DownloadError('Пустой поисковый запрос.')

    errors: list[str] = []
    for source in (SOURCE_YOUTUBE, SOURCE_SOUNDCLOUD):
        try:
            candidates = await asyncio.wait_for(
                asyncio.to_thread(_search_entries, query, source, 5),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            errors.append(f'{SOURCE_TITLE[source]}: таймаут')
            continue
        except DownloadError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:
            errors.append(f'{SOURCE_TITLE[source]}: {exc}')
            continue
        if candidates:
            return candidates
        errors.append(f'{SOURCE_TITLE[source]}: нет результатов')

    raise DownloadError('Кандидаты не найдены. ' + '; '.join(errors))


async def download_by_url(url: str, source: str) -> dict:
    """Скачивает конкретный трек, выбранный пользователем из кандидатов."""
    if source not in SOURCE_PREFIX:
        raise DownloadError(f'Неизвестный источник: {source}')
    url = url.strip()
    if not url:
        raise DownloadError('Пустой URL трека.')

    _validate_url(url, source)

    try:
        track = await asyncio.wait_for(
            asyncio.to_thread(_probe_url, url, source),
            timeout=SEARCH_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise DownloadError('Таймаут при открытии выбранного трека.') from exc
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f'Не удалось открыть выбранный трек: {exc}') from exc

    try:
        return await asyncio.wait_for(
            _save_downloaded(track, track['title'], source),
            timeout=DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise DownloadError('Таймаут при скачивании выбранного трека.') from exc
