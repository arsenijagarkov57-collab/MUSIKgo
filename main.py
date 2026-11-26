"""main.py — основной HTTP-сервер музыкального сервиса (FastAPI).

Структура проекта:
    music_service/
    ├── main.py           <- этот файл (FastAPI-приложение)
    ├── database.py       <- SQLite через aiosqlite
    ├── downloader.py     <- поиск и скачивание через yt-dlp
    ├── requirements.txt
    ├── storage/tracks/   <- скачанные .mp3
    ├── storage/covers/   <- обложки .jpg
    └── static/           <- фронтенд (index.html, app.js, style.css)

Запуск на Windows (из папки music_service):

    1. Установите Python 3.10+ (проверка: python --version)
    2. Создайте виртуальное окружение:
         python -m venv .venv
         .venv\\Scripts\\activate
    3. Установите зависимости:
         pip install -r requirements.txt
    4. Установите FFmpeg (нужен downloader'у для конвертации в MP3):
         winget install ffmpeg
    5. Запустите сервер:
         uvicorn main:app --host 0.0.0.0 --port 8000
           --proxy-headers --forwarded-allow-ips='*'
       (или просто: python main.py — эти параметры уже включены в код)
        Порт берётся из PORT (по умолчанию 8000); в Docker/HF Spaces PORT=7860.

    Swagger-документация API: /docs на любом домене сервиса.

    Публикация в интернет: Cloudflare Tunnel, nginx/Caddy или любой другой
    обратный прокси с TLS. Все адреса в API и во фронтенде относительные
    (/api/..., /storage/...), поэтому сервер не привязан к конкретному хосту
    и одинаково работает на любом HTTPS-домене. Заголовки X-Forwarded-Proto
    и X-Forwarded-For доверяются через ProxyHeadersMiddleware: даже если
    прокси подключается к Uvicorn по plaintext HTTP, приложение знает,
    что внешний запрос пришёл по https (важно для PWA и Media Session).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import database
import downloader
import features

# --------------------------------------------------------------------------
# Пути и порт
# --------------------------------------------------------------------------
# Локально — 8000; Hugging Face Spaces / PaaS задают PORT=7860 (или свой).
PORT = int(os.environ.get('PORT', 8000))

BASE_DIR = Path(__file__).resolve().parent
TRACKS_DIR = BASE_DIR / 'storage' / 'tracks'
COVERS_DIR = BASE_DIR / 'storage' / 'covers'
STATIC_DIR = BASE_DIR / 'static'

# APK необязателен: сетевой роут (/download/app) проверяет файл на диске.
APK_FILENAME = 'MyMusic.apk'
APK_PATH = STATIC_DIR / APK_FILENAME
APK_MEDIA_TYPE = 'application/vnd.android.package-archive'

# Папки должны существовать до монтирования статики — создаём при импорте.
TRACKS_DIR.mkdir(parents=True, exist_ok=True)
COVERS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """При старте сервера создаём таблицы в БД, при остановке — закрываем их."""
    await database.init_db()
    yield
    await database.close_db()


app = FastAPI(
    title='Music Service',
    description='Локальный музыкальный стриминговый сервис (аналог Яндекс Музыки).',
    version='0.1.0',
    lifespan=lifespan,
)

# CORS: разрешаем все источники — удобно для разработки, когда фронтенд
# запускается на другом порту или домене.
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,  # обязательно False при allow_origins=['*']
    allow_methods=['*'],
    allow_headers=['*'],
)

# Cloudflare Tunnel и другие прокси подключаются к Uvicorn по HTTP, но снаружи
# отдают HTTPS. Доверяем X-Forwarded-Proto / X-Forwarded-For, чтобы scheme
# запроса был https: иначе браузер не разрешит установку PWA и Media Session.
# '*' — осознанный выбор: сервер стоит за доверенным туннелем/прокси.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts='*')


def _download_app_response(request: Request) -> Response:
    """Отдаёт APK или понятный JSON, если сборка ещё не создана.

    GET возвращает файл/JSON, HEAD — только заголовки (фронтенд использует
    HEAD, чтобы проверить наличие APK, не скачивая десятки мегабайт).
    """
    if APK_PATH.is_file():
        headers = {'Cache-Control': 'no-store'}
        if request.method == 'HEAD':
            headers['Content-Length'] = str(APK_PATH.stat().st_size)
            headers['Content-Disposition'] = f'attachment; filename="{APK_FILENAME}"'
            return Response(status_code=200, media_type=APK_MEDIA_TYPE, headers=headers)
        return FileResponse(
            APK_PATH,
            media_type=APK_MEDIA_TYPE,
            filename=APK_FILENAME,
            headers=headers,
        )

    # Файла нет — не отдаём 404, чтобы фронтенд мог вежливо предложить PWA.
    if request.method == 'HEAD':
        return Response(
            status_code=200,
            media_type='application/json',
            headers={'Cache-Control': 'no-store'},
        )
    return JSONResponse(
        {'status': 'not_built'},
        headers={'Cache-Control': 'no-store'},
    )


# Точный маршрут объявляется ДО монтирования /static, иначе StaticFiles
# перехватит запрос к файлу. Оба адреса ведут к одной и той же «умной» логике:
# кнопка в интерфейсе ссылается на /static/MyMusic.apk, а API использует
# /download/app.
@app.api_route('/static/MyMusic.apk', methods=['GET', 'HEAD'], include_in_schema=False)
@app.api_route('/download/app', methods=['GET', 'HEAD'])
async def download_app(request: Request) -> Response:
    """Скачать APK приложения (или status=not_built, если файл не собран)."""
    return _download_app_response(request)


# Раздача статики: скачанные треки, обложки и папка будущего фронтенда.
app.mount('/storage/tracks', StaticFiles(directory=TRACKS_DIR), name='tracks')
app.mount('/storage/covers', StaticFiles(directory=COVERS_DIR), name='covers')
app.mount('/static', StaticFiles(directory=STATIC_DIR, html=True), name='static')


def _public_track(track: dict) -> dict:
    """Дополняет запись из БД готовыми URL для стриминга и обложки."""
    data = dict(track)
    data['stream_url'] = f"/storage/tracks/{Path(data['file_path']).name}"
    if data.get('cover_path'):
        data['cover_url'] = f"/storage/covers/{Path(data['cover_path']).name}"
    return data


class DownloadRequest(BaseModel):
    """Запрос на скачивание конкретного трека из списка кандидатов."""

    url: str = Field(..., min_length=1, description='URL выбранного трека')
    source: str = Field(
        ..., pattern='^(youtube|soundcloud)$', description='Источник трека'
    )


class PlaylistCreate(BaseModel):
    """Запрос на создание плейлиста."""

    name: str = Field(..., min_length=1, description='Название плейлиста')


class PlaylistTrackAdd(BaseModel):
    """Запрос на добавление трека в плейлист."""

    track_id: int = Field(..., gt=0, description='id трека')


class WaveRequest(BaseModel):
    """Необязательное тело запроса для «Моей волны»."""

    seed_track_id: int | None = Field(default=None, description='id опорного трека')
    exclude_youtube_ids: list[str] = Field(
        default_factory=list, description='Уже сыгранные треки'
    )


# --------------------------------------------------------------------------
# Эндпоинты
# --------------------------------------------------------------------------


@app.get('/')
async def root() -> FileResponse:
    """Отдаёт готовый интерфейс (статический index.html)."""
    return FileResponse(
        STATIC_DIR / 'index.html',
        media_type='text/html',
        headers={'Cache-Control': 'no-cache'},
    )


@app.get('/api/health')
async def health() -> dict:
    """Простая проверка живости для Cloudflare / Render / Hugging Face."""
    return {'status': 'ok'}


@app.get('/sw.js')
async def service_worker() -> FileResponse:
    """Отдаёт Service Worker из /static с правом управлять всем Origin.

    Файл лежит в /static/sw.js, но регистрируется как '/sw.js': только так
    он может контролировать главную страницу ('/' вне папки /static/).
    """
    return FileResponse(
        STATIC_DIR / 'sw.js',
        media_type='text/javascript',
        headers={
            'Service-Worker-Allowed': '/',
            'Cache-Control': 'no-cache',
        },
    )


@app.get('/api/tracks')
async def get_all_tracks() -> list[dict]:
    """Список всех треков библиотеки, отсортированный по новизне."""
    tracks = await database.get_all_tracks()
    return [_public_track(track) for track in tracks]


@app.get('/api/tracks/{track_id}')
async def get_track(track_id: int) -> dict:
    """Информация об одном треке."""
    track = await database.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail='Трек не найден')
    return _public_track(track)


@app.get('/api/search')
async def search(q: str = Query(..., min_length=1, description='Поисковый запрос')) -> dict:
    """Быстрый поиск по локальной БД с авто-скачиванием первого результата.

    Сначала ищет совпадения в локальной БД (без учёта регистра). Если в БД
    пусто — автоматически скачивает трек через downloader (YouTube, при
    ошибке — SoundCloud). Поле source в ответе показывает, откуда пришёл
    результат. Для выбора из нескольких вариантов используйте
    `/api/search_candidates` + `/api/download`.
    """
    local = await database.search_tracks(q)
    if local:
        return {'source': 'local', 'tracks': [_public_track(track) for track in local]}

    try:
        downloaded = await downloader.search_and_download(q)
    except downloader.DownloadError as exc:
        # 502 = Bad Gateway: не получилось достать трек из внешнего источника.
        raise HTTPException(status_code=502, detail=str(exc))

    return {'source': 'download', 'tracks': [_public_track(downloaded)]}


@app.get('/api/search_candidates')
async def search_candidates(
    q: str = Query(..., min_length=1, description='Поисковый запрос'),
) -> list[dict]:
    """Быстрый поиск до 5 вариантов для интерфейса, без скачивания.

    Сначала YouTube (`ytsearch5`), при ошибке или нуле результатах —
    SoundCloud (`scsearch5`). Каждый элемент содержит source, source_id,
    url, title, artist, duration и thumbnail.
    """
    try:
        return await downloader.search_candidates(q)
    except downloader.DownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post('/api/download')
async def download(payload: DownloadRequest) -> dict:
    """Скачивает выбранный пользователем трек и сохраняет его в БД."""
    try:
        track = await downloader.download_by_url(payload.url, payload.source)
    except downloader.DownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return _public_track(track)


@app.post('/api/tracks/{track_id}/like')
async def toggle_like(track_id: int) -> dict:
    """Переключает лайк у трека (is_liked: 0 <-> 1)."""
    track = await database.toggle_like(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail='Трек не найден')
    return _public_track(track)


@app.get('/api/stream/{track_id}')
async def stream_track(track_id: int) -> FileResponse:
    """Стриминг MP3 с поддержкой перемотки (HTTP Range).

    Starlette FileResponse сам обрабатывает заголовок Range: возвращает
    статус 206 и нужный фрагмент файла, поэтому перемотка работает
    в любом плеере «из коробки».
    """
    track = await database.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail='Трек не найден')

    audio_path = Path(track['file_path'])
    if not audio_path.is_file():
        raise HTTPException(status_code=410, detail='Аудиофайл отсутствует на диске')

    # Считаем прослушивание.
    await database.increment_play_count(track_id)

    return FileResponse(
        path=audio_path,
        media_type='audio/mpeg',
        headers={'Accept-Ranges': 'bytes'},
    )


# --------------------------------------------------------------------------
# Тексты песен (караоке)
# --------------------------------------------------------------------------


@app.get('/api/tracks/{track_id}/lyrics')
async def get_lyrics(track_id: int) -> dict:
    """Возвращает синхронизированный и обычный текст трека из LRCLIB.

    Внешний API — «лучшее усилие»: при отсутствии результата или недоступности
    сети возвращаются пустые строки со статусом 200.
    """
    track = await database.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail='Трек не найден')
    return await features.fetch_lyrics(track)


# --------------------------------------------------------------------------
# «Моя волна»
# --------------------------------------------------------------------------


@app.post('/api/wave/next')
async def wave_next(payload: WaveRequest | None = None) -> dict:
    """Подбирает и скачивает следующий трек рекомендательной волны."""
    request = payload or WaveRequest()
    if request.seed_track_id is not None:
        if await database.get_track(request.seed_track_id) is None:
            raise HTTPException(status_code=404, detail='Трек не найден')
    try:
        track = await features.next_wave_track(
            seed_track_id=request.seed_track_id,
            exclude_youtube_ids=request.exclude_youtube_ids,
        )
    except features.WaveError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return _public_track(track)


# --------------------------------------------------------------------------
# Плейлисты и удаление треков
# --------------------------------------------------------------------------


@app.get('/api/playlists')
async def get_playlists() -> list[dict]:
    """Список плейлистов с количеством треков."""
    return await database.get_playlists()


@app.post('/api/playlists')
async def create_playlist(payload: PlaylistCreate) -> dict:
    """Создаёт новый плейлист."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail='Название плейлиста не может быть пустым')
    return await database.create_playlist(name)


