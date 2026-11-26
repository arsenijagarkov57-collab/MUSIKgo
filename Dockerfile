# ============================================================================
# MyMusic — контейнер для Hugging Face Spaces, Render, Fly.io и любого Docker-хоста.
# FFmpeg нужен yt-dlp для конвертации YouTube/SoundCloud в MP3.
# Hugging Face Spaces запускает контейнер от UID 1000 и слушает порт 7860
# (переменная PORT), поэтому /app доступен для записи всем пользователям.
# ============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860

# ffmpeg — конвертация аудио; curl — healthcheck; ca-certificates — HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости: Docker переиспользует слой, если изменился только код.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . ./

# Папки для треков и обложек должны существовать до старта приложения.
# SQLite (music.db), /storage и любые временные файлы создаются от UID 1000.
RUN mkdir -p storage/tracks storage/covers \
    && chmod -R 777 /app

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# --proxy-headers + '*': корректный https:// за Cloudflare Tunnel / Reverse Proxy.
CMD exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-7860}" \
    --proxy-headers --forwarded-allow-ips='*'
