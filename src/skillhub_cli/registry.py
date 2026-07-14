"""HTTP client for the stable Skill Hub Registry contract."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from .errors import RegistryError

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class RegistryClient:
    """Small synchronous Registry client used by the macOS CLI."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_download_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        base_url = base_url.rstrip("/")
        parsed_base = urlsplit(base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
            raise RegistryError("Registry URL must be an absolute HTTP(S) URL")
        if parsed_base.username is not None or parsed_base.password is not None:
            raise RegistryError("Registry URL must not contain credentials")
        if parsed_base.query or parsed_base.fragment:
            raise RegistryError("Registry URL must not contain a query or fragment")
        if parsed_base.scheme == "http" and not self._is_loopback(parsed_base.hostname):
            raise RegistryError("Registry URL must use HTTPS unless it targets loopback")
        if not parsed_base.path.rstrip("/").endswith("/api/v1"):
            base_url = f"{base_url}/api/v1"
            parsed_base = urlsplit(base_url)
        self.base_url = base_url
        self._registry_origin = (parsed_base.scheme.lower(), parsed_base.netloc.lower())
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.max_download_bytes = max_download_bytes
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _url(self, path_or_url: str) -> str:
        parsed = urlsplit(path_or_url)
        if parsed.scheme:
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RegistryError("Registry resource URL must use HTTP(S)")
            if parsed.username is not None or parsed.password is not None:
                raise RegistryError("Registry resource URL must not contain credentials")
            if parsed.scheme == "http" and not self._is_loopback(parsed.hostname):
                raise RegistryError(
                    "Registry resource URL must use HTTPS unless it targets loopback"
                )
            return path_or_url
        if path_or_url.startswith("/"):
            base = urlsplit(self.base_url)
            return f"{base.scheme}://{base.netloc}{path_or_url}"
        return urljoin(f"{self.base_url}/", path_or_url)

    def _headers_for(self, url: str) -> dict[str, str]:
        """Never forward a Registry bearer token to COS/S3 or another origin."""

        parsed = urlsplit(url)
        headers = {"Accept": self.headers["Accept"]}
        if (parsed.scheme.lower(), parsed.netloc.lower()) == self._registry_origin:
            authorization = self.headers.get("Authorization")
            if authorization:
                headers["Authorization"] = authorization
        return headers

    @staticmethod
    def _origin(url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        return parsed.scheme.lower(), parsed.netloc.lower()

    @staticmethod
    def _is_loopback(hostname: str | None) -> bool:
        if not hostname:
            return False
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _redirect_target(self, current_url: str, response: httpx.Response) -> str:
        location = response.headers.get("location")
        if not location or any(ord(char) < 32 for char in location):
            raise RegistryError("Registry redirect has a missing or invalid Location header")
        target, _fragment = urldefrag(urljoin(current_url, location))
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RegistryError("Registry redirect target must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise RegistryError("Registry redirect target must not contain credentials")

        if parsed.scheme == "https":
            return target
        current = urlsplit(current_url)
        local_http_same_origin = (
            current.scheme == "http"
            and self._origin(current_url) == self._origin(target)
            and self._is_loopback(parsed.hostname)
        )
        if not local_http_same_origin:
            raise RegistryError(
                "Registry refused a non-HTTPS redirect; only same-origin loopback HTTP "
                "is allowed for local testing"
            )
        return target

    def _request(self, method: str, path_or_url: str, **kwargs: Any) -> httpx.Response:
        url = self._url(path_or_url)
        visited = {url}
        redirects = 0
        request_kwargs = dict(kwargs)
        while True:
            try:
                response = self.client.request(
                    method,
                    url,
                    headers=self._headers_for(url),
                    follow_redirects=False,
                    **request_kwargs,
                )
            except httpx.HTTPError as exc:
                raise RegistryError(f"Registry request failed: {exc}") from exc
            if response.status_code not in _REDIRECT_STATUSES:
                break
            if method.upper() not in {"GET", "HEAD"}:
                response.close()
                raise RegistryError("Registry refused to redirect a non-read request")
            if redirects >= _MAX_REDIRECTS:
                response.close()
                raise RegistryError(f"Registry redirect limit exceeded ({_MAX_REDIRECTS})")
            try:
                target = self._redirect_target(url, response)
            finally:
                response.close()
            if target in visited:
                raise RegistryError("Registry redirect cycle detected")
            visited.add(target)
            redirects += 1
            url = target
            # Query selectors belong to the original request. A Location URL is
            # authoritative and must not inherit those parameters.
            request_kwargs.pop("params", None)
        if not 200 <= response.status_code < 300:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("detail") or body.get("title") or body.get("message") or "")
            except (ValueError, AttributeError):
                detail = response.text[:300]
            suffix = f": {detail}" if detail else ""
            raise RegistryError(f"Registry returned HTTP {response.status_code}{suffix}")
        return response

    def _json(self, method: str, path_or_url: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path_or_url, **kwargs)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RegistryError("Registry returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RegistryError("Registry JSON response must be an object")
        return payload

    def me(self) -> dict[str, Any]:
        return self._json("GET", "me")

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        compatibility: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if compatibility:
            params["compatibility"] = compatibility
        return self._json("GET", "skills", params=params)

    def describe(self, namespace: str, name: str) -> dict[str, Any]:
        return self._json("GET", f"skills/{namespace}/{name}")

    def resolve(
        self,
        namespace: str,
        name: str,
        *,
        version: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        if version and label:
            raise RegistryError("version and label are mutually exclusive")
        params: dict[str, str] = {}
        if version:
            params["version"] = version
        else:
            params["label"] = label or "stable"
        payload = self._json("GET", f"skills/{namespace}/{name}/resolve", params=params)
        required = {
            "resolved_version",
            "artifact_sha256",
            "manifest_sha256",
            "artifact_url",
            "manifest_url",
            "signature_status",
            "etag",
        }
        missing = required - payload.keys()
        if missing:
            raise RegistryError(f"Resolve response missing fields: {', '.join(sorted(missing))}")
        for field in ("artifact_sha256", "manifest_sha256"):
            digest = payload[field]
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise RegistryError(f"Resolve response has an invalid {field}")
        for field in (
            "resolved_version",
            "artifact_url",
            "manifest_url",
            "signature_status",
            "etag",
        ):
            if not isinstance(payload[field], str) or not payload[field]:
                raise RegistryError(f"Resolve response has an invalid {field}")
        return payload

    def manifest(self, manifest_url: str) -> dict[str, Any]:
        return self._json("GET", manifest_url)

    def download(self, artifact_url: str, destination: Path) -> Path:
        url = self._url(artifact_url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".download", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temp_name)
        total = 0
        visited = {url}
        redirects = 0
        try:
            while True:
                with self.client.stream(
                    "GET",
                    url,
                    headers=self._headers_for(url),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        if redirects >= _MAX_REDIRECTS:
                            raise RegistryError(
                                f"Artifact redirect limit exceeded ({_MAX_REDIRECTS})"
                            )
                        target = self._redirect_target(url, response)
                        if target in visited:
                            raise RegistryError("Artifact redirect cycle detected")
                    else:
                        if not 200 <= response.status_code < 300:
                            raise RegistryError(
                                f"Artifact download returned HTTP {response.status_code}"
                            )
                        declared = response.headers.get("content-length")
                        if declared:
                            try:
                                declared_size = int(declared)
                            except ValueError as exc:
                                raise RegistryError(
                                    "Artifact has an invalid Content-Length"
                                ) from exc
                            if declared_size < 0 or declared_size > self.max_download_bytes:
                                raise RegistryError("Artifact exceeds configured download limit")
                        with temporary.open("wb") as stream:
                            for chunk in response.iter_bytes():
                                total += len(chunk)
                                if total > self.max_download_bytes:
                                    raise RegistryError(
                                        "Artifact exceeds configured download limit"
                                    )
                                stream.write(chunk)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, destination)
                        return destination
                visited.add(target)
                redirects += 1
                url = target
        except httpx.HTTPError as exc:
            raise RegistryError(f"Artifact download failed: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
