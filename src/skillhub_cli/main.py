"""`skillctl` command line interface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from skillhub import __version__
from skillhub.adapters import AilyCloudConnector, AilyMapping, resolve_home

from .config import CLIConfig, load_config, save_config
from .errors import SkillHubError
from .installer import SkillInstaller
from .registry import RegistryClient

app = typer.Typer(
    name="skillctl",
    help="Verified Skill Hub registry client and multi-Agent installer.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect or update local CLI configuration.", no_args_is_help=True)
aily_app = typer.Typer(help="Read/invoke-only Feishu Aily cloud connector.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(aily_app, name="aily")


@dataclass
class Runtime:
    home: Path


def _version_callback(value: bool) -> bool:
    if value:
        typer.echo(__version__)
        raise typer.Exit()
    return value


@app.callback()
def callback(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            envvar="SKILLHUB_HOME",
            help="Override HOME for Agent paths and local state.",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the skillctl version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    del version
    ctx.obj = Runtime(home=resolve_home(home))


def _runtime(ctx: typer.Context) -> Runtime:
    if not isinstance(ctx.obj, Runtime):
        raise typer.Exit(2)
    return ctx.obj


def _echo(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _skill_reference(namespace_or_reference: str, name: str | None) -> tuple[str, str]:
    if name is not None:
        if "/" in namespace_or_reference or "/" in name:
            raise SkillHubError("Use either `namespace name` or `namespace/name`, not both")
        return namespace_or_reference, name
    parts = namespace_or_reference.split("/")
    if len(parts) != 2 or not all(parts):
        raise SkillHubError("Skill reference must be `namespace/name`")
    return parts[0], parts[1]


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1)


def _client(config: CLIConfig) -> RegistryClient:
    if not config.token:
        raise SkillHubError("Not logged in; run `skillctl login --token ...`")
    return RegistryClient(config.registry_url, token=config.token)


def _installer(ctx: typer.Context) -> tuple[SkillInstaller, RegistryClient]:
    runtime = _runtime(ctx)
    config = load_config(runtime.home)
    client = _client(config)
    installer = SkillInstaller(
        client,
        home=runtime.home,
        require_verified_signature=config.require_verified_signature,
    )
    return installer, client


def _local_installer(ctx: typer.Context) -> tuple[SkillInstaller, RegistryClient]:
    """Build an installer for commands that never need Registry network access."""

    runtime = _runtime(ctx)
    config = load_config(runtime.home)
    client = RegistryClient(config.registry_url, token=config.token)
    installer = SkillInstaller(
        client,
        home=runtime.home,
        require_verified_signature=config.require_verified_signature,
    )
    return installer, client


@app.command()
def login(
    ctx: typer.Context,
    token: str = typer.Option(..., "--token", prompt=True, hide_input=True),
    registry_url: str | None = typer.Option(None, "--registry-url", "--url"),
) -> None:
    """Validate a caller-provided Bearer token with GET /api/v1/me and save it locally."""

    runtime = _runtime(ctx)
    config = load_config(runtime.home)
    candidate_url = registry_url or config.registry_url
    try:
        with RegistryClient(candidate_url, token=token) as client:
            identity = client.me()
        config.registry_url = candidate_url
        config.token = token
        path = save_config(config, runtime.home)
        _echo({"authenticated": True, "identity": identity, "config_path": str(path)})
    except Exception as exc:
        _fail(exc)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    runtime = _runtime(ctx)
    try:
        _echo(load_config(runtime.home).public_dict())
    except Exception as exc:
        _fail(exc)


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    registry_url: str | None = typer.Option(None, "--registry-url"),
    require_verified_signature: bool | None = typer.Option(
        None,
        "--require-verified-signature/--allow-unverified-signature",
    ),
    aily_base_url: str | None = typer.Option(None, "--aily-base-url"),
    aily_app_id: str | None = typer.Option(None, "--aily-app-id"),
    aily_access_token: str | None = typer.Option(None, "--aily-access-token", hide_input=True),
) -> None:
    """Update only explicitly supplied configuration values."""

    runtime = _runtime(ctx)
    try:
        config = load_config(runtime.home)
        updates = {
            "registry_url": registry_url,
            "require_verified_signature": require_verified_signature,
            "aily_base_url": aily_base_url,
            "aily_app_id": aily_app_id,
            "aily_access_token": aily_access_token,
        }
        for key, value in updates.items():
            if value is not None:
                setattr(config, key, value)
        path = save_config(config, runtime.home)
        _echo({"saved": True, "config_path": str(path), "config": config.public_dict()})
    except Exception as exc:
        _fail(exc)


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(""),
    limit: int = typer.Option(20, min=1, max=100),
    compatibility: str | None = typer.Option(None, "--agent"),
) -> None:
    """Search private, authorization-filtered registry entries."""

    runtime = _runtime(ctx)
    config = load_config(runtime.home)
    try:
        with _client(config) as client:
            _echo(client.search(query, limit=limit, compatibility=compatibility))
    except Exception as exc:
        _fail(exc)


@app.command()
def describe(
    ctx: typer.Context,
    namespace_or_reference: str = typer.Argument(..., metavar="NAMESPACE[/NAME]"),
    name: str | None = typer.Argument(None, metavar="NAME"),
) -> None:
    """Show metadata, labels, and visible versions for one Skill."""

    runtime = _runtime(ctx)
    config = load_config(runtime.home)
    try:
        namespace, skill_name = _skill_reference(namespace_or_reference, name)
        with _client(config) as client:
            _echo(client.describe(namespace, skill_name))
    except Exception as exc:
        _fail(exc)


@app.command()
def install(
    ctx: typer.Context,
    namespace_or_reference: str = typer.Argument(..., metavar="NAMESPACE[/NAME]"),
    name: str | None = typer.Argument(None, metavar="NAME"),
    agent: str = typer.Option(..., "--agent"),
    version: str | None = typer.Option(None, "--version"),
    label: str | None = typer.Option(None, "--label"),
    force: bool = typer.Option(False, "--force"),
    root: Annotated[
        Path | None, typer.Option("--root", help="Override the Agent Skill root.")
    ] = None,
) -> None:
    """Download, verify, and atomically install a fixed release."""

    try:
        namespace, skill_name = _skill_reference(namespace_or_reference, name)
        installer, client = _installer(ctx)
        with client:
            result = installer.install(
                namespace,
                skill_name,
                agent=agent,
                version=version,
                label=label,
                force=force,
                root_override=root,
            )
        _echo(result)
    except Exception as exc:
        _fail(exc)


@app.command()
def update(
    ctx: typer.Context,
    namespace_or_reference: str = typer.Argument(..., metavar="NAMESPACE[/NAME]"),
    name: str | None = typer.Argument(None, metavar="NAME"),
    agent: str = typer.Option(..., "--agent"),
    version: str | None = typer.Option(None, "--version"),
    label: str = typer.Option("stable", "--label"),
    force: bool = typer.Option(False, "--force"),
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Resolve a selected release, then update with conflict protection."""

    try:
        namespace, skill_name = _skill_reference(namespace_or_reference, name)
        installer, client = _installer(ctx)
        with client:
            result = installer.update(
                namespace,
                skill_name,
                agent=agent,
                version=version,
                label=label,
                force=force,
                root_override=root,
            )
        _echo(result)
    except Exception as exc:
        _fail(exc)


