from __future__ import annotations

from abc import ABC, abstractmethod


class ObjectStorage(ABC):
    """Two-zone object store: untrusted quarantine and immutable release."""

    @abstractmethod
    def put_quarantine(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_quarantine(self, key: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def delete_quarantine(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def put_release(self, key: str, data: bytes, *, content_type: str) -> None:
        """Create an immutable release object; fail if the key already has different data."""

    @abstractmethod
    def get_release(self, key: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def release_exists(self, key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def presign_release(self, key: str, *, expires_seconds: int = 300) -> str | None:
        """Return a signed URL, or None when the API should stream the object itself."""
