from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def append_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    request_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
        before_json=before,
        after_json=after,
    )
    session.add(event)
    return event
