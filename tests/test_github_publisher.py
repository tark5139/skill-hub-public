from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from skillhub.publishers.github import (
    AssetVerificationError,
    AuthorizationError,
    GitHubAsset,
    GitHubConflictError,
    GitHubPublisher,
    GitHubRelease,
    IntegrityError,
    PublicationError,
    PublicSkillVersion,
    UrllibGitHubReleaseClient,
    VersionPublicationAuthorization,
)


class FakeGitHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.existing_tag = False
        self.existing_release: GitHubRelease | None = None
        self.assets: list[GitHubAsset] = []
        self.asset_contents: dict[str, bytes] = {}
        self.corrupt_asset: str | None = None
        self.replace_asset_during_publish: str | None = None
        self.post_publish_size_mismatch: str | None = None
        self.post_publish_rename: tuple[str, str] | None = None
        self.post_publish_url_suffix: str | None = None
        self.list_assets_count = 0
        self.repository_immutable = True
        self.publish_immutable = True

    def immutable_releases_enabled(self, owner: str, repository: str) -> bool:
        self.calls.append(("immutable_releases_enabled", owner, repository))
        return self.repository_immutable

    def tag_exists(self, owner: str, repository: str, tag: str) -> bool:
        self.calls.append(("tag_exists", owner, repository, tag))
        return self.existing_tag

    def get_release_by_tag(self, owner: str, repository: str, tag: str) -> GitHubRelease | None:
        self.calls.append(("get_release_by_tag", owner, repository, tag))
        return self.existing_release

    def create_draft_release(
        self,
        owner: str,
        repository: str,
        *,
        tag: str,
        name: str,
        body: str,
        target_commitish: str,
    ) -> GitHubRelease:
        self.calls.append(
            ("create_draft_release", owner, repository, tag, name, body, target_commitish)
        )
        return GitHubRelease(
            id=41,
            tag_name=tag,
            draft=True,
            upload_url="https://uploads.github.test/releases/41/assets{?name,label}",
        )

    def upload_asset(
        self,
        release: GitHubRelease,
        *,
        name: str,
        content: bytes,
        media_type: str,
    ) -> GitHubAsset:
        self.calls.append(("upload_asset", release.id, name, media_type))
        self.asset_contents[name] = content
        digest = hashlib.sha256(content).hexdigest()
        asset = GitHubAsset(
            name=name,
            size=len(content),
            digest=f"sha256:{digest}",
            browser_download_url=f"https://github.test/download/{name}",
        )
        self.assets.append(asset)
        return asset

    def list_assets(self, owner: str, repository: str, release_id: int) -> list[GitHubAsset]:
        self.calls.append(("list_assets", owner, repository, release_id))
        self.list_assets_count += 1
        assets = [
            replace(asset, digest="sha256:" + ("0" * 64))
            if asset.name == self.corrupt_asset
            else asset
            for asset in self.assets
        ]
        if self.list_assets_count == 2:
            if self.post_publish_size_mismatch is not None:
                assets = [
                    replace(asset, size=asset.size + 1)
                    if asset.name == self.post_publish_size_mismatch
                    else asset
                    for asset in assets
                ]
            if self.post_publish_rename is not None:
                old_name, new_name = self.post_publish_rename
                assets = [
                    replace(asset, name=new_name) if asset.name == old_name else asset
                    for asset in assets
                ]
            if self.post_publish_url_suffix is not None:
                assets = [
                    replace(
                        asset,
                        browser_download_url=(asset.browser_download_url or "")
                        + self.post_publish_url_suffix,
                    )
                    for asset in assets
                ]
        return assets

    def publish_release(self, owner: str, repository: str, release_id: int) -> GitHubRelease:
        self.calls.append(("publish_release", owner, repository, release_id))
        if self.replace_asset_during_publish is not None:
            name = self.replace_asset_during_publish
            original = self.asset_contents[name]
            replacement = bytes(byte ^ 0xFF for byte in original)
            self.asset_contents[name] = replacement
            self.assets = [
                replace(asset, digest=f"sha256:{hashlib.sha256(replacement).hexdigest()}")
                if asset.name == name
                else asset
                for asset in self.assets
            ]
        tag = str(self.calls[2][3])
        return GitHubRelease(
            id=release_id,
            tag_name=tag,
            draft=False,
            upload_url="",
            html_url=f"https://github.test/releases/tag/{tag}",
            immutable=self.publish_immutable,
        )


class StaticResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> StaticResponse:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def read(self) -> bytes:
        return self.payload


def make_inputs() -> tuple[PublicSkillVersion, VersionPublicationAuthorization]:
    package = b"exact-original-zip-bytes"
    digest = hashlib.sha256(package).hexdigest()
    publication = PublicSkillVersion(
        skill="meeting-notes",
        version="1.2.3",
        package_bytes=package,
        manifest={
            "schema_version": "1.0",
            "namespace": "tark5139",
            "name": "meeting-notes",
            "version": "1.2.3",
            "description": "A test Skill",
            "compatibility": ["codex"],
            "files": [],
            "artifact_sha256": digest,
            "license": "Apache-2.0",
            "signatures": [{"algorithm": "ed25519"}],
            "scan_status": "passed",
            "created_at": "2026-07-14T00:00:00Z",
        },
        signature=b"detached-signature",
        license_text=(Path(__file__).resolve().parents[1] / "LICENSE").read_text(),
    )
    authorization = VersionPublicationAuthorization(
        skill="meeting-notes",
        version="1.2.3",
        artifact_sha256=digest,
        approved_by="tark5139",
        approval_id="review-2026-0001",
        approved_at=datetime(2026, 7, 14, tzinfo=UTC),
    )
    return publication, authorization


