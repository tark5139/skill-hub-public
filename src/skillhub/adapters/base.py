"""Shared contracts for Skill Hub Agent adapters."""

from __future__ import annotations

import os
import re
import shutil
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class AdapterError(RuntimeError):
    """Raised when an Agent adapter cannot safely perform an operation."""


def validate_skill_name(name: str) -> str:
    """Validate a directory-safe Skill name and return it unchanged."""

    if not _SKILL_NAME.fullmatch(name):
        raise AdapterError(
            f"Skill name must match ^[a-z0-9][a-z0-9._-]{{0,62}}$; received {name!r}"
        )
    return name


def resolve_home(home: Path | str | None = None) -> Path:
    """Resolve HOME with an explicit, test-friendly override."""

    candidate = home or os.environ.get("SKILLHUB_HOME")
    if candidate is None:
        return Path.home().resolve()
    return Path(candidate).expanduser().resolve()


@dataclass(frozen=True)
class AdapterHealth:
    """A machine-readable adapter health result."""

    adapter: str
    support_level: str
    root: str | None
    root_exists: bool
    writable: bool
    executable: str | None
    executable_found: bool | None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "support_level": self.support_level,
            "root": self.root,
            "root_exists": self.root_exists,
            "writable": self.writable,
            "executable": self.executable,
            "executable_found": self.executable_found,
            "notes": list(self.notes),
        }


class AgentAdapter(ABC):
    """Base class for a Skill Hub integration target."""

    adapter_id: ClassVar[str]
    display_name: ClassVar[str]
    support_level: ClassVar[str] = "ga"
    automatic_install: ClassVar[bool] = True
    relative_root: ClassVar[Path | None] = None
    executable: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        home: Path | str | None = None,
        root_override: Path | str | None = None,
    ) -> None:
        self.home = resolve_home(home)
        self._root_override = Path(root_override).expanduser().resolve() if root_override else None

    @property
    def root(self) -> Path | None:
        if self._root_override is not None:
            return self._root_override
        if self.relative_root is None:
            return None
        return self.home / self.relative_root

    def destination(self, skill_name: str) -> Path:
        root = self.root
        if not self.automatic_install or root is None:
            raise AdapterError(f"{self.display_name} does not support direct local installation")
        return root / validate_skill_name(skill_name)

    def backup_root(self, skill_name: str) -> Path:
        """Return an out-of-scan backup root on the same filesystem as the target."""

        root = self.root
        if root is None:
            raise AdapterError(f"{self.display_name} has no local Skill root")
        return root.parent / ".skillhub-backups" / root.name / validate_skill_name(skill_name)

    def doctor(self) -> AdapterHealth:
        root = self.root
        root_exists = bool(root and root.exists())
        writable = False
        if root is not None:
            probe = root if root_exists else root.parent
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            writable = os.access(probe, os.W_OK)
        executable_path = shutil.which(self.executable) if self.executable else None
        return AdapterHealth(
            adapter=self.adapter_id,
            support_level=self.support_level,
            root=str(root) if root else None,
            root_exists=root_exists,
            writable=writable,
            executable=self.executable,
            executable_found=(executable_path is not None) if self.executable else None,
        )
