from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(slots=True)
class HubError(Exception):
    status: int
    code: str
    detail: str
    extra: dict[str, Any] | None = None


async def hub_error_handler(request: Request, error: HubError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    body: dict[str, Any] = {
        "type": f"https://skill-hub.local/problems/{error.code}",
        "title": error.code.replace("_", " ").title(),
        "status": error.status,
        "detail": error.detail,
        "instance": str(request.url.path),
        "request_id": request_id,
    }
    if error.extra:
        body.update(error.extra)
    headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
    return JSONResponse(
        status_code=error.status,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )
