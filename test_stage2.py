"""test_stage2.py — проверка доработок второго этапа.

Запуск из папки music_service:
    .venv\\Scripts\\python.exe test_stage2.py

Сначала идут быстрые тесты с подменой сетевых функций (без интернета),
затем — реальное скачивание одного трека через TestClient.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import database
import downloader
from fastapi.testclient import TestClient
from mutagen.id3 import ID3
from pydantic import ValidationError

import main

BASE_DIR = Path(__file__).resolve().parent
TRACKS_DIR = downloader.TRACKS_DIR
COVERS_DIR = downloader.COVERS_DIR


def check_helpers() -> None:
    """Префиксы, локальный ffmpeg и замена -large → -t500x500."""
    assert downloader._track_key('youtube', 'abc') == 'yt_abc'
    assert downloader._track_key('soundcloud', '123') == 'sc_123'
    assert downloader._normalize_thumbnail(
        'https://i1.sndcdn.com/artworks-X-large.jpg', 'soundcloud'
    ) == 'https://i1.sndcdn.com/artworks-X-t500x500.jpg'
    assert downloader._normalize_thumbnail(
        'https://i1.sndcdn.com/artworks-X-large.jpg', 'youtube'
    ) == 'https://i1.sndcdn.com/artworks-X-large.jpg'
    opts = downloader._base_ydl_opts()
    assert ('ffmpeg_location' in opts) == downloader.LOCAL_FFMPEG.is_file()
    print('[OK] вспомогательные функции')


async def check_fallback_mocked() -> None:
    """YouTube «падает» — должны сработать scsearch1/scsearch5 и префикс sc_."""
    calls: list[tuple[str, str, int]] = []

    def fake_search(query: str, source: str, limit: int) -> list[dict]:
        calls.append((query, source, limit))
        if source == 'youtube':
            raise downloader.DownloadError('YouTube: бот-детект')
        return [{
            'source': 'soundcloud',
            'source_id': '987654321',
            'url': 'https://api.soundcloud.com/tracks/987654321',
            'title': 'Test Artist - Test Song',
            'artist': 'Test Artist',
            'duration': 120,
            'thumbnail': 'https://i1.sndcdn.com/artworks-X-t500x500.jpg',
        }]

    def fake_download(track: dict, query: str, source: str):
        assert source == 'soundcloud'
        stem = f'Test Song [{downloader._track_key(source, track["source_id"])}]'
        mp3 = TRACKS_DIR / f'{stem}.mp3'
        cover = COVERS_DIR / f'{stem}.jpg'
        mp3.write_bytes(b'ID3 fake mp3')
        cover.write_bytes(b'\xff\xd8\xff fake jpg')
        return {}, mp3, cover

    await database.init_db()
    # На всякий случай убираем следы предыдущих запусков теста.
    await database._db.execute(
        "DELETE FROM tracks WHERE youtube_id = 'sc_987654321'"
    )
    await database._db.commit()
    with (
        patch.object(downloader, '_search_entries', fake_search),
        patch.object(downloader, '_download_audio', fake_download),
    ):
        track = await downloader.search_and_download('Test Query')
        assert calls == [('Test Query', 'youtube', 1), ('Test Query', 'soundcloud', 1)], calls
        assert track['youtube_id'] == 'sc_987654321', track

        calls.clear()
        candidates = await downloader.search_candidates('Test Query')
        assert calls == [('Test Query', 'youtube', 5), ('Test Query', 'soundcloud', 5)], calls
        assert candidates[0]['source'] == 'soundcloud'
        assert candidates[0]['source_id'] == '987654321'

    await database._db.execute('DELETE FROM tracks WHERE id = ?', (track['id'],))
    await database._db.commit()
    Path(track['file_path']).unlink(missing_ok=True)
    Path(track['cover_path']).unlink(missing_ok=True)
    await database.close_db()
    print('[OK] fallback на SoundCloud и префикс sc_')


def check_real_download() -> None:
    """Реальное скачивание: кандидаты → POST /api/download → MP3 + JPG."""
    with TestClient(main.app) as client:
        response = client.get('/api/search_candidates', params={'q': 'Daft Punk Get Lucky'})
        assert response.status_code == 200, response.text
        candidates = response.json()
        assert candidates and isinstance(candidates, list)
        assert set(candidates[0]) == {
            'source', 'source_id', 'url', 'title', 'artist', 'duration', 'thumbnail'
        }
        print(f'[OK] кандидаты: {len(candidates)} шт., первый: {candidates[0]["title"][:60]}')

        chosen = next(item for item in candidates if item['source'] == 'youtube')
        response = client.post('/api/download', json={
            'url': chosen['url'], 'source': chosen['source'],
        })
        assert response.status_code == 200, response.text
        track = response.json()
        assert track['youtube_id'].startswith('yt_'), track

        mp3 = Path(track['file_path'])
        cover = Path(track['cover_path'])
        assert mp3.is_file() and mp3.stat().st_size > 100_000, mp3
        assert cover.is_file() and cover.stat().st_size > 3_000, cover
        assert cover.suffix.lower() == '.jpg', cover
        assert cover.read_bytes()[:3] == b'\xff\xd8\xff', 'обложка не JPG'
        assert mp3.read_bytes()[:3] == b'ID3', 'в MP3 нет ID3-тегов'
        assert ID3(str(mp3)).getall('APIC'), 'в MP3 нет вшитой обложки'
        print(f'[OK] скачано: {mp3.name} ({mp3.stat().st_size} байт)')
        print(f'[OK] обложка: {cover.name} ({cover.stat().st_size} байт, JPG)')


def check_validation() -> None:
    """Некорректные source/URL не должны попадать в скачивание."""
    try:
        main.DownloadRequest(url='https://example.com/x', source='rutube')
    except ValidationError:
        pass
    else:
        raise AssertionError('неверный source прошёл валидацию')

    with TestClient(main.app) as client:
        response = client.post('/api/download', json={
            'url': 'file:///C:/Windows/win.ini', 'source': 'youtube',
        })
        assert response.status_code == 502, response.text
    print('[OK] валидация запроса')


if __name__ == '__main__':
    try:
        check_helpers()
        asyncio.run(check_fallback_mocked())
        check_real_download()
        check_validation()
        print('\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ')
    finally:
        # aiosqlite держит фоновый поток — без close_db процесс не выйдет.
        asyncio.run(database.close_db())
