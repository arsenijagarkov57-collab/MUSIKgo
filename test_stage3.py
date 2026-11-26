"""test_stage3.py — автотесты третьего этапа (плейлисты, тексты, волна).

Запуск из папки music_service:
    .venv\\Scripts\\python.exe test_stage3.py

Все сетевые вызовы подменяются: тесты не требуют интернета, ключей API
и реальных MP3-файлов. База данных создаётся во временной папке.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import database
import downloader
import features
import main


# ---------------------------------------------------------------------------
# Фейковый HTTP-клиент для httpx.AsyncClient
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                'HTTP error', request=httpx.Request('GET', 'https://example.com'),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self.payload


class FakeAsyncClient:
    """Очередь ответов + журнал вызовов, когда-то отданных в httpx."""

    queue: list[FakeResponse] = []
    calls: list[tuple[str, dict | None, dict | None]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if not self.queue:
            raise AssertionError(f'Неожиданный HTTP-запрос: {url} {params}')
        return self.queue.pop(0)

    @classmethod
    def reset(cls, responses):
        cls.queue = list(responses)
        cls.calls = []


# ---------------------------------------------------------------------------
# Вспомогательная фабрика треков
# ---------------------------------------------------------------------------


async def make_track(tmp: Path, title: str, artist: str, youtube_id: str,
                     liked: bool = False) -> dict:
    mp3 = tmp / 'storage' / 'tracks' / f'{youtube_id}.mp3'
    cover = tmp / 'storage' / 'covers' / f'{youtube_id}.jpg'
    mp3.write_bytes(b'fake mp3 data')
    cover.write_bytes(b'\xff\xd8\xff fake jpg')
    track = await database.add_track(
        title=title, artist=artist, duration=180,
        file_path=str(mp3), cover_path=str(cover), youtube_id=youtube_id,
    )
    if liked:
        await database.toggle_like(track['id'])
    return track


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


def check_cleaning() -> None:
    """Очистка названий от мусора и разбор «Артист - Название»."""
    title, artist = features.clean_artist_title({
        'title': 'Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers [yt_5NV6Rdv1a3I]',
        'artist': 'Daft Punk',
    })
    assert title == 'Get Lucky', title
    assert artist == 'Daft Punk', artist

    assert features.clean_text('Песня  [320 kbps]') == 'Песня'
    assert features.clean_text('Artist - song (Official Video)') == 'Artist - song'

    title, artist = features.clean_artist_title({
        'title': 'Daft Punk - Get Lucky [320 kbps]', 'artist': 'Daft Punk',
    })
    assert (title, artist) == ('Get Lucky', 'Daft Punk'), (title, artist)
    print('[OK] очистка названий для поиска текстов')


async def check_lyrics(client: TestClient, tmp: Path) -> dict:
    """GET /api/tracks/{id}/lyrics: успех, пустой ответ и сетевой сбой."""
    track = await make_track(tmp, 'Get Lucky (Official Audio)', 'Daft Punk', 'yt_lyrics')

    FakeAsyncClient.reset([FakeResponse([{
        'id': 1,
        'trackName': 'Get Lucky',
        'artistName': 'Daft Punk',
        'plainLyrics': 'We are all getting lucky',
        'syncedLyrics': '[00:12.00]We are all\n[00:15.50]getting lucky',
    }])])
    with patch.object(features.httpx, 'AsyncClient', FakeAsyncClient):
        response = client.get(f'/api/tracks/{track["id"]}/lyrics')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['synced_lyrics'] == '[00:12.00]We are all\n[00:15.50]getting lucky', data
    assert data['plain_lyrics'] == 'We are all getting lucky', data

    url, params, headers = FakeAsyncClient.calls[0]
    assert url == features.LRCLIB_URL, url
    assert params['track_name'] == 'Get Lucky', params
    assert params['artist_name'] == 'Daft Punk', params
    assert headers['User-Agent'] == 'LocalMusicService/1.0', headers

    # Пустой результат → 200 с пустыми строками.
    FakeAsyncClient.reset([FakeResponse([])])
    with patch.object(features.httpx, 'AsyncClient', FakeAsyncClient):
        response = client.get(f'/api/tracks/{track["id"]}/lyrics')
    assert response.status_code == 200 and response.json() == {
        'synced_lyrics': '', 'plain_lyrics': '',
    }, response.text

    # Сбой API (500) тоже не должен ронять сервер.
    FakeAsyncClient.reset([FakeResponse({'error': 'oops'}, status_code=500)])
    with patch.object(features.httpx, 'AsyncClient', FakeAsyncClient):
        response = client.get(f'/api/tracks/{track["id"]}/lyrics')
    assert response.status_code == 200 and response.json() == {
        'synced_lyrics': '', 'plain_lyrics': '',
    }, response.text

    assert client.get('/api/tracks/999999/lyrics').status_code == 404
    print('[OK] GET /api/tracks/{id}/lyrics (успех / пусто / ошибка API)')
    return track


async def check_playlists(client: TestClient, track: dict) -> None:
    """Полный CRUD плейлистов и связей."""
    assert client.get('/api/playlists').json() == []

    response = client.post('/api/playlists', json={'name': 'Дорогая'})
    assert response.status_code == 200, response.text
    playlist = response.json()
    assert playlist['name'] == 'Дорогая' and isinstance(playlist['id'], int)
    playlist_id = playlist['id']

    assert client.post('/api/playlists', json={'name': '   '}).status_code == 400

    response = client.post(f'/api/playlists/{playlist_id}/tracks', json={'track_id': track['id']})
    assert response.status_code == 200, response.text

    # Повторное добавление не создаёт дубликат.
    client.post(f'/api/playlists/{playlist_id}/tracks', json={'track_id': track['id']})
    tracks = client.get(f'/api/playlists/{playlist_id}/tracks').json()
    assert [item['id'] for item in tracks] == [track['id']], tracks
    assert tracks[0]['stream_url'].startswith('/storage/tracks/'), tracks[0]

    playlists = client.get('/api/playlists').json()
    assert playlists[0]['track_count'] == 1, playlists

    assert client.post(f'/api/playlists/{playlist_id}/tracks', json={'track_id': 999999}).status_code == 404
    assert client.get('/api/playlists/999999/tracks').status_code == 404
    assert client.delete(f'/api/playlists/{playlist_id}/tracks/999999').status_code == 404

    assert client.delete(f'/api/playlists/{playlist_id}/tracks/{track["id"]}').status_code == 200
    assert client.get(f'/api/playlists/{playlist_id}/tracks').json() == []
    assert client.get('/api/playlists').json()[0]['track_count'] == 0

    assert client.delete(f'/api/playlists/{playlist_id}').status_code == 200
    assert client.get('/api/playlists').json() == []
    assert client.get(f'/api/playlists/{playlist_id}/tracks').status_code == 404
    assert client.delete(f'/api/playlists/{playlist_id}').status_code == 404
    print('[OK] CRUD /api/playlists и /api/playlists/{id}/tracks')


async def check_delete_track(client: TestClient, tmp: Path) -> None:
    """DELETE /api/tracks/{id}: БД, файлы и каскад плейлистов."""
    track = await make_track(tmp, 'Удаляемый трек', 'Artist', 'yt_delete')
    playlist = (await database.create_playlist('На удаление'))
    assert await database.add_track_to_playlist(playlist['id'], track['id']) is True

    response = client.delete(f'/api/tracks/{track["id"]}')
    assert response.status_code == 200, response.text
    assert client.get(f'/api/tracks/{track["id"]}').status_code == 404
    assert not Path(track['file_path']).exists(), track['file_path']
    assert not Path(track['cover_path']).exists(), track['cover_path']
    assert await database.get_playlist_tracks(playlist['id']) == []  # ON DELETE CASCADE
    assert client.delete(f'/api/tracks/{track["id"]}').status_code == 404

    await database.delete_playlist(playlist['id'])
    print('[OK] DELETE /api/tracks/{id} (диск + каскадное удаление связей)')


async def check_wave(client: TestClient, tmp: Path) -> None:
    """POST /api/wave/next: сид, лайкнутые,exclude, фоллбэк iTunes."""
    liked = await make_track(tmp, 'Get Lucky', 'Daft Punk', 'yt_wave_seed', liked=True)
    other = await make_track(tmp, 'Случайный трек', 'Other Artist', 'yt_wave_other')

    seed_artists: list[str] = []
    download_queries: list[str] = []
    download_counter = [0]
    offers = [
        ('Daft Punk', 'Around the World'),
        ('Daft Punk', 'Harder Better Faster Stronger'),
        ('Daft Punk', 'Excluded Song'),
    ]

    offer_index = [0]

    async def fake_recommendations(artist: str, use_related: bool = False):
        index = offer_index[0]
        offer_index[0] += 1
        seed_artists.append(artist)
        return [offers[min(index, len(offers) - 1)]]

    async def fake_download(query: str) -> dict:
        download_queries.append(query)
        download_counter[0] += 1
        title = query.split(' - ', 1)[1]
        return await make_track(
            tmp, title, 'Daft Punk',
            f'yt_wave_downloaded_{download_counter[0]}',
        )

    with (
        patch.object(features, 'fetch_recommendations', fake_recommendations),
        patch.object(downloader, 'search_and_download', fake_download),
    ):
        # Явно указанный сид.
        response = client.post('/api/wave/next', json={
            'seed_track_id': liked['id'], 'exclude_youtube_ids': [],
        })
        assert response.status_code == 200, (
            f'{response.text} | seed_artists={seed_artists} downloads={download_queries}'
        )
        result = response.json()
        assert result['artist'] == 'Daft Punk', result
        assert result['stream_url'].startswith('/storage/tracks/'), result
        assert seed_artists[-1] == 'Daft Punk', seed_artists
        assert download_queries[-1] == 'Daft Punk - Around the World', download_queries

        # Без сида опорой становится случайный лайкнутый трек.
        seed_artists.clear()
        response = client.post('/api/wave/next')
        assert response.status_code == 200, response.text
        assert seed_artists[-1] == 'Daft Punk', seed_artists

        # Исключённый кандидат не скачивается повторно.
        before = len(download_queries)
        response = client.post('/api/wave/next', json={
            'seed_track_id': other['id'],
            'exclude_youtube_ids': ['Excluded Song'],
        })
        assert response.status_code == 502, response.text
        assert len(download_queries) == before, download_queries

    assert client.post('/api/wave/next', json={'seed_track_id': 999999}).status_code == 404

    # Пустая библиотека → корректная ошибка, без падения сервера.
    for item in await database.get_all_tracks():
        await database.delete_track(item['id'])
    response = client.post('/api/wave/next')
    assert response.status_code == 502, response.text
    print('[OK] POST /api/wave/next (сид, лайк, exclude, ошибки)')


def check_network_fallback() -> None:
    """Deezer упал — должен сработать iTunes, парсер обоих форматов."""
    deezer_error = FakeResponse({'error': 'server down'}, status_code=500)
    itunes_ok = FakeResponse({'results': [{
        'artistName': 'Daft Punk', 'trackName': 'Get Lucky',
    }]})
    FakeAsyncClient.reset([deezer_error, itunes_ok])
    with patch.object(features.httpx, 'AsyncClient', FakeAsyncClient):
        result = asyncio.run(features.fetch_recommendations('Daft Punk'))
    assert result == [('Daft Punk', 'Get Lucky')], result
    assert FakeAsyncClient.calls[0][0].startswith('https://api.deezer.com/search')
    assert FakeAsyncClient.calls[1][0] == features.ITUNES_URL

    FakeAsyncClient.reset([FakeResponse({'data': [{
        'title': 'Harder, Better, Faster, Stronger',
        'artist': {'id': 27, 'name': 'Daft Punk'},
    }]})])
    with patch.object(features.httpx, 'AsyncClient', FakeAsyncClient):
        result = asyncio.run(features.fetch_recommendations('Daft Punk'))
    assert result == [('Daft Punk', 'Harder, Better, Faster, Stronger')], result
    print('[OK] фоллбэк Deezer -> iTunes и разбор ответов')


def check_routes() -> None:
    """Все новые эндпоинты зарегистрированы в приложении."""
    expected = {
        ('GET', '/api/tracks/{track_id}/lyrics'),
        ('POST', '/api/wave/next'),
        ('GET', '/api/playlists'),
        ('POST', '/api/playlists'),
        ('DELETE', '/api/playlists/{playlist_id}'),
        ('GET', '/api/playlists/{playlist_id}/tracks'),
        ('POST', '/api/playlists/{playlist_id}/tracks'),
        ('DELETE', '/api/playlists/{playlist_id}/tracks/{track_id}'),
        ('DELETE', '/api/tracks/{track_id}'),
    }
    registered = {
        (method, route.path)
        for route in main.app.routes
        for method in getattr(route, 'methods', set())
    }
    assert expected <= registered, expected - registered
    print('[OK] все новые эндпоинты зарегистрированы')


def run() -> None:
    check_cleaning()
    check_network_fallback()
    check_routes()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        (tmp / 'storage' / 'tracks').mkdir(parents=True)
        (tmp / 'storage' / 'covers').mkdir(parents=True)

        old_db_path, old_base = database.DB_PATH, database.BASE_DIR
        database.DB_PATH = tmp / 'music.db'
        database.BASE_DIR = tmp
        try:
            with TestClient(main.app) as client:
                async def scenario() -> None:
                    track = await check_lyrics(client, tmp)
                    await check_playlists(client, track)
                    await check_delete_track(client, tmp)
                    await check_wave(client, tmp)

                asyncio.run(scenario())
        finally:
            asyncio.run(database.close_db())
            database.DB_PATH, database.BASE_DIR = old_db_path, old_base

    print('\nВСЕ ПРОВЕРКИ ЭТАПА 3 ПРОЙДЕНЫ')


if __name__ == '__main__':
    run()
