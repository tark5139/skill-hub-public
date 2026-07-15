from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_image_build_is_pinned_cached_and_offline_for_local_package() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    first_line = dockerfile.splitlines()[0]
    assert first_line.startswith("FROM python:3.12-slim-bookworm@sha256:")
    assert len(first_line.rsplit("@sha256:", 1)[1]) == 64
    assert "apt-get" not in dockerfile
    assert "already contains the system CA bundle" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/pip" in dockerfile
    assert "--retries 8 --timeout 60 --only-binary=:all:" in dockerfile
    assert dockerfile.index("COPY constraints.txt requirements-image.txt") < dockerfile.index(
        "COPY pyproject.toml"
    )
    assert "python -m pip install --no-deps --no-build-isolation ." in dockerfile


def test_deploy_and_rollback_derive_an_immutable_image_tag_from_signed_tag() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "image: personal-skill-hub:${SKILLHUB_IMAGE_TAG:-dev}" in compose

    for relative in ("ops/tencent/deploy.sh", "ops/tencent/rollback.sh"):
        script = (ROOT / relative).read_text(encoding="utf-8")
        assert "SKILLHUB_IMAGE_TAG=${BASH_REMATCH[1]}" in script
        assert "export SKILLHUB_IMAGE_TAG" in script
        assert "vMAJOR.MINOR.PATCH" in script
