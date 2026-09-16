# syntax=docker/dockerfile:1
# Образ сервера мониторинга. Контекст сборки — корень репозитория:
#   docker build -f server/Dockerfile .
# python:3.11-slim — та же версия Python, что в CI (ubuntu, 3.11) и в Ansible-деплое
# (uv/3.11); slim — без лишних системных пакетов, все зависимости из
# requirements.txt ставятся как чистые wheel-ы, компилятор не нужен.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Код сервера. В server_app.py RES_DIR, DB_PATH и CONFIG_PATH резолвятся от
# __file__, поэтому важно положить templates/, static/ и config.json РЯДОМ
# с server_app.py — так же, как в репозитории.
COPY server/server_app.py server/config.json ./server/
COPY server/templates ./server/templates
COPY server/static ./server/static

# DB_PATH захардкожен как <dir server_app.py>/server.db и не настраивается,
# поэтому файл БД выносим в /app/data через симлинк — этот каталог монтируется
# как volume в docker-compose. SQLite резолвит симлинк и кладёт journal-файл
# рядом с реальным файлом, т.е. тоже в volume.
RUN useradd --system --uid 1001 --home /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && ln -s /app/data/server.db /app/server/server.db \
    && chown -R app:app /app

USER app
# Рабочий каталог = каталог server_app.py (как WorkingDirectory в systemd-юните)
WORKDIR /app/server
EXPOSE 8000
CMD ["python", "server_app.py"]


# Стадия для прогона тестов внутри того же окружения, что и сервер
FROM base AS test
USER root
COPY requirements-dev.txt /app/
RUN pip install -r /app/requirements-dev.txt
COPY --chown=app:app server/tests /app/server/tests
USER app
WORKDIR /app/server
CMD ["python", "-m", "pytest", "tests", "-v"]
