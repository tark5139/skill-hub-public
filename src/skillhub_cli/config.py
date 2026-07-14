"""Persistent CLI configuration with injectable HOME."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from skillhub.adapters import resolve_home

from .errors import StateError

DEFAULT_REGISTRY_URL = "http://127.0.0.1:8000/api/v1"


@dataclass
class CLIConfig:
    registry_url: str = DEFAULT_REGISTRY_URL
    token: str | None = None
    require_verified_signature: bool = True
    aily_base_url: str = "https://open.feishu.cn/open-apis/aily/v1"
    aily_app_id: str | None = None
    aily_access_token: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CLIConfig:
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["token"] = "configured" if self.token else None
        data["aily_access_token"] = "configured" if self.aily_access_token else None
        return data


def state_dir(home: Path | str | None = None) -> Path:
    return resolve_home(home) / ".skillhub"


def config_path(home: Path | str | None = None) -> Path:
    return state_dir(home) / "config.json"


def local_state_path(home: Path | str | None = None) -> Path:
    return state_dir(home) / "state.json"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_config(home: Path | str | None = None) -> CLIConfig:
    path = config_path(home)
    if not path.exists():
        return CLIConfig()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read CLI config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StateError(f"CLI config must be a JSON object: {path}")
    return CLIConfig.from_dict(payload)


def save_config(config: CLIConfig, home: Path | str | None = None) -> Path:
    path = config_path(home)
    _atomic_json_write(path, asdict(config))
    return path
