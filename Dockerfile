FROM python:3.12-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid "${APP_GID}" skillhub \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home skillhub \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml constraints.txt README.md SECURITY.md LICENSE alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN python -m pip install --constraint constraints.txt . \
    && mkdir -p /app/var/storage \
    && chown -R skillhub:skillhub /app

USER skillhub

EXPOSE 8080

CMD ["skillhub-api"]
