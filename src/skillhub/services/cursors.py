from __future__ import annotations

import base64
import json
from datetime import datetime

from ..errors import HubError


def encode_cursor(updated_at: datetime, record_id: str) -> str:
    raw = json.dumps(
        {"updated_at": updated_at.isoformat(), "id": record_id}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(data["updated_at"]), str(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HubError(400, "invalid_cursor", "The pagination cursor is malformed") from exc
