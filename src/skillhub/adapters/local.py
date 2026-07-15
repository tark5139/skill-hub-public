"""First-release filesystem adapters for supported local Agents."""

from __future__ import annotations

from pathlib import Path

from .base import AgentAdapter


class CodexAdapter(AgentAdapter):
    adapter_id = "codex"
    display_name = "OpenAI Codex"
    relative_root = Path(".codex/skills")
    executable = "codex"


class ClaudeCodeAdapter(AgentAdapter):
    adapter_id = "claude-code"
    display_name = "Anthropic Claude Code"
    relative_root = Path(".claude/skills")
    executable = "claude"


class TraeCNAdapter(AgentAdapter):
    adapter_id = "trae-cn"
    display_name = "TRAE CN IDE"
    relative_root = Path(".trae-cn/skills")


class OpenClawAdapter(AgentAdapter):
    adapter_id = "openclaw"
    display_name = "OpenClaw"
    relative_root = Path(".openclaw/skills")
    executable = "openclaw"


class HermesAdapter(AgentAdapter):
    adapter_id = "hermes"
    display_name = "Nous Research Hermes Agent"
    relative_root = Path(".hermes/skills")
    executable = "hermes"


LOCAL_ADAPTER_TYPES: tuple[type[AgentAdapter], ...] = (
    CodexAdapter,
    ClaudeCodeAdapter,
    TraeCNAdapter,
    OpenClawAdapter,
    HermesAdapter,
)
