from __future__ import annotations

from ..config import Settings
from .base import ObjectStorage
from .local import LocalObjectStorage
from .s3 import S3ObjectStorage


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "local":
        return LocalObjectStorage(settings.local_storage_path)
    assert settings.s3_access_key_id is not None
    assert settings.s3_secret_access_key is not None
    return S3ObjectStorage(
        quarantine_bucket=settings.s3_quarantine_bucket,
        release_bucket=settings.s3_release_bucket,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
