"""Preview adapter for Tencent WorkBuddy's guided local package import."""

from __future__ import annotations

import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .base import AdapterError, AdapterHealth, AgentAdapter, validate_skill_name

_PACKAGE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def _validate_package_component(kind: str, value: str) -> str:
    if not _PACKAGE_COMPONENT.fullmatch(value):
        raise AdapterError(f"Unsafe WorkBuddy {kind} component: {value!r}")
    return value


@dataclass(frozen=True)
class WorkBuddyImportResult:
    package_path: Path
    guide_path: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "package_path": str(self.package_path),
            "guide_path": str(self.guide_path),
        }


class WorkBuddyAdapter(AgentAdapter):
    """Generate a reviewed ZIP and instructions; never mutate undocumented internals."""

    adapter_id = "workbuddy"
    display_name = "Tencent WorkBuddy"
    support_level = "preview"
    automatic_install = False

    def doctor(self) -> AdapterHealth:
        return AdapterHealth(
            adapter=self.adapter_id,
            support_level=self.support_level,
            root=None,
            root_exists=False,
            writable=True,
            executable=None,
            executable_found=None,
            notes=(
                "官方公开文档仅确认本地技能包导入，未承诺稳定的批量安装 CLI/API。",
                "Skill Hub 只生成已校验的导入包与操作指引。",
            ),
        )

    def prepare_import(
        self,
        source_dir: Path,
        *,
        namespace: str,
        name: str,
        version: str,
        output_dir: Path,
    ) -> WorkBuddyImportResult:
        name = validate_skill_name(name)
        namespace = _validate_package_component("namespace", namespace)
        version = _validate_package_component("version", version)
        source_dir = source_dir.resolve()
        if not (source_dir / "SKILL.md").is_file():
            raise AdapterError("WorkBuddy import package must contain SKILL.md at its root")

        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{namespace}-{name}-{version}"
        package_path = output_dir / f"{stem}.zip"
        guide_path = output_dir / f"{stem}-IMPORT.txt"
        nonce = uuid.uuid4().hex
        package_temp = output_dir / f".{stem}.{nonce}.zip.tmp"
        guide_temp = output_dir / f".{stem}.{nonce}.txt.tmp"

        guide = f"""Tencent WorkBuddy 导入指引（Preview）

技能：{namespace}/{name}@{version}
导入包：{package_path.name}

1. 打开 WorkBuddy，进入“技能”→“已安装”。
2. 选择“添加技能”或“导入本地技能包”。
3. 选择同目录下的 {package_path.name}。
4. 导入前核对技能名称、版本、权限和脚本；导入后执行一次只读验收任务。

说明：Skill Hub 未写入 WorkBuddy 的未公开内部目录，因此不能自动确认安装状态、回滚或卸载。
"""
        try:
            with zipfile.ZipFile(package_temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(source_dir.rglob("*")):
                    if path.is_symlink():
                        raise AdapterError(f"WorkBuddy package refuses symlink: {path}")
                    if path.is_file():
                        archive.write(path, path.relative_to(source_dir).as_posix())
            guide_temp.write_text(guide, encoding="utf-8")
            os.replace(guide_temp, guide_path)
            os.replace(package_temp, package_path)
        finally:
            package_temp.unlink(missing_ok=True)
            guide_temp.unlink(missing_ok=True)
        return WorkBuddyImportResult(package_path=package_path, guide_path=guide_path)
