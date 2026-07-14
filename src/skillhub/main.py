from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .api.routes import router
from .config import Settings, get_settings
from .db import create_db_engine, create_session_factory, initialize_database
from .errors import HubError, hub_error_handler
from .models import WorkerHeartbeat
from .storage import ObjectStorage, build_storage


def create_app(
    settings: Settings | None = None,
    *,
    storage: ObjectStorage | None = None,
    initialize_schema: bool | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    object_storage = storage or build_storage(settings)
    if initialize_schema is None:
        initialize_schema = settings.env != "production"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if initialize_schema:
            initialize_database(engine)
        yield
        engine.dispose()

    app = FastAPI(
        title="Personal Skill Hub Registry API",
        version="0.1.0",
        description=(
            "Private-first immutable Agent Skill registry. Errors use RFC 9457 Problem Details."
        ),
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = object_storage

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "If-Match",
                "If-None-Match",
                "X-Request-ID",
            ],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = incoming[:64] if incoming else uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self' https:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        return response

    app.add_exception_handler(HubError, hub_error_handler)  # type: ignore[arg-type]

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for raw_error in exc.errors():
            error = dict(raw_error)
            if "ctx" in error:
                error["ctx"] = {key: str(value) for key, value in error["ctx"].items()}
            errors.append(error)
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "https://skill-hub.local/problems/validation_error",
                "title": "Validation Error",
                "status": 422,
                "detail": "The request did not satisfy the API contract",
                "instance": request.url.path,
                "request_id": request.state.request_id,
                "errors": jsonable_encoder(errors),
            },
        )

    @app.get("/health/live", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def readiness() -> dict[str, str]:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            if settings.env == "production":
                heartbeat = session.get(WorkerHeartbeat, "default")
                maximum_age = timedelta(seconds=max(30.0, settings.worker_poll_seconds * 5))
                if heartbeat is None or heartbeat.last_seen_at < datetime.now(UTC) - maximum_age:
                    raise HubError(
                        503, "worker_unavailable", "Worker heartbeat is missing or stale"
                    )
        return {"status": "ready"}

    @app.get("/", include_in_schema=False)
    def root_console() -> RedirectResponse:
        return RedirectResponse("/console/", status_code=307)

    app.include_router(router)
    web_directory = Path(__file__).with_name("web")
    app.mount("/console", StaticFiles(directory=web_directory, html=True), name="console")
    return app


app = create_app()


def run() -> None:
    uvicorn.run("skillhub.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
