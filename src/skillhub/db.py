from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import AuditEvent, Base, RegistryChange, Review, SkillFile, SkillVersion


def create_db_engine(settings: Settings) -> Engine:
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(settings.database_url, **kwargs)
    if settings.database_url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def get_session(request: Request) -> Generator[Session, None, None]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


@event.listens_for(Session, "before_flush")
def enforce_append_only_and_immutable(session: Session, *_: object) -> None:
    """Application-level WORM guard, complemented by object lock in production.

    Published version payload fields and evidence rows must not be editable through an accidental
    future code path. PostgreSQL backups and database roles remain part of the production boundary.
    """

    for value in session.deleted:
        if isinstance(value, (AuditEvent, RegistryChange, Review)):
            raise ValueError("audit and review evidence is append-only")
        if isinstance(value, SkillVersion) and value.immutable:
            raise ValueError("immutable Skill versions cannot be deleted")
        if isinstance(value, SkillFile) and value.version.immutable:
            raise ValueError("files of an immutable Skill version cannot be deleted")

    protected = {
        "skill_id",
        "upload_id",
        "semver",
        "sha256",
        "artifact_key",
        "manifest_key",
        "manifest_sha256",
        "manifest_json",
        "scan_status",
        "signature_status",
        "immutable",
    }
    for value in session.dirty:
        if isinstance(value, (AuditEvent, RegistryChange, Review)):
            if session.is_modified(value, include_collections=False):
                raise ValueError("audit and review evidence is append-only")
            continue
        if isinstance(value, SkillVersion):
            state = inspect(value)
            immutable_history = state.attrs.immutable.history
            old_immutable = (
                immutable_history.deleted[0] is True
                if immutable_history.deleted
                else state.attrs.immutable.loaded_value is True
            )
            if old_immutable and any(state.attrs[name].history.has_changes() for name in protected):
                raise ValueError("published Skill version payload is immutable")
            if old_immutable and state.attrs.status.history.has_changes():
                history = state.attrs.status.history
                previous = history.deleted[0] if history.deleted else None
                current = history.added[0] if history.added else value.status
                allowed_transitions = {
                    ("submitted", "approved"),
                    ("submitted", "rejected"),
                    ("approved", "published"),
                    ("published", "deprecated"),
                }
                if (previous, current) not in allowed_transitions:
                    raise ValueError("immutable Skill version lifecycle transition is invalid")
        if isinstance(value, SkillFile) and value.version.immutable:
            if session.is_modified(value, include_collections=False):
                raise ValueError("files of an immutable Skill version cannot be edited")
