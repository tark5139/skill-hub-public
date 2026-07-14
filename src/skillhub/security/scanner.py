"""Static, fail-closed scanner for immutable ZIP skill packages."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from .models import ScanIssue, ScannedFile, ScanPolicy, ScanResult
from .signatures import verify_detached_signature

_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:[-+][0-9A-Za-z.-]+)?$"
)
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LICENSE_FILES = {
    "license",
    "license.md",
    "license.txt",
    "copying",
    "copying.md",
    "copying.txt",
}
_SCRIPT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".fish",
    ".js",
    ".mjs",
    ".pl",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".zsh",
}
_SECRET_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("aws-access-key", re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")),
    ("github-token", re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,255}")),
    ("github-fine-grained-token", re.compile(rb"github_pat_[A-Za-z0-9_]{20,255}")),
    ("openai-api-key", re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    (
        "generic-secret-assignment",
        re.compile(
            rb"(?i)(?:api[_-]?key|client[_-]?secret|password|secret|token)"
            rb"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{16,})"
        ),
    ),
)
type _FileIdentity = tuple[int, int, int, int, int]


class _StrictFrontmatterLoader(yaml.SafeLoader):
    """Safe YAML loader that removes aliases and duplicate-key ambiguity."""

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise ConstructorError(
                None,
                None,
                "YAML aliases are forbidden in skill frontmatter",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def _construct_unique_mapping(
    loader: _StrictFrontmatterLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(None, None, "expected a mapping", node.start_mark)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictFrontmatterLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def scan_skill_archive(
    archive_path: Path,
    *,
    namespace: str,
    name: str,
    version: str,
    artifact_sha256: str | None = None,
    policy: ScanPolicy | None = None,
) -> ScanResult:
    """Scan an archive without extracting or executing any package content."""

    archive_path = Path(archive_path)
    policy = policy or ScanPolicy()
    issues: list[ScanIssue] = []
    scanned_files: list[ScannedFile] = []
    calculated_digest = ""
    description: str | None = None
    license_id: str | None = None
    skill_root: str | None = None
    signature_key_id: str | None = None
    signature_record: dict[str, object] | None = None
    compatibility: list[str] = []
    hashed_identity: _FileIdentity | None = None

    _validate_coordinates(namespace, name, version, issues)
    if not archive_path.exists() or not archive_path.is_file() or archive_path.is_symlink():
        issues.append(
            ScanIssue(
                "ARCHIVE_NOT_REGULAR_FILE", "error", "Archive must be an existing regular file."
            )
        )
        return _result(
            namespace,
            name,
            version,
            calculated_digest,
            scanned_files,
            issues,
            description,
            license_id,
            skill_root,
            signature_key_id,
            signature_record,
        )

    try:
        archive_size = archive_path.stat().st_size
        if archive_size > policy.max_archive_size:
            issues.append(
                ScanIssue(
                    "ARCHIVE_TOO_LARGE",
                    "error",
                    f"Archive exceeds the {policy.max_archive_size}-byte limit.",
                )
            )
            return _result(
                namespace,
                name,
                version,
                calculated_digest,
                scanned_files,
                issues,
                description,
                license_id,
                skill_root,
                signature_key_id,
                signature_record,
            )
        calculated_digest, hashed_identity = _sha256_file(archive_path)
    except OSError:
        issues.append(ScanIssue("ARCHIVE_READ_FAILED", "error", "Archive could not be read."))
        return _result(
            namespace,
            name,
            version,
            calculated_digest,
            scanned_files,
            issues,
            description,
            license_id,
            skill_root,
            signature_key_id,
            signature_record,
        )

    if artifact_sha256 is not None:
        expected = artifact_sha256.lower()
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            issues.append(
                ScanIssue(
                    "ARTIFACT_DIGEST_INVALID",
                    "error",
                    "Expected artifact_sha256 is not a valid SHA-256 digest.",
                )
            )
        elif not _constant_time_digest_equal(expected, calculated_digest):
            issues.append(
                ScanIssue(
                    "ARTIFACT_DIGEST_MISMATCH",
                    "error",
                    "Archive SHA-256 does not match the expected digest.",
                )
            )

    signature = verify_detached_signature(archive_path, calculated_digest, policy)
    issues.extend(signature.issues)
    signature_key_id = signature.key_id
    signature_record = signature.record

    try:
        with zipfile.ZipFile(archive_path) as archive:
            if hashed_identity is None or _zip_file_identity(archive) != hashed_identity:
                issues.append(
                    ScanIssue(
                        "ARCHIVE_CHANGED_DURING_SCAN",
                        "error",
                        "Archive identity changed after hashing; scan failed closed.",
                    )
                )
                return _result(
                    namespace,
                    name,
                    version,
                    calculated_digest,
                    scanned_files,
                    issues,
                    description,
                    license_id,
                    skill_root,
                    signature_key_id,
                    signature_record,
                )
            infos = archive.infolist()
            validated = _validate_archive_structure(infos, policy, issues)
            if validated is None:
                return _result(
                    namespace,
                    name,
                    version,
                    calculated_digest,
                    scanned_files,
                    issues,
                    description,
                    license_id,
                    skill_root,
                    signature_key_id,
                    signature_record,
                )
            skill_root, file_infos = validated
            actual_total = 0
            content_by_path: dict[str, bytes] = {}
            for relative_path, info in file_infos:
                try:
                    content = _read_bounded(archive, info, policy.max_file_size)
                except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, ValueError):
                    issues.append(
                        ScanIssue(
                            "ARCHIVE_ENTRY_READ_FAILED",
                            "error",
                            "Archive entry failed integrity or decompression checks.",
                            relative_path,
                        )
                    )
                    continue
                actual_total += len(content)
                if actual_total > policy.max_total_uncompressed_size:
                    issues.append(
                        ScanIssue(
                            "UNCOMPRESSED_TOTAL_TOO_LARGE",
                            "error",
                            "Actual uncompressed data exceeds the configured total limit.",
                        )
                    )
                    break
                if len(content) != info.file_size:
                    issues.append(
                        ScanIssue(
                            "ENTRY_SIZE_MISMATCH",
                            "error",
                            "Archive entry size does not match its central-directory metadata.",
                            relative_path,
                        )
                    )
                digest = hashlib.sha256(content).hexdigest()
                scanned_files.append(
                    ScannedFile(
                        path=relative_path,
                        size=len(content),
                        sha256=digest,
                        media_type=_media_type(relative_path),
                    )
                )
                content_by_path[relative_path] = content
                _scan_secrets(relative_path, content, policy, issues)
                _warn_about_scripts(relative_path, info, policy, issues)

            skill_document = content_by_path.get("SKILL.md")
            if skill_document is None:
                issues.append(
                    ScanIssue("SKILL_MD_MISSING", "error", "Package root must contain SKILL.md.")
                )
            else:
                frontmatter = _parse_frontmatter(
                    skill_document,
                    policy.max_frontmatter_size,
                    issues,
                )
                if frontmatter is not None:
                    description, license_id = _validate_frontmatter(frontmatter, name, issues)
                    compatibility = _parse_compatibility(frontmatter, issues)
                else:
                    compatibility = []

            package_paths = set(content_by_path)
            license_files = sorted(
                path
                for path in package_paths
                if "/" not in path and path.casefold() in _LICENSE_FILES
            )
            usable_license_files = [path for path in license_files if content_by_path[path].strip()]
            if license_files and not usable_license_files:
                issues.append(
                    ScanIssue(
                        "LICENSE_FILE_EMPTY",
                        "error" if policy.require_license else "warning",
                        "Root license file is empty.",
                        license_files[0],
                    )
                )
            if license_id is None and usable_license_files:
                license_id = "SEE-LICENSE-IN-PACKAGE"
            license_id = _validate_license(license_id, policy, issues)
            if _zip_file_identity(archive) != hashed_identity:
                issues.append(
                    ScanIssue(
                        "ARCHIVE_CHANGED_DURING_SCAN",
                        "error",
                        "Archive changed while its contents were being scanned.",
                    )
                )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        issues.append(
            ScanIssue("ARCHIVE_INVALID", "error", "Input is not a valid supported ZIP archive.")
        )
        compatibility = []

    scanned_files.sort(key=lambda item: item.path)
    return _result(
        namespace,
        name,
        version,
        calculated_digest,
        scanned_files,
        issues,
        description,
        license_id,
        skill_root,
        signature_key_id,
        signature_record,
        compatibility=compatibility,
    )


def _validate_coordinates(
    namespace: str,
    name: str,
    version: str,
    issues: list[ScanIssue],
) -> None:
    if not _IDENTIFIER_RE.fullmatch(namespace):
        issues.append(ScanIssue("NAMESPACE_INVALID", "error", "Namespace format is invalid."))
    if not _IDENTIFIER_RE.fullmatch(name):
        issues.append(ScanIssue("SKILL_NAME_INVALID", "error", "Skill name format is invalid."))
    if not _VERSION_RE.fullmatch(version):
        issues.append(
            ScanIssue("VERSION_INVALID", "error", "Version must be semantic version text.")
        )


def _validate_archive_structure(
    infos: list[zipfile.ZipInfo],
    policy: ScanPolicy,
    issues: list[ScanIssue],
) -> tuple[str, list[tuple[str, zipfile.ZipInfo]]] | None:
    if not infos:
        issues.append(ScanIssue("ARCHIVE_EMPTY", "error", "Archive contains no entries."))
        return None
    if len(infos) > policy.max_entries:
        issues.append(
            ScanIssue(
                "ENTRY_COUNT_EXCEEDED",
                "error",
                f"Archive exceeds the {policy.max_entries}-entry limit.",
            )
        )
        return None
    if _has_errors(issues):
        return None

    seen: dict[str, tuple[str, bool]] = {}
    paths: list[tuple[str, bool, zipfile.ZipInfo]] = []
    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        raw_name = getattr(info, "orig_filename", info.filename)
        path = _validate_entry_path(raw_name, issues)
        if path is None:
            continue
        is_dir = info.is_dir()
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            issues.append(
                ScanIssue(
                    "ENTRY_TYPE_UNSAFE",
                    "error",
                    "Symlink, device, socket, and FIFO entries are forbidden.",
                    path,
                )
            )
        elif (file_type == stat.S_IFDIR and not is_dir) or (file_type == stat.S_IFREG and is_dir):
            issues.append(
                ScanIssue(
                    "ENTRY_TYPE_AMBIGUOUS",
                    "error",
                    "Entry metadata disagrees about whether the path is a directory.",
                    path,
                )
            )
        if is_dir and (info.file_size != 0 or info.compress_size != 0):
            issues.append(
                ScanIssue(
                    "DIRECTORY_ENTRY_HAS_DATA",
                    "error",
                    "Directory entries must not contain hidden file data.",
                    path,
                )
            )
        if info.flag_bits & 0x1:
            issues.append(
                ScanIssue("ENTRY_ENCRYPTED", "error", "Encrypted ZIP entries are forbidden.", path)
            )
        if info.file_size < 0 or info.compress_size < 0:
            issues.append(ScanIssue("ENTRY_SIZE_INVALID", "error", "Entry size is invalid.", path))
        if not is_dir and info.file_size > policy.max_file_size:
            issues.append(
                ScanIssue(
                    "FILE_TOO_LARGE",
                    "error",
                    f"File exceeds the {policy.max_file_size}-byte limit.",
                    path,
                )
            )
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        if not is_dir and _compression_ratio(info.file_size, info.compress_size) > (
            policy.max_compression_ratio
        ):
            issues.append(
                ScanIssue(
                    "COMPRESSION_RATIO_EXCEEDED",
                    "error",
                    "Entry compression ratio exceeds the configured limit.",
                    path,
                )
            )

        collision_key = unicodedata.normalize("NFC", path).casefold()
        previous = seen.get(collision_key)
        if previous is not None:
            previous_path, previous_is_dir = previous
            code = "DUPLICATE_ENTRY" if previous_path == path else "PATH_CASE_CONFLICT"
            issues.append(
                ScanIssue(
                    code,
                    "error",
                    "Duplicate or case/Unicode-conflicting archive path is forbidden.",
                    path,
                )
            )
            if previous_is_dir != is_dir:
                issues.append(
                    ScanIssue(
                        "PATH_TYPE_CONFLICT",
                        "error",
                        "The same logical path cannot be both a file and directory.",
                        path,
                    )
                )
        else:
            seen[collision_key] = (path, is_dir)
        paths.append((path, is_dir, info))

    if total_uncompressed > policy.max_total_uncompressed_size:
        issues.append(
            ScanIssue(
                "UNCOMPRESSED_TOTAL_TOO_LARGE",
                "error",
                "Declared uncompressed data exceeds the configured total limit.",
            )
        )
    if _compression_ratio(total_uncompressed, total_compressed) > policy.max_compression_ratio:
        issues.append(
            ScanIssue(
                "TOTAL_COMPRESSION_RATIO_EXCEEDED",
                "error",
                "Overall archive compression ratio exceeds the configured limit.",
            )
        )

    file_paths = {path for path, is_dir, _ in paths if not is_dir}
    _detect_file_directory_conflicts(file_paths, issues)
    if _has_errors(issues):
        return None

    if "SKILL.md" in file_paths:
        root = ""
    else:
        roots = {path.split("/", 1)[0] for path in file_paths}
        if len(roots) != 1:
            issues.append(
                ScanIssue(
                    "MULTIPLE_ROOTS",
                    "error",
                    "Archive must use package-root layout or exactly one wrapper directory.",
                )
            )
            return None
        root = next(iter(roots))
        prefix = f"{root}/"
        if f"{prefix}SKILL.md" not in file_paths:
            issues.append(
                ScanIssue("SKILL_MD_MISSING", "error", "Package root must contain SKILL.md.")
            )
            return None
        outside = [path for path, _, _ in paths if path != root and not path.startswith(prefix)]
        if outside:
            issues.append(
                ScanIssue(
                    "MULTIPLE_ROOTS",
                    "error",
                    "Wrapper layout contains entries outside its single root directory.",
                    outside[0],
                )
            )
            return None

    prefix = f"{root}/" if root else ""
    output: list[tuple[str, zipfile.ZipInfo]] = []
    for path, is_dir, info in paths:
        if is_dir:
            continue
        relative = path.removeprefix(prefix)
        output.append((relative, info))
    output.sort(key=lambda item: item[0])
    return root, output


def _validate_entry_path(raw_name: str, issues: list[ScanIssue]) -> str | None:
    display_path = raw_name[:256] if isinstance(raw_name, str) else None
    if not isinstance(raw_name, str) or not raw_name:
        issues.append(ScanIssue("ENTRY_PATH_INVALID", "error", "Entry path is empty or invalid."))
        return None
    if "\\" in raw_name:
        issues.append(
            ScanIssue(
                "ENTRY_PATH_BACKSLASH",
                "error",
                "Backslashes are forbidden because extraction semantics differ by platform.",
                display_path,
            )
        )
        return None
    if raw_name.startswith("/") or _DRIVE_PATH_RE.match(raw_name):
        issues.append(
            ScanIssue(
                "ENTRY_PATH_ABSOLUTE",
                "error",
                "Absolute archive paths are forbidden.",
                display_path,
            )
        )
        return None
    if _CONTROL_RE.search(raw_name):
        issues.append(
            ScanIssue(
                "ENTRY_PATH_CONTROL_CHAR",
                "error",
                "Control characters are forbidden.",
                display_path,
            )
        )
        return None

    directory_hint = raw_name.endswith("/")
    trimmed = raw_name[:-1] if directory_hint else raw_name
    parts = trimmed.split("/")
    if not trimmed or any(part in {"", ".", ".."} for part in parts):
        code = "ENTRY_PATH_TRAVERSAL" if ".." in parts else "ENTRY_PATH_AMBIGUOUS"
        issues.append(
            ScanIssue(
                code, "error", "Traversal or ambiguous path components are forbidden.", display_path
            )
        )
        return None
    canonical = PurePosixPath(*parts).as_posix()
    return canonical


def _detect_file_directory_conflicts(file_paths: set[str], issues: list[ScanIssue]) -> None:
    canonical_files = {unicodedata.normalize("NFC", path).casefold(): path for path in file_paths}
    for logical_path, original in canonical_files.items():
        parts = logical_path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            if prefix in canonical_files:
                issues.append(
                    ScanIssue(
                        "PATH_TYPE_CONFLICT",
                        "error",
                        "A file path cannot also be an ancestor directory.",
                        original,
                    )
                )


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(min(64 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("decompressed file exceeded limit")
    return b"".join(chunks)


def _parse_frontmatter(
    content: bytes,
    max_size: int,
    issues: list[ScanIssue],
) -> dict[str, object] | None:
    source = content.removeprefix(b"\xef\xbb\xbf")
    first_line_end = source.find(b"\n")
    if first_line_end == -1 or source[:first_line_end].rstrip(b"\r") != b"---":
        issues.append(
            ScanIssue(
                "FRONTMATTER_MISSING",
                "error",
                "SKILL.md must begin with YAML frontmatter.",
                "SKILL.md",
            )
        )
        return None
    search_area = source[first_line_end + 1 : max_size + 1]
    closing = re.search(rb"(?m)^---[ \t]*\r?$", search_area)
    if closing is None:
        if len(source) > max_size:
            issues.append(
                ScanIssue(
                    "FRONTMATTER_TOO_LARGE",
                    "error",
                    f"YAML frontmatter must close within {max_size} bytes.",
                    "SKILL.md",
                )
            )
        else:
            issues.append(
                ScanIssue(
                    "FRONTMATTER_UNTERMINATED",
                    "error",
                    "SKILL.md YAML frontmatter is not terminated.",
                    "SKILL.md",
                )
            )
        return None
    frontmatter_bytes = search_area[: closing.start()]
    try:
        text = frontmatter_bytes.decode("utf-8")
    except UnicodeDecodeError:
        issues.append(
            ScanIssue("SKILL_MD_ENCODING", "error", "SKILL.md must be UTF-8.", "SKILL.md")
        )
        return None
    try:
        parsed = yaml.load(text, Loader=_StrictFrontmatterLoader)
    except yaml.YAMLError:
        issues.append(
            ScanIssue("FRONTMATTER_INVALID", "error", "SKILL.md YAML is invalid.", "SKILL.md")
        )
        return None
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        issues.append(
            ScanIssue(
                "FRONTMATTER_INVALID",
                "error",
                "SKILL.md frontmatter must be a string-keyed mapping.",
                "SKILL.md",
            )
        )
        return None
    return parsed


def _validate_frontmatter(
    frontmatter: dict[str, object],
    expected_name: str,
    issues: list[ScanIssue],
) -> tuple[str | None, str | None]:
    declared_name = frontmatter.get("name")
    description = frontmatter.get("description")
    license_id = frontmatter.get("license")
    if not isinstance(declared_name, str) or not declared_name.strip():
        issues.append(
            ScanIssue(
                "FRONTMATTER_NAME_MISSING", "error", "Frontmatter name is required.", "SKILL.md"
            )
        )
    elif declared_name != expected_name:
        issues.append(
            ScanIssue(
                "FRONTMATTER_NAME_MISMATCH",
                "error",
                "Frontmatter name does not match the requested skill name.",
                "SKILL.md",
            )
        )
    if not isinstance(description, str) or not description.strip():
        issues.append(
            ScanIssue(
                "FRONTMATTER_DESCRIPTION_MISSING",
                "error",
                "Frontmatter description is required.",
                "SKILL.md",
            )
        )
        normalized_description = None
    else:
        normalized_description = description.strip()
        if len(normalized_description) > 4096:
            issues.append(
                ScanIssue(
                    "FRONTMATTER_DESCRIPTION_TOO_LONG",
                    "error",
                    "Frontmatter description exceeds 4096 characters.",
                    "SKILL.md",
                )
            )
    if license_id is None:
        normalized_license = None
    elif (
        not isinstance(license_id, str)
        or not license_id.strip()
        or len(license_id) > 128
        or _CONTROL_RE.search(license_id)
    ):
        issues.append(
            ScanIssue(
                "LICENSE_METADATA_INVALID",
                "error",
                "License metadata must be non-empty text of at most 128 characters.",
                "SKILL.md",
            )
        )
        normalized_license = None
    else:
        normalized_license = license_id.strip()
    return normalized_description, normalized_license


def _parse_compatibility(frontmatter: dict[str, object], issues: list[ScanIssue]) -> list[str]:
    raw = frontmatter.get("compatibility", [])
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(value, str) or not value for value in raw):
        issues.append(
            ScanIssue(
                "COMPATIBILITY_INVALID",
                "error",
                "Compatibility must be a list of non-empty strings.",
                "SKILL.md",
            )
        )
        return []
    if len(set(raw)) != len(raw):
        issues.append(
            ScanIssue(
                "COMPATIBILITY_DUPLICATE",
                "error",
                "Compatibility entries must be unique.",
                "SKILL.md",
            )
        )
        return []
    return sorted(raw)


def _validate_license(
    license_id: str | None,
    policy: ScanPolicy,
    issues: list[ScanIssue],
) -> str | None:
    if license_id is None:
        if policy.require_license:
            issues.append(
                ScanIssue(
                    "LICENSE_MISSING",
                    "error",
                    "A frontmatter license value or root license file is required.",
                )
            )
            return None
        else:
            issues.append(
                ScanIssue("LICENSE_MISSING", "warning", "Package has no declared license.")
            )
            return "NOASSERTION"
    if policy.allowed_licenses is not None and license_id not in policy.allowed_licenses:
        issues.append(
            ScanIssue(
                "LICENSE_NOT_ALLOWED",
                "error",
                "Declared license is not permitted by policy.",
            )
        )
    return license_id


def _scan_secrets(
    path: str,
    content: bytes,
    policy: ScanPolicy,
    issues: list[ScanIssue],
) -> None:
    if any(issue.code == "SECRET_FINDING_LIMIT_EXCEEDED" for issue in issues):
        return
    finding_count = sum(issue.code in {"SECRET_DETECTED", "SECRET_EXEMPTED"} for issue in issues)
    for rule_id, pattern in _SECRET_RULES:
        for match in pattern.finditer(content):
            matched_value = match.group(1) if match.lastindex else match.group(0)
            fingerprint = _secret_fingerprint(path, rule_id, matched_value)
            if fingerprint in policy.secret_exemptions:
                issues.append(
                    ScanIssue(
                        "SECRET_EXEMPTED",
                        "warning",
                        f"Potential secret matched rule {rule_id!r} but has an exact exemption.",
                        path,
                        fingerprint,
                    )
                )
            else:
                issues.append(
                    ScanIssue(
                        "SECRET_DETECTED",
                        "error",
                        f"Potential secret matched rule {rule_id!r}; matched value is redacted.",
                        path,
                        fingerprint,
                    )
                )
            finding_count += 1
            if finding_count >= policy.max_secret_findings:
                issues.append(
                    ScanIssue(
                        "SECRET_FINDING_LIMIT_EXCEEDED",
                        "error",
                        "Secret finding limit was reached; scan failed closed.",
                    )
                )
                return


def _secret_fingerprint(path: str, rule_id: str, matched_value: bytes) -> str:
    value_digest = hashlib.sha256(matched_value).hexdigest()
    material = f"skillhub-secret-v1\0{path}\0{rule_id}\0{value_digest}".encode()
    return f"secret:v1:{hashlib.sha256(material).hexdigest()}"


def _warn_about_scripts(
    path: str,
    info: zipfile.ZipInfo,
    policy: ScanPolicy,
    issues: list[ScanIssue],
) -> None:
    if not policy.warn_on_scripts:
        return
    pure_path = PurePosixPath(path)
    is_scripts_directory = any(part.casefold() == "scripts" for part in pure_path.parts[:-1])
    mode = info.external_attr >> 16
    executable = bool(mode & 0o111)
    if is_scripts_directory or pure_path.suffix.casefold() in _SCRIPT_EXTENSIONS or executable:
        issues.append(
            ScanIssue(
                "SCRIPT_PRESENT",
                "error" if policy.scripts_are_errors else "warning",
                "Package contains script-like or executable content; it was not executed.",
                path,
            )
        )


def _media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path, strict=False)
    if guessed:
        return guessed
    if path.casefold().endswith(".md"):
        return "text/markdown"
    return "application/octet-stream"


def _sha256_file(path: Path) -> tuple[str, _FileIdentity]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        identity = _stat_identity(os.fstat(source.fileno()))
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError("archive descriptor is not a regular file")
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
        if _stat_identity(os.fstat(source.fileno())) != identity:
            raise OSError("archive changed while being hashed")
    return digest.hexdigest(), identity


def _zip_file_identity(archive: zipfile.ZipFile) -> _FileIdentity | None:
    if archive.fp is None:
        return None
    try:
        return _stat_identity(os.fstat(archive.fp.fileno()))
    except (AttributeError, OSError):
        return None


def _stat_identity(value: os.stat_result) -> _FileIdentity:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _constant_time_digest_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _compression_ratio(uncompressed: int, compressed: int) -> float:
    if uncompressed == 0:
        return 0.0
    if compressed == 0:
        return float("inf")
    return uncompressed / compressed


def _has_errors(issues: Iterable[ScanIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def _result(
    namespace: str,
    name: str,
    version: str,
    artifact_sha256: str,
    scanned_files: list[ScannedFile],
    issues: list[ScanIssue],
    description: str | None,
    license_id: str | None,
    skill_root: str | None,
    signature_key_id: str | None,
    signature_record: dict[str, object] | None,
    *,
    compatibility: list[str] | None = None,
) -> ScanResult:
    ordered_files = tuple(sorted(scanned_files, key=lambda item: item.path))
    passed = not _has_errors(issues)
    has_warnings = any(issue.severity == "warning" for issue in issues)
    scan_status = "failed" if not passed else "passed_with_warnings" if has_warnings else "passed"
    manifest: dict[str, object] | None = None
    if passed and description is not None and license_id is not None:
        manifest = {
            "schema_version": "1.0",
            "namespace": namespace,
            "name": name,
            "version": version,
            "description": description,
            "compatibility": compatibility or [],
            "files": [
                {
                    "path": item.path,
                    "size": item.size,
                    "sha256": item.sha256,
                    "media_type": item.media_type,
                }
                for item in ordered_files
            ],
            "artifact_sha256": artifact_sha256,
            "license": license_id,
            "signatures": [signature_record] if signature_record is not None else [],
            "scan_status": scan_status,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    return ScanResult(
        passed=passed,
        scan_status=scan_status,
        namespace=namespace,
        name=name,
        version=version,
        artifact_sha256=artifact_sha256,
        files=ordered_files,
        issues=tuple(issues),
        manifest=manifest,
        description=description,
        license=license_id,
        skill_root=skill_root,
        signature_key_id=signature_key_id,
    )
