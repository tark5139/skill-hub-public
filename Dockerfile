FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

RUN groupadd --gid "${APP_GID}" skillhub \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home skillhub \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY constraints.txt requirements-image.txt ./

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --retries 8 --timeout 60 --only-binary=:all: \
    --constraint constraints.txt --requirement requirements-image.txt

COPY pyproject.toml README.md SECURITY.md LICENSE alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN python -m pip install --no-deps --no-build-isolation . \
    && mkdir -p /app/var/storage \
    && chown -R skillhub:skillhub /app

USER skillhub

EXPOSE 8080

CMD ["skillhub-api"]
