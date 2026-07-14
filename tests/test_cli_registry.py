from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from skillhub_cli.errors import RegistryError
from skillhub_cli.registry import RegistryClient


def test_registry_client_uses_locked_contract_and_bearer_auth(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer secret"
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"subject": "user-1"})
        if request.url.path == "/api/v1/skills" and request.method == "GET":
            assert request.url.params["q"] == "pdf"
            return httpx.Response(200, json={"items": [{"name": "pdf"}]})
        if request.url.path.endswith("/resolve"):
            assert dict(request.url.params) == {"label": "stable"}
            return httpx.Response(
                200,
                json={
                    "resolved_version": "1.2.3",
                    "artifact_sha256": "a" * 64,
                    "manifest_sha256": "b" * 64,
                    "artifact_url": "/api/v1/artifacts/a",
                    "manifest_url": "/api/v1/manifests/a",
                    "signature_status": "verified",
                    "etag": '"sha256:a"',
                },
            )
        if request.url.path == "/api/v1/manifests/a":
            return httpx.Response(200, json={"schema_version": "1.0"})
        if request.url.path == "/api/v1/artifacts/a":
            return httpx.Response(200, content=b"zip-bytes")
        return httpx.Response(404, json={"detail": "not found"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RegistryClient("https://hub.example/api/v1", token="secret", client=http)
    assert client.me()["subject"] == "user-1"
    assert client.search("pdf")["items"][0]["name"] == "pdf"
    resolved = client.resolve("personal", "pdf")
    assert resolved["resolved_version"] == "1.2.3"
    assert client.manifest(resolved["manifest_url"])["schema_version"] == "1.0"
    target = client.download(resolved["artifact_url"], tmp_path / "artifact.zip")
    assert target.read_bytes() == b"zip-bytes"
    assert seen
    http.close()


def test_registry_rejects_ambiguous_selector_and_contract_gaps() -> None:
    client = RegistryClient(
        "https://hub.example/api/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"artifact_url": "x"}))
        ),
    )
    with pytest.raises(RegistryError, match="mutually exclusive"):
        client.resolve("personal", "pdf", version="1.0.0", label="stable")
    with pytest.raises(RegistryError, match="missing fields"):
        client.resolve("personal", "pdf")
    client.client.close()


def test_registry_requires_a_valid_manifest_digest() -> None:
    response = {
        "resolved_version": "1.2.3",
        "artifact_sha256": "a" * 64,
        "artifact_url": "/api/v1/artifacts/a",
        "manifest_url": "/api/v1/manifests/b",
        "signature_status": "verified",
        "etag": '"sha256:a"',
    }
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    client = RegistryClient("https://hub.example/api/v1", client=http)
    with pytest.raises(RegistryError, match="missing fields: manifest_sha256"):
        client.resolve("personal", "pdf")

    response["manifest_sha256"] = "not-a-digest"
    with pytest.raises(RegistryError, match="invalid manifest_sha256"):
        client.resolve("personal", "pdf")
    http.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example/api/v1",
        "http://localhost.evil/api/v1",
        "https://user:password@hub.example/api/v1",
    ],
)
def test_registry_rejects_insecure_remote_or_credentialed_base_urls(url: str) -> None:
    with pytest.raises(RegistryError):
        RegistryClient(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/api/v1",
        "http://127.0.0.1:8000/api/v1",
        "http://[::1]:8000/api/v1",
    ],
)
def test_registry_allows_loopback_http_for_local_development(url: str) -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = RegistryClient(url, client=http)
    assert client.base_url.endswith("/api/v1")
    http.close()


def test_registry_normalizes_api_root_and_does_not_leak_token_cross_origin(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "hub.example":
            assert request.headers["authorization"] == "Bearer secret"
            assert request.url.path == "/api/v1/me"
            return httpx.Response(200, json={"subject": "user-1"})
        assert request.url.host == "cos.example"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"artifact")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RegistryClient("https://hub.example", token="secret", client=http)
    assert client.me()["subject"] == "user-1"
    target = client.download("https://cos.example/signed.zip", tmp_path / "artifact.zip")
    assert target.read_bytes() == b"artifact"
    assert len(seen) == 2
    http.close()


def test_artifact_follows_cos_307_without_forwarding_registry_token(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "hub.example":
            assert request.headers["authorization"] == "Bearer secret"
            return httpx.Response(
                307,
                headers={"Location": "https://bucket.cos.example/releases/demo.zip?sig=ok"},
            )
        assert request.url.host == "bucket.cos.example"
        assert "authorization" not in request.headers
        assert request.url.params["sig"] == "ok"
        return httpx.Response(200, content=b"verified-zip")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RegistryClient("https://hub.example/api/v1", token="secret", client=http)
    target = client.download("artifacts/digest", tmp_path / "artifact.zip")
    assert target.read_bytes() == b"verified-zip"
    assert [request.url.host for request in seen] == ["hub.example", "bucket.cos.example"]
    http.close()


@pytest.mark.parametrize(
    "location,error",
    [
        ("http://evil.example/steal", "non-HTTPS redirect"),
        ("https://user:password@evil.example/steal", "must not contain credentials"),
        ("https://hub.example/api/v1/artifacts/digest", "cycle detected"),
    ],
)
def test_artifact_rejects_malicious_redirects(tmp_path: Path, location: str, error: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": location})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RegistryClient("https://hub.example/api/v1", token="secret", client=http)
    destination = tmp_path / "artifact.zip"
    destination.write_bytes(b"existing")
    with pytest.raises(RegistryError, match=error):
        client.download("artifacts/digest", destination)
    assert destination.read_bytes() == b"existing"
    http.close()


def test_artifact_allows_same_origin_loopback_http_redirect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifact"):
            return httpx.Response(307, headers={"Location": "/object"})
        return httpx.Response(200, content=b"local")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RegistryClient("http://127.0.0.1:8000/api/v1", token="secret", client=http)
    target = client.download("artifact", tmp_path / "artifact.zip")
    assert target.read_bytes() == b"local"
    http.close()


def test_artifact_rejects_more_than_three_redirects(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(307, headers={"Location": f"https://cos.example/{step + 1}"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = RegistryClient("https://hub.example/api/v1", token="secret", client=http)
    with pytest.raises(RegistryError, match=r"redirect limit exceeded \(3\)"):
        client.download("https://cos.example/0", tmp_path / "artifact.zip")
    http.close()
