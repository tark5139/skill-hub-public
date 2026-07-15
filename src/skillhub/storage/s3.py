from __future__ import annotations

import hashlib
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .base import ObjectStorage


class S3ObjectStorage(ObjectStorage):
    def __init__(
        self,
        *,
        quarantine_bucket: str,
        release_bucket: str,
        region: str,
        endpoint_url: str | None,
        access_key_id: str,
        secret_access_key: str,
        client: Any | None = None,
    ):
        self.quarantine_bucket = quarantine_bucket
        self.release_bucket = release_bucket
        self.client = client or boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(s3={"addressing_style": "virtual"}),
        )

    def put_quarantine(self, key: str, data: bytes) -> None:
        self._put_once(self.quarantine_bucket, key, data, "application/zip")

    def get_quarantine(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.quarantine_bucket, Key=key)["Body"].read()

    def delete_quarantine(self, key: str) -> None:
        self.client.delete_object(Bucket=self.quarantine_bucket, Key=key)

    def put_release(self, key: str, data: bytes, *, content_type: str) -> None:
        self._put_once(self.release_bucket, key, data, content_type)

    def _put_once(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": digest, "immutable": "true"},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
            }:
                raise
            current = self.client.head_object(Bucket=bucket, Key=key)
            if current.get("Metadata", {}).get("sha256") != digest:
                raise FileExistsError(
                    f"immutable object already exists with different data: {key}"
                ) from exc

    def get_release(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.release_bucket, Key=key)["Body"].read()

    def release_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.release_bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def presign_release(self, key: str, *, expires_seconds: int = 300) -> str | None:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.release_bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )
