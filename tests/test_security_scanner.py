from __future__ import annotations

import hashlib
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from skillhub.security import ScanPolicy, scan_skill_archive
from skillhub.security import scanner as scanner_module


def _skill_md(
    *,
    name: str = "demo",
    description: str = "A safe demonstration skill.",
    license_id: str | None = "Apache-2.0",
) -> bytes:
    license_line = f"license: {license_id}\n" if license_id is not None else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{license_line}"
        "compatibility:\n"
        "  - codex\n"
        "---\n"
        "# Demo\n"
    ).encode()


def _archive(
    tmp_path: Path,
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    filename: str = "demo.zip",
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w", compression=compression) as output:
        for entry, content in entries:
            output.writestr(entry, content)
    return path


def _scan(path: Path, policy: ScanPolicy | None = None, **kwargs):
    return scan_skill_archive(
        path,
        namespace="personal",
        name="demo",
        version="1.2.3",
        policy=policy,
        **kwargs,
    )


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_valid_root_package_builds_server_manifest(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), ("references/readme.txt", b"safe")],
    )

    result = _scan(archive)

    assert result.passed is True
    assert result.scan_status == "passed"
    assert result.skill_root == ""
    assert result.manifest is not None
    assert result.manifest["artifact_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert result.manifest["compatibility"] == ["codex"]
    assert [item["path"] for item in result.manifest["files"]] == [
        "SKILL.md",
        "references/readme.txt",
    ]


def test_valid_single_wrapper_is_normalized_to_package_paths(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("demo/SKILL.md", _skill_md()), ("demo/assets/data.json", b"{}")],
    )

    result = _scan(archive)

    assert result.passed is True
    assert result.skill_root == "demo"
    assert [item.path for item in result.files] == ["SKILL.md", "assets/data.json"]


@pytest.mark.parametrize(
    ("unsafe_path", "expected_code"),
    [
        ("../escape", "ENTRY_PATH_TRAVERSAL"),
        ("nested/../../escape", "ENTRY_PATH_TRAVERSAL"),
        ("/absolute", "ENTRY_PATH_ABSOLUTE"),
        ("C:/absolute", "ENTRY_PATH_ABSOLUTE"),
        ("scripts\\run.ps1", "ENTRY_PATH_BACKSLASH"),
        ("double//component", "ENTRY_PATH_AMBIGUOUS"),
    ],
)
def test_unsafe_paths_are_rejected(
    tmp_path: Path,
    unsafe_path: str,
    expected_code: str,
) -> None:
    archive = _archive(tmp_path, [("SKILL.md", _skill_md()), (unsafe_path, b"payload")])

    result = _scan(archive)

    assert result.passed is False
    assert expected_code in _codes(result)
    assert result.manifest is None


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO])
def test_symlink_and_device_entries_are_rejected(tmp_path: Path, kind: int) -> None:
    unsafe = zipfile.ZipInfo("unsafe")
    unsafe.create_system = 3
    unsafe.external_attr = (kind | 0o777) << 16
    archive = _archive(tmp_path, [("SKILL.md", _skill_md()), (unsafe, b"target")])

    result = _scan(archive)

    assert result.passed is False
    assert "ENTRY_TYPE_UNSAFE" in _codes(result)


def test_duplicate_and_case_conflicting_paths_are_rejected(tmp_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        archive = _archive(
            tmp_path,
            [
                ("SKILL.md", _skill_md()),
                ("README.md", b"one"),
                ("README.md", b"two"),
                ("readme.md", b"three"),
            ],
        )

    result = _scan(archive)

    assert result.passed is False
    assert {"DUPLICATE_ENTRY", "PATH_CASE_CONFLICT"} <= _codes(result)


def test_file_directory_prefix_conflict_is_rejected(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), ("assets", b"file"), ("assets/item.txt", b"nested")],
    )

    result = _scan(archive)

    assert result.passed is False
    assert "PATH_TYPE_CONFLICT" in _codes(result)


def test_multiple_wrapper_roots_are_rejected(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("one/SKILL.md", _skill_md()), ("two/readme.txt", b"other root")],
    )

    result = _scan(archive)

    assert result.passed is False
    assert "MULTIPLE_ROOTS" in _codes(result)


def test_entry_count_file_total_and_ratio_limits_fail_closed(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), ("large.txt", b"A" * 4096)],
    )

    entry_result = _scan(archive, ScanPolicy(max_entries=1))
    file_result = _scan(archive, ScanPolicy(max_file_size=1024))
    total_result = _scan(archive, ScanPolicy(max_total_uncompressed_size=2048))
    ratio_result = _scan(archive, ScanPolicy(max_compression_ratio=5.0))

    assert "ENTRY_COUNT_EXCEEDED" in _codes(entry_result)
    assert "FILE_TOO_LARGE" in _codes(file_result)
    assert "UNCOMPRESSED_TOTAL_TOO_LARGE" in _codes(total_result)
    assert {
        "COMPRESSION_RATIO_EXCEEDED",
        "TOTAL_COMPRESSION_RATIO_EXCEEDED",
    } & _codes(ratio_result)


def test_oversized_archive_is_rejected_before_hashing_or_opening(tmp_path: Path) -> None:
    archive = _archive(tmp_path, [("SKILL.md", _skill_md())])

    result = _scan(archive, ScanPolicy(max_archive_size=1))

    assert result.passed is False
    assert result.artifact_sha256 == ""
    assert _codes(result) == {"ARCHIVE_TOO_LARGE"}


