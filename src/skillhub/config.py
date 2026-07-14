from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Environment variables use the ``SKILLHUB_`` prefix. Secrets are intentionally absent from
    log-friendly representations.
    """

    model_config = SettingsConfigDict(
        env_prefix="SKILLHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./var/skillhub.db"
    admin_token: str = Field(default="development-token-change-me-32chars", repr=False)
    admin_subject: str = "tark5139"
    public_base_url: str = "http://localhost:8080"

    storage_backend: Literal["local", "s3"] = "local"
    local_storage_path: Path = Path("./var/storage")
    s3_endpoint_url: str | None = None
    s3_region: str = "ap-shanghai"
    s3_quarantine_bucket: str = "skill-hub-quarantine"
    s3_release_bucket: str = "skill-hub-release"
    s3_access_key_id: str | None = Field(default=None, repr=False)
    s3_secret_access_key: str | None = Field(default=None, repr=False)

    max_archive_bytes: int = 50 * 1024 * 1024
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_files: int = 2_000
    max_compression_ratio: float = 100.0
    require_signature: bool = False
    trusted_public_keys_json: dict[str, str] = Field(default_factory=dict, repr=False)
    worker_poll_seconds: float = 2.0
    worker_scan_lease_seconds: int = 300
    worker_max_scan_attempts: int = 3

    github_owner: str = "tark5139"
    github_repository: str = "skill-hub-public"
    github_token: str | None = Field(default=None, repr=False)
    github_approver: str = "tark5139"

    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("public_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("max_archive_bytes", "max_uncompressed_bytes", "max_files")
    @classmethod
    def positive_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("archive limits must be positive")
        return value

    @field_validator("worker_poll_seconds")
    @classmethod
    def positive_poll_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        return value

    @model_validator(mode="after")
    def validate_production(self) -> Settings:
        if self.env == "production":
            if self.database_url.startswith("sqlite"):
                raise ValueError("production requires PostgreSQL")
            if self.storage_backend != "s3":
                raise ValueError("production requires S3/COS storage")
            if len(self.admin_token) < 32 or self.admin_token.startswith("development-"):
                raise ValueError("production admin token must be at least 32 random characters")
            if not self.require_signature:
                raise ValueError("production requires trusted signature verification")
        if self.storage_backend == "s3" and not (
            self.s3_access_key_id and self.s3_secret_access_key
        ):
            raise ValueError("S3/COS credentials are required when storage_backend=s3")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
