#!/usr/bin/env python3
"""Fail-closed macOS fast path for publishing one immutable Skill version.

The helper deliberately keeps the administrator token and signing-key passphrase in
memory only. GitHub export remains a separate, per-version authorization and is off
unless the caller supplies both an explicit flag and the exact confirmation phrase.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from skillhub.security.models import ScanPolicy
from skillhub.security.scanner import scan_skill_archive
from skillhub.security.signatures import ed25519_signature_payload
from skillhub_cli.registry import RegistryClient

DEFAULT_REGISTRY_URL = "http://127.0.0.1:18080"
DEFAULT_SSH_HOST = "ubuntu@122.51.39.172"
DEFAULT_SSH_KEY = Path("~/.ssh/skill-hub-mvp").expanduser()
DEFAULT_SIGNING_KEY = Path(
    "~/.config/skill-hub/keys/skill-package-ed25519.pem"
).expanduser()
DEFAULT_SIGNING_KEY_ID = "tark5139:personal-1"
DEFAULT_COMPATIBILITY = (
    "codex",
    "claude-code",
    "trae-cn",
    "openclaw",
    "hermes",
    "workbuddy",
)


class PublishError(RuntimeError):
    """Expected, non-secret-bearing publication failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(document: dict[str, Any]) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _frontmatter(archive: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(archive) as package:
            members = [item.filename for item in package.infolist() if not item.is_dir()]
            if "SKILL.md" not in members:
                raise PublishError("ZIP 根目录必须直接包含 SKILL.md；不接受外层包装目录")
            license_names = {"license", "license.md", "license.txt"}
            if not any(name.casefold() in license_names for name in members):
                raise PublishError("ZIP 根目录必须包含完整 LICENSE 文件")
            document = package.read("SKILL.md").decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise PublishError("无法读取 Skill ZIP") from exc
    if not document.startswith("---\n"):
        raise PublishError("SKILL.md 缺少 YAML frontmatter")
    try:
        _, raw, _body = document.split("---", 2)
        parsed = yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError) as exc:
        raise PublishError("SKILL.md frontmatter 无效") from exc
    if not isinstance(parsed, dict):
        raise PublishError("SKILL.md frontmatter 必须为对象")
    return parsed