class GitHubPublisherTests(unittest.TestCase):
    def test_rest_client_checks_repository_immutable_release_policy(self) -> None:
        requests: list[object] = []

        def opener(req: object, **_: object) -> StaticResponse:
            requests.append(req)
            return StaticResponse({"enabled": True, "enforced_by_owner": False})

        client = UrllibGitHubReleaseClient("test-token", opener=opener)
        self.assertTrue(client.immutable_releases_enabled("tark5139", "skill-hub-public"))
        self.assertEqual(
            requests[0].full_url,
            "https://api.github.com/repos/tark5139/skill-hub-public/immutable-releases",
        )

    def test_publishes_only_after_exact_asset_inventory_verification(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.post_publish_url_suffix = "?snapshot=published"

        result = GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(result.tag, "meeting-notes-v1.2.3")
        self.assertTrue(result.immutable)
        self.assertEqual(result.approval_id, "review-2026-0001")
        self.assertEqual(
            [call[0] for call in client.calls[:3]],
            ["immutable_releases_enabled", "tag_exists", "get_release_by_tag"],
        )
        self.assertEqual(
            [call[0] for call in client.calls[-3:]],
            ["list_assets", "publish_release", "list_assets"],
        )
        self.assertEqual(client.list_assets_count, 2)
        self.assertTrue(
            all(
                asset.browser_download_url
                and asset.browser_download_url.endswith("?snapshot=published")
                for asset in result.assets
            )
        )
        self.assertEqual(
            set(client.asset_contents),
            {
                "meeting-notes-v1.2.3.zip",
                "manifest.json",
                "SHA256SUMS",
                "signature.sig.json",
                "ATTESTATION.md",
                "LICENSE",
                "README.md",
            },
        )
        self.assertEqual(
            client.asset_contents["meeting-notes-v1.2.3.zip"],
            publication.package_bytes,
        )
        checksum_text = client.asset_contents["SHA256SUMS"].decode()
        self.assertIn(f"{authorization.artifact_sha256}  meeting-notes-v1.2.3.zip", checksum_text)
        self.assertNotIn("SHA256SUMS\n", checksum_text)

    def test_rejects_existing_tag_and_never_creates_or_overwrites_release(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.existing_tag = True

        with self.assertRaises(GitHubConflictError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(
            [call[0] for call in client.calls],
            ["immutable_releases_enabled", "tag_exists", "get_release_by_tag"],
        )

    def test_rejects_existing_release_even_when_tag_lookup_is_empty(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.existing_release = GitHubRelease(
            id=2,
            tag_name="meeting-notes-v1.2.3",
            draft=True,
            upload_url="",
        )

        with self.assertRaises(GitHubConflictError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(len(client.calls), 3)

    def test_rejects_authorization_for_another_artifact_before_network(self) -> None:
        publication, authorization = make_inputs()
        authorization = replace(authorization, artifact_sha256="0" * 64)
        client = FakeGitHubClient()

        with self.assertRaises(AuthorizationError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls, [])

    def test_rejects_unapproved_decision_before_network(self) -> None:
        publication, authorization = make_inputs()
        authorization = replace(authorization, decision="rejected")
        client = FakeGitHubClient()

        with self.assertRaises(AuthorizationError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls, [])

    def test_rejects_package_that_does_not_match_manifest_before_network(self) -> None:
        publication, authorization = make_inputs()
        publication = replace(publication, package_bytes=b"tampered")
        client = FakeGitHubClient()

        with self.assertRaises(IntegrityError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls, [])

    def test_rejects_placeholder_license_text_before_network(self) -> None:
        publication, authorization = make_inputs()
        publication = replace(publication, license_text="Apache-2.0")
        client = FakeGitHubClient()

        with self.assertRaises(IntegrityError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls, [])

    def test_asset_digest_mismatch_keeps_draft_and_does_not_publish(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.corrupt_asset = "manifest.json"

        with self.assertRaises(AssetVerificationError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls[-1][0], "list_assets")
        self.assertNotIn("publish_release", [call[0] for call in client.calls])

    def test_detects_asset_replaced_between_preflight_and_publish(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.replace_asset_during_publish = "meeting-notes-v1.2.3.zip"

        with self.assertRaisesRegex(AssetVerificationError, "asset digest mismatch"):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(
            [call[0] for call in client.calls[-3:]],
            ["list_assets", "publish_release", "list_assets"],
        )
        self.assertEqual(client.list_assets_count, 2)

    def test_rejects_post_publish_asset_size_mismatch(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.post_publish_size_mismatch = "manifest.json"

        with self.assertRaisesRegex(AssetVerificationError, "asset size mismatch"):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls[-1][0], "list_assets")
        self.assertEqual(client.list_assets_count, 2)

    def test_rejects_post_publish_asset_name_mismatch(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.post_publish_rename = ("README.md", "README-replaced.md")

        with self.assertRaisesRegex(AssetVerificationError, "asset inventory mismatch"):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls[-1][0], "list_assets")
        self.assertEqual(client.list_assets_count, 2)

    def test_requires_repository_release_immutability(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.repository_immutable = False

        with self.assertRaises(PublicationError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual([call[0] for call in client.calls], ["immutable_releases_enabled"])

    def test_rechecks_release_immutability_after_publish(self) -> None:
        publication, authorization = make_inputs()
        client = FakeGitHubClient()
        client.publish_immutable = False

        with self.assertRaises(PublicationError):
            GitHubPublisher(client).publish(publication, authorization)

        self.assertEqual(client.calls[-1][0], "publish_release")


if __name__ == "__main__":
    unittest.main()
