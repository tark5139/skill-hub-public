from __future__ import annotations

from typing import Any

from skillhub.services.idempotency import lock_key, request_digest


class _Dialect:
    name = "postgresql"


class _Bind:
    dialect = _Dialect()


class _RecordingSession:
    bind = _Bind()

    def __init__(self) -> None:
        self.parameters: dict[str, Any] | None = None

    def execute(self, _statement: object, parameters: dict[str, Any]) -> None:
        self.parameters = parameters


def test_postgresql_lock_value_is_nul_free_and_unambiguous() -> None:
    session = _RecordingSession()
    lock_key(session, principal_id="owner\x00one", key="publish\x00v1")  # type: ignore[arg-type]

    assert session.parameters is not None
    lock_value = session.parameters["lock_value"]
    assert lock_value == request_digest(
        {"principal_id": "owner\x00one", "key": "publish\x00v1"}
    )
    assert "\x00" not in lock_value
    assert len(lock_value) == 64


def test_lock_coordinates_cannot_collapse_at_a_text_delimiter() -> None:
    first = request_digest({"principal_id": "owner:key", "key": "version"})
    second = request_digest({"principal_id": "owner", "key": "key:version"})

    assert first != second