def validate_archive(
    archive: Path,
    *,
    namespace: str,
    name: str,
    version: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    if not archive.is_file() or archive.is_symlink():
        raise PublishError(f"Skill ZIP 不存在或不是普通文件：{archive}")
    metadata = _frontmatter(archive)
    if metadata.get("name") != name:
        raise PublishError(
            f"Skill 名称不一致：frontmatter={metadata.get('name')!r}, requested={name!r}"
        )
    license_id = metadata.get("license")
    if not isinstance(license_id, str) or not license_id.strip():
        raise PublishError("SKILL.md frontmatter 必须声明 license")
    result = scan_skill_archive(
        archive,
        namespace=namespace,
        name=name,
        version=version,
        policy=ScanPolicy(require_license=True, require_signature=False),
    )
    issues = [
        {
            "code": item.code,
            "severity": item.severity,
            "path": item.path,
            "message": item.message,
        }
        for item in result.issues
    ]
    if not result.passed:
        codes = ", ".join(item["code"] for item in issues if item["severity"] == "error")
        raise PublishError(f"本地安全扫描未通过：{codes or 'unknown'}")
    return sha256_file(archive), license_id.strip(), issues


def fetch_admin_token(ssh_host: str, ssh_key: Path) -> str:
    if not ssh_key.is_file():
        raise PublishError(f"SSH 私钥不存在：{ssh_key}")
    command = [
        "/usr/bin/ssh",
        "-i",
        str(ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        ssh_host,
        "sudo -n -u skillhub sed -n 's/^SKILLHUB_ADMIN_TOKEN=//p' /opt/skill-hub/.env",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode != 0:
        raise PublishError("无法通过 SSH 安全读取服务器管理员令牌")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) < 32:
        raise PublishError("服务器管理员令牌不存在或格式异常")
    return lines[0]


def prompt_signing_passphrase() -> str:
    if sys.platform == "darwin" and Path("/usr/bin/osascript").exists():
        script = (
            'text returned of (display dialog '
            '"请输入 Skill 包签名私钥密码。密码仅在本机内存中使用。" '
            'default answer "" with hidden answer buttons {"取消", "继续"} '
            'default button "继续" cancel button "取消" with title "Skill Hub 安全签名")'
        )
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PublishError("已取消 Skill 包签名")
        password = result.stdout.rstrip("\r\n")
    else:
        password = getpass.getpass("Skill-package signing-key passphrase: ")
    if not password:
        raise PublishError("签名私钥密码不能为空")
    return password


def sign_digest(signing_key: Path, password: str, digest: str) -> str:
    if not signing_key.is_file() or signing_key.is_symlink():
        raise PublishError(f"签名私钥不存在或类型异常：{signing_key}")
    try:
        key = serialization.load_pem_private_key(
            signing_key.read_bytes(),
            password=password.encode(),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise PublishError("无法解锁签名私钥；请检查密码") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PublishError("签名私钥不是 Ed25519")
    signature = key.sign(ed25519_signature_payload(digest))
    return base64.b64encode(signature).decode("ascii")


class HubAPI:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=45,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> HubAPI:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:240]
        if not isinstance(body, dict):
            return ""
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        return str(body.get("title") or body.get("message") or body.get("type") or "")

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                f"/api/v1/{path.lstrip('/')}",
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise PublishError(f"Skill Hub 请求失败：{type(exc).__name__}") from exc
        if response.status_code not in expected:
            detail = self._detail(response)
            suffix = f"：{detail}" if detail else ""
            endpoint = f"/api/v1/{path.lstrip('/')}"
            raise PublishError(
                f"Skill Hub {method.upper()} {endpoint} 返回 HTTP "
                f"{response.status_code}{suffix}"
            )
        return response

    def json(self, method: str, path: str, *, expected: set[int], **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, path, expected=expected, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublishError("Skill Hub 返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise PublishError("Skill Hub JSON 响应格式异常")
        return payload


def _existing_skill(api: HubAPI, namespace: str, name: str) -> dict[str, Any] | None:
    response = api.client.get(f"/api/v1/skills/{namespace}/{name}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        detail = api._detail(response)
        raise PublishError(f"无法查询 Skill：HTTP {response.status_code} {detail}".strip())
    payload = response.json()
    if not isinstance(payload, dict):
        raise PublishError("Skill 查询响应格式异常")
    return payload


def _wait_for_scan(api: HubAPI, version_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        version = api.json("GET", f"/versions/{version_id}", expected={200})
        status = version.get("status")
        if status == "draft":
            return version
        if status == "scan_failed":
            raise PublishError("服务器安全扫描失败；该版本号已占用，请检查服务器审计记录")
        if status not in {"pending_scan", "scanning"}:
            raise PublishError(f"服务器扫描进入异常状态：{status}")
        time.sleep(2)
    raise PublishError(f"等待服务器扫描超时（{timeout_seconds} 秒）")


def _verify_distribution(
    *,
    registry_url: str,
    token: str,
    namespace: str,
    name: str,
    version: str,
    expected_digest: str,
    label: str,
) -> dict[str, Any]:
    with RegistryClient(registry_url, token=token) as client:
        by_version = client.resolve(namespace, name, version=version)
        by_label = client.resolve(namespace, name, label=label)
        if by_version["artifact_sha256"] != expected_digest:
            raise PublishError("版本解析返回的制品摘要不一致")
        if by_label["artifact_sha256"] != expected_digest:
            raise PublishError("稳定标签未指向刚发布的制品")
        if by_version["signature_status"] != "verified":
            raise PublishError("发布制品的签名状态不是 verified")
        manifest = client.manifest(by_version["manifest_url"])
        if canonical_json_sha256(manifest) != by_version["manifest_sha256"]:
            raise PublishError("下载清单摘要校验失败")
        with tempfile.TemporaryDirectory(prefix="skillhub-verify-") as temporary:
            downloaded = Path(temporary) / "artifact.zip"
            client.download(by_version["artifact_url"], downloaded)
            if sha256_file(downloaded) != expected_digest:
                raise PublishError("回读制品摘要校验失败")
    return {
        "version": by_version["resolved_version"],
        "label": label,
        "artifact_sha256": by_version["artifact_sha256"],
        "manifest_sha256": by_version["manifest_sha256"],
        "signature_status": by_version["signature_status"],
    }


def publish(args: argparse.Namespace) -> dict[str, Any]:
    archive = args.archive.expanduser().resolve()
    digest, license_id, local_issues = validate_archive(
        archive,
        namespace=args.namespace,
        name=args.name,
        version=args.version,
    )
    token = fetch_admin_token(args.ssh_host, args.ssh_key.expanduser())
    created = False
    with HubAPI(args.registry_url, token) as api:
        identity = api.json("GET", "/me", expected={200})
        capabilities = api.json("GET", "/system/capabilities", expected={200})
        if capabilities.get("require_signature") is not True:
            raise PublishError("生产 Skill Hub 未启用强制签名，拒绝继续")

        skill = _existing_skill(api, args.namespace, args.name)
        if skill is None:
            skill = api.json(
                "POST",
                "/skills",
                expected={201},
                headers={"Idempotency-Key": f"quick-upload-{args.namespace}-{args.name}"},
                json={
                    "namespace": args.namespace,
                    "name": args.name,
                    "description": args.description,
                    "visibility": args.visibility,
                    "tags": args.tags,
                },
            )
            created = True
        elif args.visibility == "public" and skill.get("visibility") != "public":
            raise PublishError("现有 Skill 不是 public，当前 API 无法原地修改可见性")

        versions = skill.get("versions", [])
        matching = [item for item in versions if item.get("version") == args.version]
        if matching:
            current = matching[0]
            if current.get("status") == "published" and current.get("sha256") == digest:
                verified = _verify_distribution(
                    registry_url=args.registry_url,
                    token=token,
                    namespace=args.namespace,
                    name=args.name,
                    version=args.version,
                    expected_digest=digest,
                    label=args.label,
                )
                return {
                    "result": "already_published",
                    "skill_id": skill["id"],
                    "version_id": current["id"],
                    "archive": str(archive),
                    "local_scan_issues": local_issues,
                    "verified": verified,
                    "github": "not_requested",
                }
            raise PublishError(
                f"版本 {args.version} 已存在且不可覆盖：status={current.get('status')}"
            )

        password = prompt_signing_passphrase()
        try:
            signature = sign_digest(args.signing_key.expanduser(), password, digest)
        finally:
            password = ""

        upload = api.json(
            "POST",
            f"/skills/{skill['id']}/uploads",
            expected={201},
            json={
                "version": args.version,
                "expected_sha256": digest,
                "license": license_id,
                "compatibility": args.compatibility,
                "signature_key_id": args.signing_key_id,
                "signature_base64": signature,
            },
        )
        content = archive.read_bytes()
        stored = api.json(
            "PUT",
            f"/uploads/{upload['id']}/content",
            expected={200},
            content=content,
            headers={"Content-Type": "application/zip"},
        )
        if stored.get("sha256") != digest:
            raise PublishError("服务器接收摘要与本地摘要不一致")
        version = api.json(
            "POST",
            f"/uploads/{upload['id']}:finalize",
            expected={202},
        )
        scanned = _wait_for_scan(api, version["id"], args.scan_timeout)
        if scanned.get("signature_status") != "verified":
            raise PublishError("服务器未验证 Ed25519 签名")
        submitted = api.json(
            "POST",
            f"/versions/{version['id']}:submit",
            expected={200},
        )
        approved = api.json(
            "POST",
            f"/versions/{version['id']}:approve",
            expected={200},
            json={
                "decision": "approved",
                "evidence": {
                    "source": "macos-quick-upload-v1",
                    "artifact_sha256": digest,
                    "local_scan": "passed",
                    "license": license_id,
                    "requested_by": identity.get("subject"),
                },
            },
        )
        published = api.json(
            "POST",
            f"/versions/{version['id']}:publish",
            expected={200},
            json={"label": args.label},
        )
        github: dict[str, Any] | str = "not_requested"
        if args.authorize_github:
            if args.github_confirmation != "PUBLISH_PUBLICLY":
                raise PublishError("GitHub 发布必须提供精确确认词 PUBLISH_PUBLICLY")
            github = api.json(
                "POST",
                f"/versions/{version['id']}/github-authorization",
                expected={202},
                json={
                    "confirmation": "PUBLISH_PUBLICLY",
                    "license_confirmed": True,
                    "sensitive_content_reviewed": True,
                },
            )

    verified = _verify_distribution(
        registry_url=args.registry_url,
        token=token,
        namespace=args.namespace,
        name=args.name,
        version=args.version,
        expected_digest=digest,
        label=args.label,
    )
    return {
        "result": "published",
        "created_skill": created,
        "skill_id": skill["id"],
        "version_id": version["id"],
        "archive": str(archive),
        "local_scan_issues": local_issues,
        "lifecycle": {
            "scan": scanned.get("scan_status"),
            "submit": submitted.get("status"),
            "approval": approved.get("status"),
            "publish": published.get("status"),
        },
        "verified": verified,
        "github": github,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Root-layout .skill.zip archive")
    parser.add_argument("--namespace", default="personal")
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--visibility", choices=("private", "public"), default="public")
    parser.add_argument("--label", default="stable")
    parser.add_argument("--tag", dest="tags", action="append", default=[])
    parser.add_argument(
        "--compatibility",
        action="append",
        default=list(DEFAULT_COMPATIBILITY),
        help="Repeat for each supported Agent",
    )
    parser.add_argument("--registry-url", default=DEFAULT_REGISTRY_URL)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--signing-key", type=Path, default=DEFAULT_SIGNING_KEY)
    parser.add_argument("--signing-key-id", default=DEFAULT_SIGNING_KEY_ID)
    parser.add_argument("--scan-timeout", type=int, default=180)
    parser.add_argument("--authorize-github", action="store_true")
    parser.add_argument("--github-confirmation")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = publish(args)
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.report.with_suffix(f"{args.report.suffix}.tmp")
            temporary.write_text(f"{rendered}\n", encoding="utf-8")
            os.replace(temporary, args.report)
        print(rendered)
        return 0
    except PublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
