from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from skillhub.storage.local import LocalObjectStorage
from skillhub.storage.s3 import S3ObjectStorage


def test_local_storage_is_immutable_and_zone_separated(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    storage.put_quarantine("personal/u/source.zip", b"untrusted")
    assert storage.get_quarantine("personal/u/source.zip") == b"untrusted"

    storage.put_release("artifacts/sha256/aa/value.zip", b"first", content_type="application/zip")
    storage.put_release("artifacts/sha256/aa/value.zip", b"first", content_type="application/zip")
    assert storage.get_release("artifacts/sha256/aa/value.zip") == b"first"
    with pytest.raises(FileExistsError):
        storage.put_release(
            "artifacts/sha256/aa/value.zip", b"different", content_type="application/zip"
        )


@pytest.mark.parametrize("key", ["../escape", "/absolute", "ok/../../escape"])
def test_local_storage_rejects_escaping_keys(tmp_path: Path, key: str) -> None:
    storage = LocalObjectStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.put_quarantine(key, b"bad")


def test_s3_storage_uses_virtual_host_addressing_for_cos() -> None:
    storage = S3ObjectStorage(
        quarantine_bucket="skill-hub-quarantine-1234567890",
        release_bucket="skill-hub-release-1234567890",
        region="ap-shanghai",
        endpoint_url="https://cos.ap-shanghai.myqcloud.com",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
    )

    url = storage.presign_release("artifacts/example.skill.zip")

    assert url is not None
    parsed = urlparse(url)
    assert parsed.hostname == "skill-hub-release-1234567890.cos.ap-shanghai.myqcloud.com"
    assert parsed.path == "/artifacts/example.skill.zip"
