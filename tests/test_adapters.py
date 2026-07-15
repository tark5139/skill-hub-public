from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx

from skillhub.adapters import (
    AilyCloudConnector,
    AilyMapping,
    WorkBuddyAdapter,
    aily_skill_fingerprint,
    get_adapter,
)


def test_local_adapter_roots_are_home_injectable(tmp_path: Path) -> None:
    expected = {
        "codex": tmp_path / ".codex/skills",
        "claude-code": tmp_path / ".claude/skills",
        "trae-cn": tmp_path / ".trae-cn/skills",
        "openclaw": tmp_path / ".openclaw/skills",
        "hermes": tmp_path / ".hermes/skills",
    }
    for adapter_id, root in expected.items():
        adapter = get_adapter(adapter_id, home=tmp_path)
        assert adapter.root == root
        assert adapter.destination("review-docs") == root / "review-docs"
    assert get_adapter("claude", home=tmp_path).adapter_id == "claude-code"
    assert get_adapter("trae", home=tmp_path).adapter_id == "trae-cn"


def test_workbuddy_preview_builds_rooted_package_and_guide(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n", encoding="utf-8")
    (source / "references").mkdir()
    (source / "references/info.md").write_text("hello", encoding="utf-8")

    adapter = WorkBuddyAdapter(home=tmp_path)
    result = adapter.prepare_import(
        source,
        namespace="personal",
        name="demo",
        version="1.0.0",
        output_dir=tmp_path / "exports",
    )

    with zipfile.ZipFile(result.package_path) as archive:
        assert archive.namelist() == ["SKILL.md", "references/info.md"]
    guide = result.guide_path.read_text(encoding="utf-8")
    assert "Preview" in guide
    assert "导入本地技能包" in guide
    assert "不能自动确认安装状态" in guide


def test_aily_connector_is_read_invoke_only_and_reports_drift() -> None:
    requests: list[httpx.Request] = []
    remote_skill = {
        "id": "skill_1",
        "label": "Review",
        "description": "Review docs",
        "input_schema": "[]",
        "output_schema": "[]",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer token"
        if request.method == "GET" and request.url.path.endswith("/skills"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"skills": [remote_skill], "has_more": False}},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"skill": remote_skill}})
        body = json.loads(request.content)
        assert json.loads(body["input"]) == {"text": "hello"}
        assert body["global_variable"] == {"query": "hello"}
        return httpx.Response(
            200,
            json={"code": 0, "data": {"status": "success", "output": "{}"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = AilyCloudConnector(
        app_id="app_1",
        access_token="token",
        base_url="https://open.feishu.cn/open-apis/aily/v1",
        client=client,
    )
    assert connector.list_skills()["skills"][0]["id"] == "skill_1"
    assert connector.get_skill("skill_1")["label"] == "Review"
    assert (
        connector.start_skill(
            "skill_1", input_data={"text": "hello"}, global_variable={"query": "hello"}
        )["status"]
        == "success"
    )

    fingerprint = aily_skill_fingerprint(remote_skill)
    report = connector.drift_report(
        [
            AilyMapping("personal/review@1.0.0", "skill_1", fingerprint),
            AilyMapping("personal/missing@1.0.0", "skill_missing", None),
        ],
        [remote_skill],
    )
    assert [item.status for item in report] == ["in_sync", "missing"]
    assert all("/apps/app_1/skills" in request.url.path for request in requests)
    client.close()
