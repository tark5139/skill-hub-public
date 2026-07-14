"""Read/invoke-only Feishu Aily cloud connector."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .base import AdapterError


@dataclass(frozen=True)
class AilyMapping:
    hub_ref: str
    skill_id: str
    expected_fingerprint: str | None = None


@dataclass(frozen=True)
class AilyDrift:
    hub_ref: str
    skill_id: str
    status: str
    expected_fingerprint: str | None
    observed_fingerprint: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "hub_ref": self.hub_ref,
            "skill_id": self.skill_id,
            "status": self.status,
            "expected_fingerprint": self.expected_fingerprint,
            "observed_fingerprint": self.observed_fingerprint,
        }


def aily_skill_fingerprint(skill: dict[str, Any]) -> str:
    """Hash the stable, read-only Aily fields used for drift reporting."""

    projection = {
        key: skill.get(key)
        for key in ("id", "label", "description", "input_schema", "output_schema")
    }
    payload = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class AilyCloudConnector:
    """Aily list/get/start client; intentionally exposes no definition CRUD."""

    DEFAULT_BASE_URL = "https://open.feishu.cn/open-apis/aily/v1"

    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not app_id or not access_token:
            raise AdapterError("Aily app_id and access_token are required")
        self.app_id = quote(app_id, safe="")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout)
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> AilyCloudConnector:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, suffix: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}/apps/{self.app_id}{suffix}"
        try:
            response = self.client.request(method, url, headers=self.headers, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(f"Aily request failed: {exc}") from exc
        if payload.get("code", 0) != 0:
            raise AdapterError(
                f"Aily API error {payload.get('code')}: {payload.get('msg', 'unknown error')}"
            )
        return payload.get("data") or {}

    def list_skills(self, *, page_size: int = 20, page_token: str | None = None) -> dict[str, Any]:
        if not 1 <= page_size <= 100:
            raise AdapterError("Aily page_size must be between 1 and 100")
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return self._request("GET", "/skills", params=params)

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/skills/{quote(skill_id, safe='')}")
        return data.get("skill") or data

    def start_skill(
        self,
        skill_id: str,
        *,
        input_data: dict[str, Any] | None = None,
        global_variable: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if input_data is not None:
            body["input"] = json.dumps(input_data, ensure_ascii=False, separators=(",", ":"))
        if global_variable is not None:
            body["global_variable"] = global_variable
        encoded_skill_id = quote(skill_id, safe="")
        return self._request("POST", f"/skills/{encoded_skill_id}/start", json=body)

    @staticmethod
    def drift_report(
        mappings: Iterable[AilyMapping], remote_skills: Iterable[dict[str, Any]]
    ) -> list[AilyDrift]:
        remote = {str(skill.get("id")): skill for skill in remote_skills if skill.get("id")}
        report: list[AilyDrift] = []
        for mapping in mappings:
            observed_skill = remote.get(mapping.skill_id)
            observed = aily_skill_fingerprint(observed_skill) if observed_skill else None
            if observed is None:
                status = "missing"
            elif mapping.expected_fingerprint is None:
                status = "unbaselined"
            elif observed == mapping.expected_fingerprint:
                status = "in_sync"
            else:
                status = "changed"
            report.append(
                AilyDrift(
                    hub_ref=mapping.hub_ref,
                    skill_id=mapping.skill_id,
                    status=status,
                    expected_fingerprint=mapping.expected_fingerprint,
                    observed_fingerprint=observed,
                )
            )
        return report
