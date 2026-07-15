from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..errors import HubError
from ..models import IdempotencyRecord


def request_digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock_key(session: Session, *, principal_id: str, key: str | None) -> None:
    """Serialize equal idempotency keys for the life of the PostgreSQL transaction."""

    if not key or not session.bind or session.bind.dialect.name != "postgresql":
        return
    # PostgreSQL text values cannot contain NUL bytes. Hash a canonical tuple first so
    # untrusted principal/key text is always valid SQL text while preserving an
    # unambiguous lock identity.
    lock_value = request_digest({"principal_id": principal_id, "key": key})
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_value, 0))"),
        {"lock_value": lock_value},
    )


def replay_if_present(
    session: Session,
    *,
    principal_id: str,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
) -> tuple[int, dict[str, Any]] | None:
    if not key:
        return None
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.principal_id == principal_id,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if record is None:
        return None
    if record.method != method or record.path != path or record.request_hash != request_hash:
        raise HubError(
            409,
            "idempotency_key_reused",
            "The Idempotency-Key was already used for a different request",
        )
    return record.status_code, record.response_json


def remember_response(
    session: Session,
    *,
    principal_id: str,
    key: str | None,
    method: str,
    path: str,
    request_hash: str,
    status_code: int,
    response: dict[str, Any],
) -> None:
    if not key:
        return
    session.add(
        IdempotencyRecord(
            principal_id=principal_id,
            idempotency_key=key,
            method=method,
            path=path,
            request_hash=request_hash,
            status_code=status_code,
            response_json=response,
        )
    )
