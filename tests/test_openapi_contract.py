from __future__ import annotations

import json
from pathlib import Path


def test_committed_openapi_matches_application(app) -> None:
    project_root = Path(__file__).resolve().parents[1]
    committed = json.loads((project_root / "docs" / "api" / "openapi.json").read_text())
    assert app.openapi() == committed


def test_resolve_contract_keeps_version_and_label_mutually_exclusive(app) -> None:
    document = app.openapi()
    operation = document["paths"]["/api/v1/skills/{namespace}/{name}/resolve"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert {"version", "label"}.issubset(parameters)
    schema = document["components"]["schemas"]["ResolveResponse"]
    assert {
        "artifact_sha256",
        "manifest_sha256",
        "artifact_url",
        "manifest_url",
        "signature_status",
        "etag",
    }.issubset(schema["required"])
