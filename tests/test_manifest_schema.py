from __future__ import annotations

import json
import re
from pathlib import Path

from skillhub.schemas import SEMVER_PATTERN


def test_committed_manifest_schema_matches_runtime_semver_contract() -> None:
    project_root = Path(__file__).resolve().parents[1]
    schema = json.loads((project_root / "docs/api/skill-manifest.schema.json").read_text())
    schema_pattern = re.compile(schema["properties"]["version"]["pattern"])
    examples = {
        "0.1.0": True,
        "1.2.3-alpha.1+macos.arm64": True,
        "01.2.3": False,
        "1.2": False,
        "1.2.3+": False,
    }
    for value, expected in examples.items():
        assert bool(schema_pattern.fullmatch(value)) is expected
        assert bool(SEMVER_PATTERN.fullmatch(value)) is expected


def test_manifest_schema_is_closed_and_bounds_agent_compatibility() -> None:
    project_root = Path(__file__).resolve().parents[1]
    schema = json.loads((project_root / "docs/api/skill-manifest.schema.json").read_text())
    assert schema["additionalProperties"] is False
    compatibility = schema["properties"]["compatibility"]
    assert compatibility["uniqueItems"] is True
    assert compatibility["maxItems"] == 32