@app.command()
def status(
    ctx: typer.Context,
    agent: str | None = typer.Option(None, "--agent"),
    namespace: str | None = typer.Option(None, "--namespace"),
    name: str | None = typer.Option(None, "--name"),
) -> None:
    """Compare installed files with the recorded immutable baseline."""

    try:
        installer, client = _local_installer(ctx)
        try:
            _echo({"installations": installer.status(agent=agent, namespace=namespace, name=name)})
        finally:
            client.close()
    except Exception as exc:
        _fail(exc)


@app.command()
def rollback(
    ctx: typer.Context,
    namespace_or_reference: str = typer.Argument(..., metavar="NAMESPACE[/NAME]"),
    name: str | None = typer.Argument(None, metavar="NAME"),
    agent: str = typer.Option(..., "--agent"),
    version: str | None = typer.Option(None, "--version"),
    force: bool = typer.Option(False, "--force"),
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Atomically restore the most recent or selected local backup."""

    try:
        namespace, skill_name = _skill_reference(namespace_or_reference, name)
        installer, client = _local_installer(ctx)
        try:
            _echo(
                installer.rollback(
                    namespace,
                    skill_name,
                    agent=agent,
                    version=version,
                    force=force,
                    root_override=root,
                )
            )
        finally:
            client.close()
    except Exception as exc:
        _fail(exc)


@app.command()
def uninstall(
    ctx: typer.Context,
    namespace_or_reference: str = typer.Argument(..., metavar="NAMESPACE[/NAME]"),
    name: str | None = typer.Argument(None, metavar="NAME"),
    agent: str = typer.Option(..., "--agent"),
    force: bool = typer.Option(False, "--force"),
    root: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Remove a managed installation without deleting local changes silently."""

    try:
        namespace, skill_name = _skill_reference(namespace_or_reference, name)
        installer, client = _local_installer(ctx)
        try:
            _echo(
                installer.uninstall(
                    namespace,
                    skill_name,
                    agent=agent,
                    force=force,
                    root_override=root,
                )
            )
        finally:
            client.close()
    except Exception as exc:
        _fail(exc)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check local adapters, state, and Registry authentication."""

    runtime = _runtime(ctx)
    try:
        config = load_config(runtime.home)
        client = RegistryClient(config.registry_url, token=config.token)
        try:
            installer = SkillInstaller(client, home=runtime.home)
            report = installer.doctor()
            if config.token:
                try:
                    report["registry"] = {"ok": True, "identity": client.me()}
                except Exception as exc:
                    report["registry"] = {"ok": False, "error": str(exc)}
            else:
                report["registry"] = {"ok": False, "error": "token not configured"}
            report["aily"] = {
                "configured": bool(config.aily_app_id and config.aily_access_token),
                "mode": "read-invoke-drift-only",
            }
            _echo(report)
        finally:
            client.close()
    except Exception as exc:
        _fail(exc)


def _aily_connector(ctx: typer.Context) -> AilyCloudConnector:
    runtime = _runtime(ctx)
    config = load_config(runtime.home)
    if not config.aily_app_id or not config.aily_access_token:
        raise SkillHubError(
            "Aily is not configured; use `skillctl config set --aily-app-id ... "
            "--aily-access-token ...`"
        )
    return AilyCloudConnector(
        app_id=config.aily_app_id,
        access_token=config.aily_access_token,
        base_url=config.aily_base_url,
    )


@aily_app.command("list")
def aily_list(
    ctx: typer.Context,
    page_size: int = typer.Option(20, min=1, max=100),
    page_token: str | None = typer.Option(None),
) -> None:
    try:
        with _aily_connector(ctx) as connector:
            _echo(connector.list_skills(page_size=page_size, page_token=page_token))
    except Exception as exc:
        _fail(exc)


@aily_app.command("get")
def aily_get(ctx: typer.Context, skill_id: str = typer.Argument(...)) -> None:
    try:
        with _aily_connector(ctx) as connector:
            _echo(connector.get_skill(skill_id))
    except Exception as exc:
        _fail(exc)


@aily_app.command("start")
def aily_start(
    ctx: typer.Context,
    skill_id: str = typer.Argument(...),
    input_json: str | None = typer.Option(None, "--input-json"),
    global_json: str | None = typer.Option(None, "--global-json"),
) -> None:
    try:
        input_data = json.loads(input_json) if input_json else None
        global_variable = json.loads(global_json) if global_json else None
        if input_data is not None and not isinstance(input_data, dict):
            raise SkillHubError("--input-json must decode to an object")
        if global_variable is not None and not isinstance(global_variable, dict):
            raise SkillHubError("--global-json must decode to an object")
        with _aily_connector(ctx) as connector:
            _echo(
                connector.start_skill(
                    skill_id,
                    input_data=input_data,
                    global_variable=global_variable,
                )
            )
    except Exception as exc:
        _fail(exc)


@aily_app.command("drift")
def aily_drift(
    ctx: typer.Context,
    mappings_file: Annotated[Path, typer.Argument(exists=True, readable=True)],
) -> None:
    """Compare remote read-only fingerprints with a saved Hub-to-Aily mapping file."""

    try:
        raw = json.loads(mappings_file.read_text(encoding="utf-8"))
        mappings = [AilyMapping(**item) for item in raw]
        with _aily_connector(ctx) as connector:
            remote: list[dict[str, Any]] = []
            page_token: str | None = None
            seen_tokens: set[str] = set()
            while True:
                page = connector.list_skills(page_size=100, page_token=page_token)
                remote.extend(page.get("skills") or [])
                if not page.get("has_more"):
                    break
                next_token = page.get("page_token")
                if not next_token or next_token in seen_tokens:
                    raise SkillHubError("Aily pagination did not return a new page_token")
                seen_tokens.add(next_token)
                page_token = next_token
            report = connector.drift_report(mappings, remote)
        _echo({"drift": [item.as_dict() for item in report]})
    except Exception as exc:
        _fail(exc)


if __name__ == "__main__":
    app()