@app.delete('/api/playlists/{playlist_id}')
async def delete_playlist(playlist_id: int) -> dict:
    """Удаляет плейлист (треки при этом не удаляются из библиотеки)."""
    if not await database.delete_playlist(playlist_id):
        raise HTTPException(status_code=404, detail='Плейлист не найден')
    return {'ok': True, 'id': playlist_id}


@app.get('/api/playlists/{playlist_id}/tracks')
async def get_playlist_tracks(playlist_id: int) -> list[dict]:
    """Треки конкретного плейлиста."""
    if await database.get_playlist(playlist_id) is None:
        raise HTTPException(status_code=404, detail='Плейлист не найден')
    tracks = await database.get_playlist_tracks(playlist_id)
    return [_public_track(track) for track in tracks]


@app.post('/api/playlists/{playlist_id}/tracks')
async def add_track_to_playlist(playlist_id: int, payload: PlaylistTrackAdd) -> dict:
    """Добавляет трек в плейлист."""
    result = await database.add_track_to_playlist(playlist_id, payload.track_id)
    if result is None:
        raise HTTPException(status_code=404, detail='Плейлист или трек не найден')
    return {'ok': True, 'playlist_id': playlist_id, 'track_id': payload.track_id}


@app.delete('/api/playlists/{playlist_id}/tracks/{track_id}')
async def remove_track_from_playlist(playlist_id: int, track_id: int) -> dict:
    """Убирает трек из плейлиста."""
    if not await database.remove_track_from_playlist(playlist_id, track_id):
        raise HTTPException(status_code=404, detail='Трек в плейлисте не найден')
    return {'ok': True, 'playlist_id': playlist_id, 'track_id': track_id}


@app.delete('/api/tracks/{track_id}')
async def delete_track(track_id: int) -> dict:
    """Удаляет трек из библиотеки и его файлы (MP3 + обложку) с диска."""
    if not await database.delete_track(track_id):
        raise HTTPException(status_code=404, detail='Трек не найден')
    return {'ok': True, 'id': track_id}


if __name__ == '__main__':
    import uvicorn

    # 0.0.0.0 — сервер доступен не только с самого ПК, но и с телефона
    # в локальной Wi-Fi-сети и через публичный HTTPS-туннель.
    # proxy_headers + forwarded_allow_ips='*' — корректный https:// схема
    # за Cloudflare Tunnel / обратным прокси (дублирует ProxyHeadersMiddleware).
    uvicorn.run(
        'main:app',
        host='0.0.0.0',
        port=PORT,
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips='*',
    )
