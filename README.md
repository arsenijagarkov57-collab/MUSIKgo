<div align="center">

# 🎵 MyMusic — свой музыкальный сервис

**Личная библиотека, поиск и скачивание треков, караоке, «Моя волна» и PWA-приложение — на любом устройстве и любом HTTPS-домене.**

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009485?logo=fastapi&logoColor=white)
![PWA](https://img.shields.io/badge/PWA-ready-FFCC00?logo=pwa&logoColor=black)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

---

## ✨ Что умеет сервис

| Фича | Описание |
| --- | --- |
| 📚 **Своя библиотека** | Все скачанные треки в одном месте: обложки, лайки, счётчик прослушиваний, удаление. |
| 🔎 **Умный поиск** | Сначала ищет по локальной БД, затем автоматически тянет трек с **YouTube** или **SoundCloud** через `yt-dlp`. Можно выбрать из нескольких кандидатов. |
| 🎤 **Караоке** | Синхронизированный текст песни из [LRCLIB](https://lrclib.net/): строки подсвечиваются по времени воспроизведения. Если синхронизации нет — показывается обычный текст. |
| 🌊 **«Моя волна»** | Бесконечный рекомендательный поток: подбирает похожие треки (Deezer / iTunes), предзагружает следующий и подстраивается под опорный трек. |
| 💛 **Коллекция и плейлисты** | Лайки, создание/удаление плейлистов, добавление и уборка треков — всё хранится в SQLite. |
| 🎧 **Плеер как у нативного приложения** | Перемотка (HTTP Range), громкость, shuffle, repeat, Media Session — управление с гарнитуры и экрана блокировки. |
| 📲 **PWA / APK** | Установка в один клик через `beforeinstallprompt`, «умная» ссылка на APK для Android, инструкция для iPhone и кнопка «Поделиться ссылкой». |
| 🌍 **Любой домен** | Все URL относительные (`/api/...`, `/storage/...`), прокси-заголовки `X-Forwarded-Proto` учитываются — работает за Cloudflare Tunnel, nginx, Caddy, на Hugging Face Spaces и Render. |

---

## 🚀 Быстрый запуск локально

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
winget install ffmpeg          # нужен для конвертации в MP3
python main.py                 # http://localhost:8000
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg        # или brew install ffmpeg
python main.py
```

Откройте `http://localhost:8000` — интерфейс готов. Swagger-документация доступна по `/docs`.

> **FFmpeg** можно не ставить глобально: положите `ffmpeg.exe` (или `ffmpeg`) рядом с `main.py` — он будет использован автоматически.

---

## 🐳 Запуск в Docker (один клик)

```bash
docker build -t mymusic .
docker run -d --name mymusic -p 8000:7860 \
  -v "%cd%/storage:/app/storage" \
  -v "%cd%/music.db:/app/music.db" \
  mymusic
```

Для Linux/macOS замените `%cd%` на `$PWD`. FFmpeg уже встроен в образ. Внутри
контейнера выставлен `PORT=7860` (как ожидает Hugging Face Spaces): внешний
порт 8000 пробрасывается на 7860. Если нужен порт 8000 и внутри контейнера,
добавьте `-e PORT=8000 -p 8000:8000`. Локальный запуск через `python main.py`
по-прежнему использует 8000 по умолчанию.

### Hugging Face Spaces

1. Создайте Space с типом **Docker**.
2. Загрузите в него содержимое папки `music_service` (`Dockerfile`, `main.py`, `static/`, `storage/tracks/.gitkeep`, `storage/covers/.gitkeep`, `requirements.txt`, `LICENSE`, `.gitignore`).
3. `PORT=7860`, `EXPOSE 7860` и `chmod -R 777 /app` уже настроены: Space поднимет сервис на `https://<ваш-логин>-<space>.hf.space`, SQLite и `/storage` доступны для записи от UID 1000.
4. Пробросьте том/переменные под свой сценарий хранения, если нужна персистентность библиотеки.

### Render / любой PaaS

Тип сервиса — **Docker**, команда запуска из `Dockerfile` уже настроена. Порт берётся из переменной `PORT`. Проверка живости — `GET /api/health`.

---

## 🌐 Публичный доступ по HTTPS

### Вариант A — Cloudflare Tunnel (рекомендуется)

Самый простой способ получить настоящий HTTPS-домен без открытия портов:

```bash
cloudflared tunnel --url http://localhost:8000
```

Cloudflared выдаст адрес вида `https://something.trycloudflare.com`. Он же выставит заголовок `X-Forwarded-Proto: https`, а приложение доверяет ему через `ProxyHeadersMiddleware`, поэтому:

- PWA-установка работает (нужен защищённый контекст);
- `navigator.serviceWorker`, Media Session и `beforeinstallprompt` доступны;
- все ссылки в интерфейсе остаются относительными и не содержат `127.0.0.1`/`localhost`.

### Вариант B — nginx / Caddy / другой reverse proxy

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;   # важно!
}
```

В Caddy достаточно `reverse_proxy localhost:8000` — он сам добавит нужные заголовки.

---

## 📲 Установка приложения

Нажмите **«Скачать приложение»** в боковом меню (на ПК) или в шапке (на телефоне). Откроется окно, которое само определяет платформу:

1. **PWA в один клик** — если браузер поддержал событие `beforeinstallprompt`, появляется большая жёлтая кнопка **«Установить приложение прямо сейчас»**. Приложение ставится на телефон или ПК как обычная программа, без браузерной панели.
2. **APK для Android** — кнопка ведёт на `/static/MyMusic.apk`. Сервер проверяет наличие файла: если сборки нет, `GET /download/app` возвращает `{"status":"not_built"}` (не 404), и интерфейс вежливо предлагает установку через браузер.
3. **iPhone / iPad** — карточка с инструкцией: «Поделиться» → «На экран „Домой“» → «Добавить».
4. **Скопировать ссылку на сервис** — одной кнопкой отправьте сайт другу.

> APK-сборка (`static/MyMusic.apk`) в репозиторий не включена: положите готовый файл рядом со статикой, и кнопка заработает автоматически.

---

## 🔌 API

| Метод | Путь | Что делает |
| --- | --- | --- |
| `GET` | `/` | Интерфейс приложения |
| `GET` | `/api/health` | Проверка живости (`{"status":"ok"}`) |
| `GET` | `/api/tracks` | Все треки библиотеки |
| `GET` | `/api/search?q=...` | Поиск + авто-скачивание первого результата |
| `GET` | `/api/search_candidates?q=...` | До 5 кандидатов без скачивания |
| `POST` | `/api/download` | Скачать выбранный трек (`url`, `source`) |
| `GET` | `/api/stream/{id}` | MP3 с поддержкой Range (перемотка) |
| `GET` | `/api/tracks/{id}/lyrics` | Текст песни для караоке |
| `POST` | `/api/wave/next` | Следующий трек «Моей волны» |
| `GET/POST/DELETE` | `/api/playlists...` | Управление плейлистами |
| `GET/HEAD` | `/download/app` | APK или `{"status":"not_built"}` |
| `GET/HEAD` | `/static/MyMusic.apk` | Та же «умная» логика (для прямой ссылки) |
| `GET` | `/sw.js`, `/static/manifest.json` | PWA Service Worker и манифест |

Полная интерактивная документация — `/docs`.

---

## 🗂 Структура проекта

```text
music_service/
├── main.py             # FastAPI-приложение, API, PWA и APK-роут
├── database.py         # SQLite (aiosqlite): треки, лайки, плейлисты
├── downloader.py       # yt-dlp: поиск и скачивание YouTube/SoundCloud
├── features.py         # караоке (LRCLIB) и «Моя волна» (Deezer/iTunes)
├── static/
│   ├── index.html      # интерфейс + модальное окно установки
│   ├── app.js          # плеер, поиск, PWA-логика, шаринг
│   ├── style.css       # тема, анимации, стили окна установки
│   ├── manifest.json   # PWA-манифест (без привязки к домену)
│   ├── sw.js           # Service Worker: кэш только «оболочки»
│   ├── icon-192.png
│   └── icon-512.png
├── storage/
│   ├── tracks/.gitkeep # скачанные MP3 (не коммитятся)
│   └── covers/.gitkeep # обложки (не коммитятся)
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .gitignore
└── test_stage*.py      # автотесты
```

---

## ✅ Тесты

```bash
.venv\Scripts\python.exe test_stage2.py   # поиск, скачивание, валидация (нужен интернет)
.venv\Scripts\python.exe test_stage3.py   # плейлисты, тексты, волна (без сети)
.venv\Scripts\python.exe test_stage4.py   # PWA: манифест, SW, иконки
.venv\Scripts\python.exe test_stage5.py   # публикация: APK-роут, прокси, установка
```

Для Linux/macOS используйте `.venv/bin/python`. Тесты этапов 3–5 работают без интернета.

---

## 🔒 О приватности

Сервис не требует регистрации и работает с локальной базой. Если публикуете его через туннель, обратите внимание: **авторизации нет**, поэтому доступ к библиотеке есть у всех, у кого есть ссылка. Для личного пользования держите домен/туннель в приватном режиме Cloudflare Access или не делитесь ссылкой.

---

<div align="center">

Сделано с любовью к музыке 🎶 и свободным open-source инструментам.

</div>
