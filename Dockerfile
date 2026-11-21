# syntax=docker/dockerfile:1
# Образ Telegram Summary Bot. Сборка: docker compose up -d --build
# Базовый образ закреплён по digest: обновление — осознанно (см. README, «Обновление»)
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PTB_TIMEDELTA=1 \
    DATABASE_PATH=/data/bot.db \
    BACKUP_DIR=/data/backups \
    HEARTBEAT_PATH=/tmp/summary-bot.heartbeat

WORKDIR /app

# Непривилегированный пользователь с фиксированным UID
RUN groupadd --system --gid 10001 bot \
 && useradd --system --uid 10001 --gid bot --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код принадлежит root и недоступен приложению на запись
COPY *.py ./
COPY prompts ./prompts
COPY tools ./tools

RUN mkdir -p /data && chown bot:bot /data && chmod 700 /data

USER bot
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD ["python", "healthcheck.py"]

STOPSIGNAL SIGTERM
CMD ["python", "bot.py"]