def test_archive_replacement_between_hash_and_zip_open_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive(tmp_path, [("SKILL.md", _skill_md())])
    replacement = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), ("extra.txt", b"replacement")],
        filename="replacement.zip",
    )
    original_verifier = scanner_module.verify_detached_signature

    def replacing_verifier(path, digest, policy):
        path.write_bytes(replacement.read_bytes())
        return original_verifier(path, digest, policy)

    monkeypatch.setattr(scanner_module, "verify_detached_signature", replacing_verifier)

    result = _scan(archive)

    assert result.passed is False
    assert "ARCHIVE_CHANGED_DURING_SCAN" in _codes(result)


def test_directory_entry_cannot_hide_file_data(tmp_path: Path) -> None:
    hidden_directory = zipfile.ZipInfo("hidden/")
    hidden_directory.create_system = 3
    hidden_directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), (hidden_directory, b"hidden payload")],
    )

    result = _scan(archive)

    assert result.passed is False
    assert "DIRECTORY_ENTRY_HAS_DATA" in _codes(result)


def test_expected_artifact_digest_is_enforced(tmp_path: Path) -> None:
    archive = _archive(tmp_path, [("SKILL.md", _skill_md())])

    result = _scan(archive, artifact_sha256="0" * 64)

    assert result.passed is False
    assert "ARTIFACT_DIGEST_MISMATCH" in _codes(result)


def test_frontmatter_name_and_description_are_required(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md(name="other", description=""))],
    )

    result = _scan(archive)

    assert result.passed is False
    assert {
        "FRONTMATTER_NAME_MISMATCH",
        "FRONTMATTER_DESCRIPTION_MISSING",
    } <= _codes(result)


@pytest.mark.parametrize(
    "frontmatter",
    [
        (b"---\nname: demo\nname: other\ndescription: duplicate\nlicense: Apache-2.0\n---\n"),
        (b"---\nname: demo\ndescription: &value alias\ncopy: *value\nlicense: Apache-2.0\n---\n"),
    ],
)
def test_ambiguous_yaml_features_are_rejected(tmp_path: Path, frontmatter: bytes) -> None:
    archive = _archive(tmp_path, [("SKILL.md", frontmatter)])

    result = _scan(archive)

    assert result.passed is False
    assert "FRONTMATTER_INVALID" in _codes(result)


def test_frontmatter_size_is_bounded_independently(tmp_path: Path) -> None:
    archive = _archive(tmp_path, [("SKILL.md", _skill_md(description="A" * 200))])

    result = _scan(archive, ScanPolicy(max_frontmatter_size=64))

    assert result.passed is False
    assert "FRONTMATTER_TOO_LARGE" in _codes(result)


def test_root_license_file_satisfies_license_check(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md(license_id=None)), ("LICENSE", b"Example license text")],
    )

    result = _scan(archive)

    assert result.passed is True
    assert result.license == "SEE-LICENSE-IN-PACKAGE"


def test_missing_or_disallowed_license_fails(tmp_path: Path) -> None:
    missing_archive = _archive(tmp_path, [("SKILL.md", _skill_md(license_id=None))])
    disallowed_archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md(license_id="GPL-3.0-only"))],
        filename="disallowed.zip",
    )

    missing = _scan(missing_archive)
    disallowed = _scan(
        disallowed_archive,
        ScanPolicy(allowed_licenses=frozenset({"Apache-2.0", "MIT"})),
    )

    assert "LICENSE_MISSING" in _codes(missing)
    assert "LICENSE_NOT_ALLOWED" in _codes(disallowed)


def test_optional_missing_license_uses_noassertion_and_keeps_manifest(tmp_path: Path) -> None:
    archive = _archive(tmp_path, [("SKILL.md", _skill_md(license_id=None))])

    result = _scan(archive, ScanPolicy(require_license=False))

    assert result.passed is True
    assert result.scan_status == "passed_with_warnings"
    assert result.license == "NOASSERTION"
    assert result.manifest is not None
    assert result.manifest["license"] == "NOASSERTION"


def test_empty_license_file_does_not_satisfy_license_policy(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md(license_id=None)), ("LICENSE", b" \n")],
    )

    result = _scan(archive)

    assert result.passed is False
    assert {"LICENSE_FILE_EMPTY", "LICENSE_MISSING"} <= _codes(result)


def test_secret_detection_redacts_value_and_exact_exemption_is_auditable(tmp_path: Path) -> None:
    secret = b"ghp_" + b"A" * 40
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), ("references/example.txt", secret)],
    )

    rejected = _scan(archive)
    finding = next(issue for issue in rejected.issues if issue.code == "SECRET_DETECTED")
    exempted = _scan(
        archive,
        ScanPolicy(secret_exemptions=frozenset({finding.fingerprint})),
    )

    assert rejected.passed is False
    assert finding.fingerprint is not None
    assert secret.decode() not in finding.message
    assert exempted.passed is True
    assert exempted.scan_status == "passed_with_warnings"
    assert "SECRET_EXEMPTED" in _codes(exempted)


def test_scripts_are_warned_about_but_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    archive = _archive(
        tmp_path,
        [
            ("SKILL.md", _skill_md()),
            ("scripts/run.sh", f"touch {marker}\n".encode()),
        ],
    )

    result = _scan(archive)

    assert result.passed is True
    assert result.scan_status == "passed_with_warnings"
    assert "SCRIPT_PRESENT" in _codes(result)
    assert not marker.exists()


def test_script_policy_can_promote_warning_to_error(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path,
        [("SKILL.md", _skill_md()), ("scripts/run.py", b"print('not executed')")],
    )

    result = _scan(archive, ScanPolicy(scripts_are_errors=True))

    assert result.passed is False
    assert any(
        issue.code == "SCRIPT_PRESENT" and issue.severity == "error" for issue in result.issues
    )
