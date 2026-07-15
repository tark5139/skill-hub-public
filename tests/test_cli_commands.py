from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from skillhub_cli.config import load_config
from skillhub_cli.main import app


class FakeClient:
    def __init__(self, base_url: str, *, token: str | None = None, **_: Any) -> None:
        self.base_url = base_url
        self.token = token

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def close(self) -> None:
        pass

    def me(self) -> dict[str, str]:
        return {"subject": "tester"}

    def search(self, query: str, **_: Any) -> dict[str, Any]:
        return {"items": [{"namespace": "personal", "name": query}]}

    def describe(self, namespace: str, name: str) -> dict[str, str]:
        return {"namespace": namespace, "name": name}


def test_login_config_and_search_commands(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr("skillhub_cli.main.RegistryClient", FakeClient)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--home",
            str(tmp_path),
            "login",
            "--token",
            "secret",
            "--registry-url",
            "https://hub.example/api/v1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "secret" not in result.output
    assert load_config(tmp_path).token == "secret"

    result = runner.invoke(
        app,
        [
            "--home",
            str(tmp_path),
            "config",
            "set",
            "--aily-app-id",
            "app_1",
            "--aily-access-token",
            "aily-secret",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "aily-secret" not in result.output

    result = runner.invoke(app, ["--home", str(tmp_path), "config", "show"])
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["token"] == "configured"
    assert shown["aily_access_token"] == "configured"

    result = runner.invoke(app, ["--home", str(tmp_path), "search", "pdf"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["items"][0]["name"] == "pdf"

    result = runner.invoke(app, ["--home", str(tmp_path), "search"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["items"][0]["name"] == ""


def test_cli_accepts_documented_aliases_and_compact_skill_reference(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr("skillhub_cli.main.RegistryClient", FakeClient)
    runner = CliRunner()
    login_result = runner.invoke(
        app,
        [
            "--home",
            str(tmp_path),
            "login",
            "--token",
            "secret",
            "--url",
            "https://hub.example",
        ],
    )
    assert login_result.exit_code == 0, login_result.output
    described = runner.invoke(
        app, ["--home", str(tmp_path), "describe", "personal/hello-skill"]
    )
    assert described.exit_code == 0, described.output
    assert json.loads(described.output) == {"name": "hello-skill", "namespace": "personal"}

    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0
    assert version.output.strip() == "0.1.3"


def test_cli_help_exposes_required_lifecycle_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "login",
        "config",
        "search",
        "describe",
        "install",
        "update",
        "status",
        "rollback",
        "uninstall",
        "doctor",
    ):
        assert command in result.output


def test_local_lifecycle_commands_do_not_require_login(tmp_path: Path) -> None:
    runner = CliRunner()
    for command in (
        ["status"],
        ["rollback", "personal", "missing", "--agent", "codex"],
        ["uninstall", "personal", "missing", "--agent", "codex"],
    ):
        result = runner.invoke(app, ["--home", str(tmp_path), *command])
        assert "Not logged in" not in result.output

    status_result = runner.invoke(app, ["--home", str(tmp_path), "status"])
    assert status_result.exit_code == 0
    assert json.loads(status_result.output) == {"installations": []}
