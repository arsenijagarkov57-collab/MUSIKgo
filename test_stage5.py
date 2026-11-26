"""test_stage5.py — автотесты публикации сервиса (этап 5).

Проверяем:
  • «умный» APK-роут (/download/app и /static/MyMusic.apk);
  • поддержку заголовков обратного прокси (X-Forwarded-Proto / For);
  • отсутствие захардкоженных хостов в бэкенде и фронтенде;
  • готовность репозитория к публикации (gitignore, Dockerfile, README).

Запуск из папки music_service:
    .venv\\Scripts\\python.exe test_stage5.py
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient

import database
import main

BASE_DIR = Path(__file__).resolve().parent
APK_BYTES = b'FAKE-APK-FOR-TESTS'


def check_sources() -> None:
    """Исходники не привязаны к хосту и содержат всю новую логику."""
    main_src = (BASE_DIR / 'main.py').read_text(encoding='utf-8')
    for fragment in (
        'ProxyHeadersMiddleware',
        "trusted_hosts='*'",
        'forwarded_allow_ips',
        "host='0.0.0.0'",
        '/download/app',
        'application/vnd.android.package-archive',
        "'status': 'not_built'",
    ):
        assert fragment in main_src, fragment
    assert '127.0.0.1' not in main_src, 'в main.py остался захардкоженный хост'

    app_src = (BASE_DIR / 'static' / 'app.js').read_text(encoding='utf-8')
    for fragment in (
        'beforeinstallprompt',
        'deferredPrompt.prompt()',
        "fetch(APK_CHECK_URL, { method: 'HEAD'",
        '/download/app',
        '/static/MyMusic.apk',
        'openInstallModal',
        'copyShareLink',
        'Android',
        'iPhone',
        'Windows',
    ):
        assert fragment in app_src, fragment
    for forbidden in ('127.0.0.1', 'localhost'):
        assert forbidden not in app_src, f'в app.js остался {forbidden}'

    html = (BASE_DIR / 'static' / 'index.html').read_text(encoding='utf-8')
    for fragment in (
        'Скачать приложение',
        'Установить приложение прямо сейчас',
        'Скачать APK для Android',
        'Скопировать ссылку на сервис',
        'На экран „Домой“',
        'data-action="open-install"',
        'href="/static/MyMusic.apk"',
        '/static/manifest.json',
    ):
        assert fragment in html, fragment
    for forbidden in ('127.0.0.1', 'localhost'):
        assert forbidden not in html, f'в index.html остался {forbidden}'

    css = (BASE_DIR / 'static' / 'style.css').read_text(encoding='utf-8')
    assert '.install-primary' in css and '.install-dialog' in css

    manifest = json.loads((BASE_DIR / 'static' / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['start_url'] == '/' and manifest['scope'] == '/'
    assert all(not icon['src'].startswith(('http://', 'https://'))
               for icon in manifest['icons']), 'иконки манифеста должны быть относительными'
    print('[OK] исходники: относительные URL, PWA-установка и прокси-заголовки')


def check_repo_files() -> None:
    """Репозиторий готов к public git / Docker-деплою."""
    gitignore = (BASE_DIR / '.gitignore').read_text(encoding='utf-8')
    for fragment in (
        '.venv/', '__pycache__/', 'music.db', 'storage/tracks/*',
        'storage/covers/*', '*.exe', '.gitkeep',
    ):
        assert fragment in gitignore, fragment

    dockerfile = (BASE_DIR / 'Dockerfile').read_text(encoding='utf-8')
    assert 'python:3.11-slim' in dockerfile
    assert 'ffmpeg' in dockerfile
    assert 'uvicorn main:app' in dockerfile

    readme = (BASE_DIR / 'README.md').read_text(encoding='utf-8')
    for fragment in ('Моя волна', 'Караоке', 'YouTube', 'SoundCloud', 'PWA', 'APK', 'Cloudflare'):
        assert fragment.lower() in readme.lower(), fragment
    print('[OK] .gitignore / Dockerfile / README.md на месте')


def check_apk_route(client: TestClient, apk_path: Path) -> None:
    """APK есть -> файл, APK нет -> понятный JSON, а не 404."""
    with patch.object(main, 'APK_PATH', apk_path):
        apk_path.unlink(missing_ok=True)

        for path in ('/download/app', '/static/MyMusic.apk'):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.json() == {'status': 'not_built'}, response.text
            assert 'json' in response.headers['content-type']

        head = client.head('/download/app')
        assert head.status_code == 200
        assert head.content == b''
        assert 'json' in head.headers['content-type']
        print('[OK] APK отсутствует: GET/HEAD -> status=not_built (не 404)')

        apk_path.write_bytes(APK_BYTES)
        try:
            for path in ('/download/app', '/static/MyMusic.apk'):
                response = client.get(path)
                assert response.status_code == 200, response.text
                assert response.content == APK_BYTES, path
                assert response.headers['content-type'] == 'application/vnd.android.package-archive'
                assert response.headers['content-disposition'] == 'attachment; filename="MyMusic.apk"'

            head = client.head('/download/app')
            assert head.status_code == 200
            assert head.content == b''
            assert head.headers['content-length'] == str(len(APK_BYTES))
            assert head.headers['content-disposition'] == 'attachment; filename="MyMusic.apk"'
            print('[OK] APK найден: GET отдаёт файл, HEAD — только заголовки')
        finally:
            apk_path.unlink(missing_ok=True)


def check_proxy_headers(client: TestClient) -> None:
    """За Cloudflare Tunnel схема запроса должна быть https, клиент — реальный IP."""
    async def probe(request: Request) -> dict:
        return {
            'scheme': request.url.scheme,
            'client': request.client.host if request.client else None,
        }

    main.app.add_api_route('/_scheme_probe', probe, methods=['GET'], include_in_schema=False)
    try:
        plain = client.get('/_scheme_probe').json()
        assert plain['scheme'] == 'http', plain

        proxied = client.get('/_scheme_probe', headers={
            'x-forwarded-proto': 'https',
            'x-forwarded-for': '203.0.113.7',
        }).json()
        assert proxied['scheme'] == 'https', proxied
        assert proxied['client'] == '203.0.113.7', proxied
        print('[OK] X-Forwarded-Proto: http -> https, X-Forwarded-For: клиент восстановлен')
    finally:
        main.app.routes[:] = [
            route for route in main.app.routes if route.path != '/_scheme_probe'
        ]


def check_health(client: TestClient) -> None:
    response = client.get('/api/health')
    assert response.status_code == 200 and response.json() == {'status': 'ok'}
    print('[OK] GET /api/health -> 200')


def run() -> None:
    check_sources()
    check_repo_files()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        (tmp / 'storage' / 'tracks').mkdir(parents=True)
        (tmp / 'storage' / 'covers').mkdir(parents=True)

        old_db_path, old_base = database.DB_PATH, database.BASE_DIR
        database.DB_PATH = tmp / 'music.db'
        database.BASE_DIR = tmp
        try:
            with TestClient(main.app) as client:
                check_health(client)
                check_apk_route(client, tmp / 'MyMusic.apk')
                check_proxy_headers(client)
        finally:
            asyncio.run(database.close_db())
            database.DB_PATH, database.BASE_DIR = old_db_path, old_base

    print('\nВСЕ ПРОВЕРКИ ЭТАПА 5 ПРОЙДЕНЫ')


if __name__ == '__main__':
    run()
