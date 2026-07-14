"""Agent adapter registry for the first Skill Hub release."""

from __future__ import annotations

from pathlib import Path

from .aily import AilyCloudConnector, AilyDrift, AilyMapping, aily_skill_fingerprint
from .base import AdapterError, AdapterHealth, AgentAdapter, resolve_home, validate_skill_name
from .local import (
    LOCAL_ADAPTER_TYPES,
    ClaudeCodeAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    TraeCNAdapter,
)
from .workbuddy import WorkBuddyAdapter, WorkBuddyImportResult

_ADAPTER_TYPES: dict[str, type[AgentAdapter]] = {
    adapter.adapter_id: adapter for adapter in (*LOCAL_ADAPTER_TYPES, WorkBuddyAdapter)
}
_ALIASES = {
    "claude": "claude-code",
    "trae": "trae-cn",
    "work-buddy": "workbuddy",
}


def adapter_ids() -> tuple[str, ...]:
    return tuple(_ADAPTER_TYPES)


def get_adapter(
    adapter_id: str,
    *,
    home: Path | str | None = None,
    root_override: Path | str | None = None,
) -> AgentAdapter:
    normalized = _ALIASES.get(adapter_id.lower(), adapter_id.lower())
    try:
        adapter_type = _ADAPTER_TYPES[normalized]
    except KeyError as exc:
        valid = ", ".join(adapter_ids())
        raise AdapterError(
            f"Unsupported local adapter {adapter_id!r}; choose one of: {valid}"
        ) from exc
    return adapter_type(home=home, root_override=root_override)


__all__ = [
    "AdapterError",
    "AdapterHealth",
    "AgentAdapter",
    "AilyCloudConnector",
    "AilyDrift",
    "AilyMapping",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "HermesAdapter",
    "OpenClawAdapter",
    "TraeCNAdapter",
    "WorkBuddyAdapter",
    "WorkBuddyImportResult",
    "adapter_ids",
    "aily_skill_fingerprint",
    "get_adapter",
    "resolve_home",
    "validate_skill_name",
]
