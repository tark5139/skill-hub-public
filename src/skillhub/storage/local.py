from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

from .base import ObjectStorage


class LocalObjectStorage(ObjectStorage):
    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / "quarantine").mkdir(parents=True, exist_ok=True)
        (self.root / "release").mkdir(parents=True, exist_ok=True)

    def _path(self, zone: str, key: str) -> Path:
        posix = PurePosixPath(key)
        if posix.is_absolute() or ".." in posix.parts or not posix.parts:
            raise ValueError("invalid object key")
        path = (self.root / zone / Path(*posix.parts)).resolve()
        if self.root not in path.parents:
            raise ValueError("object key escaped storage root")
        return path

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    @staticmethod
    def _create_once(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest():
                raise FileExistsError(
                    f"immutable object already exists with different data: {path}"
                ) from exc
            return
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def put_quarantine(self, key: str, data: bytes) -> None:
        self._create_once(self._path("quarantine", key), data)

    def get_quarantine(self, key: str) -> bytes:
        return self._path("quarantine", key).read_bytes()

    def delete_quarantine(self, key: str) -> None:
        self._path("quarantine", key).unlink(missing_ok=True)

    def put_release(self, key: str, data: bytes, *, content_type: str) -> None:
        del content_type
        self._create_once(self._path("release", key), data)

    def get_release(self, key: str) -> bytes:
        return self._path("release", key).read_bytes()

    def release_exists(self, key: str) -> bool:
        return self._path("release", key).is_file()

    def presign_release(self, key: str, *, expires_seconds: int = 300) -> str | None:
        del key, expires_seconds
        return None
