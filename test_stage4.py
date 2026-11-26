"""test_stage4.py — автотесты четвёртого этапа (PWA и телефон).

Запуск из папки music_service:
    .venv\\Scripts\\python.exe test_stage4.py

Проверяем через TestClient, что манифест, Service Worker и иконки отдаются
со статусом 200, а разметка содержит всё нужное для установки PWA.
База данных на время теста подменяется временной папкой.
"""
from __future__ import annotations

import asyncio
import struct
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import database
import main

BASE_DIR = Path(__file__).resolve().parent


def check_pwa_files(client: TestClient) -> None:
    """Манифест, Service Worker и обе иконки доступны по HTTP."""
    manifest_resp = client.get('/static/manifest.json')
    assert manifest_resp.status_code == 200, manifest_resp.status_code
    manifest = manifest_resp.json()
    assert manifest['name'] == 'Моя Музыка'
    assert manifest['short_name'] == 'Музыка'
    assert manifest['display'] == 'standalone'
    assert manifest['background_color'] == '#0e0e0e'
    assert manifest['theme_color'] == '#0e0e0e'
    assert manifest['start_url'] == '/'
    sizes = {icon['sizes'] for icon in manifest['icons']}
    assert {'192x192', '512x512'} <= sizes, sizes
    print('[OK] GET /static/manifest.json -> 200, поля корректны')

    sw_resp = client.get('/static/sw.js')
    assert sw_resp.status_code == 200, sw_resp.status_code
    sw = sw_resp.text
    for asset in ('/', '/static/style.css', '/static/app.js'):
        assert asset in sw, asset
    assert "path.startsWith('/api/')" in sw
    assert "path.startsWith('/storage/')" in sw
    print('[OK] GET /static/sw.js -> 200, API и аудио не кэшируются')

    for size in (192, 512):
        icon_resp = client.get(f'/static/icon-{size}.png')
        assert icon_resp.status_code == 200, icon_resp.status_code
        assert icon_resp.headers['content-type'].startswith('image/png')
        data = icon_resp.content
        assert data[:8] == b'\x89PNG\r\n\x1a\n', 'не PNG'
        width, height = struct.unpack('>II', data[16:24])
        assert (width, height) == (size, size), (width, height)
        print(f'[OK] GET /static/icon-{size}.png -> 200, {width}x{height}')

    # Корневой алиас нужен, чтобы SW с областью '/' мог управлять главной.
    root_sw = client.get('/sw.js')
    assert root_sw.status_code == 200
    assert root_sw.headers.get('service-worker-allowed') == '/'
    print('[OK] GET /sw.js -> 200, Service-Worker-Allowed: /')


def check_index(client: TestClient) -> None:
    """Индекс подключает манифест, иконки и регистрирует Service Worker."""
    html = client.get('/').text
    for fragment in (
        '<link rel="manifest" href="/static/manifest.json">',
        '<meta name="theme-color" content="#0e0e0e">',
        'rel="apple-touch-icon"',
        "navigator.serviceWorker.register('/sw.js'",
    ):
        assert fragment in html, fragment
    print('[OK] index.html: manifest, theme-color, Apple icon, SW registration')


def check_sources() -> None:
    """Сервер слушает 0.0.0.0, а плеер обновляет Media Session."""
    main_src = (BASE_DIR / 'main.py').read_text(encoding='utf-8')
    assert "host='0.0.0.0'" in main_src
    print('[OK] main.py: uvicorn слушает 0.0.0.0')

    app_src = (BASE_DIR / 'static' / 'app.js').read_text(encoding='utf-8')
    for fragment in (
        'function updateMediaSession()',
        'previoustrack: () => prevTrack()',
        'nexttrack: () => nextTrack()',
        'updateMediaSession();',
        'playsinline',
    ):
        assert fragment in app_src, fragment
    # Metadata обновляется и при запуске Waves через общий playTrack().
    assert 'playWaveTrack(track)' in app_src
    print('[OK] app.js: Media Session и обработчики гарнитуры обновляются при смене трека')


def run() -> None:
    check_sources()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        (tmp / 'storage' / 'tracks').mkdir(parents=True)
        (tmp / 'storage' / 'covers').mkdir(parents=True)

        old_db_path, old_base = database.DB_PATH, database.BASE_DIR
        database.DB_PATH = tmp / 'music.db'
        database.BASE_DIR = tmp
        try:
            with TestClient(main.app) as client:
                check_pwa_files(client)
                check_index(client)
        finally:
            asyncio.run(database.close_db())
            database.DB_PATH, database.BASE_DIR = old_db_path, old_base

    print('\nВСЕ ПРОВЕРКИ ЭТАПА 4 ПРОЙДЕНЫ')


if __name__ == '__main__':
    run()
