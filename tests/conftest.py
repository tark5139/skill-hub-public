from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillhub.config import Settings
from skillhub.main import create_app

ADMIN_TOKEN = "test-admin-token-which-is-long-enough"


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'skillhub.db'}",
        admin_token=ADMIN_TOKEN,
        admin_subject="tark5139",
        local_storage_path=tmp_path / "storage",
        public_base_url="https://hub.test",
    )
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as value:
        yield value


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def build_skill_zip(name: str = "hello-skill") -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: A safe test skill.\n---\n\n# Instructions\nHello.\n",
        )
        archive.writestr("LICENSE", "Apache License 2.0\n")
    return target.getvalue()
