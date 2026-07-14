from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import HubError


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    is_admin: bool = False

    def require(self, scope: str) -> None:
        if self.is_admin or scope in self.scopes:
            return
        raise HubError(403, "insufficient_scope", f"Required scope: {scope}")


bearer = HTTPBearer(auto_error=False)


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HubError(401, "authentication_required", "A Bearer token is required")
    settings = request.app.state.settings
    if not secrets.compare_digest(credentials.credentials, settings.admin_token):
        raise HubError(401, "invalid_token", "The access token is invalid or revoked")
    return Principal(
        subject=settings.admin_subject,
        scopes=frozenset(
            {
                "skill.read",
                "skill.write",
                "skill.review",
                "skill.publish",
                "skill.admin",
                "agent.sync",
            }
        ),
        is_admin=True,
    )


def require_scope(scope: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        principal.require(scope)
        return principal

    return dependency
