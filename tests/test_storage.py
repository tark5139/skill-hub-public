from __future__ import annotations

from pathlib import Path

import pytest

from skillhub.storage.local import LocalObjectStorage


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
