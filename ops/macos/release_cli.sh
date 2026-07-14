#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  DEFAULT_PYTHON="$PROJECT_DIR/.venv/bin/python"
else
  DEFAULT_PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
fi
PYTHON=${PYTHON:-$DEFAULT_PYTHON}
FORMAT=${SKILLHUB_RELEASE_FORMAT:-zip}

: "${SKILLHUB_CODESIGN_IDENTITY:?set the exact Developer ID Application identity}"
: "${SKILLHUB_EXPECTED_TEAM_ID:?set the expected Apple Developer Team ID}"
: "${SKILLHUB_EXPECTED_COMMIT:?set the expected full 40-character source commit}"
: "${SKILLHUB_EXPECTED_REF:?set the exact refs/heads/... or refs/tags/... source ref}"
: "${SKILLHUB_EXPECTED_VERSION:?set the exact project version}"

case "$SKILLHUB_CODESIGN_IDENTITY" in
  'Developer ID Application: '?*) ;;
  *)
    printf '%s\n' 'Official releases require an exact Developer ID Application identity.' >&2
    exit 65
    ;;
esac

case "$FORMAT" in
  zip | dmg | both) ;;
  *)
    printf '%s\n' 'SKILLHUB_RELEASE_FORMAT must be zip, dmg, or both.' >&2
    exit 64
    ;;
esac
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  printf '%s\n' 'The official release workflow requires Apple Silicon macOS.' >&2
  exit 69
fi
for command in codesign git security shasum xcrun; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required release command is unavailable: %s\n' "$command" >&2
    exit 69
  }
done
if [ "$FORMAT" = "dmg" ] || [ "$FORMAT" = "both" ]; then
  command -v spctl >/dev/null 2>&1 || {
    printf '%s\n' 'spctl is required to assess a release DMG.' >&2
    exit 69
  }
  spctl --status | grep -F 'assessments enabled' >/dev/null || {
    printf '%s\n' 'Gatekeeper assessments must be enabled to validate a release DMG.' >&2
    exit 65
  }
fi
if [ "$PYTHON" = "" ] || { [ ! -x "$PYTHON" ] && ! command -v "$PYTHON" >/dev/null 2>&1; }; then
  printf '%s\n' 'Python is unavailable; install the project build dependencies first.' >&2
  exit 69
fi
for script in build_cli.sh create_dmg.sh notarize_cli.sh verify_cli.sh; do
  if [ ! -x "$PROJECT_DIR/ops/macos/$script" ]; then
    printf 'Required release helper is missing or not executable: %s\n' "$script" >&2
    exit 69
  fi
done

case "$SKILLHUB_EXPECTED_COMMIT" in
  *[!0-9A-Fa-f]* | '')
    printf '%s\n' 'SKILLHUB_EXPECTED_COMMIT must be exactly 40 hexadecimal characters.' >&2
    exit 64
    ;;
esac
if [ "${#SKILLHUB_EXPECTED_COMMIT}" -ne 40 ]; then
  printf '%s\n' 'SKILLHUB_EXPECTED_COMMIT must be exactly 40 hexadecimal characters.' >&2
  exit 64
fi
EXPECTED_COMMIT=$(printf '%s' "$SKILLHUB_EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')
case "$SKILLHUB_EXPECTED_REF" in
  refs/heads/* | refs/tags/*) ;;
  *)
    printf '%s\n' 'SKILLHUB_EXPECTED_REF must be a full branch or tag ref.' >&2
    exit 64
    ;;
esac
printf '%s\n' "$SKILLHUB_EXPECTED_VERSION" \
  | grep -E '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$' >/dev/null || {
    printf '%s\n' 'SKILLHUB_EXPECTED_VERSION is not a safe semantic version.' >&2
    exit 64
  }

git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  printf '%s\n' 'Official releases must run from a Git worktree.' >&2
  exit 65
}
HEAD_COMMIT=$(git -C "$PROJECT_DIR" rev-parse HEAD | tr '[:upper:]' '[:lower:]')
if [ "$HEAD_COMMIT" != "$EXPECTED_COMMIT" ]; then
  printf 'Source commit mismatch: expected %s, observed %s\n' \
    "$EXPECTED_COMMIT" "$HEAD_COMMIT" >&2
  exit 65
fi
REF_COMMIT=$(git -C "$PROJECT_DIR" rev-parse --verify \
  "$SKILLHUB_EXPECTED_REF^{commit}" 2>/dev/null) || {
    printf 'Expected source ref does not exist: %s\n' "$SKILLHUB_EXPECTED_REF" >&2
    exit 65
  }
REF_COMMIT=$(printf '%s' "$REF_COMMIT" | tr '[:upper:]' '[:lower:]')
if [ "$REF_COMMIT" != "$EXPECTED_COMMIT" ]; then
  printf 'Source ref %s resolves to %s, not %s.\n' \
    "$SKILLHUB_EXPECTED_REF" "$REF_COMMIT" "$EXPECTED_COMMIT" >&2
  exit 65
fi
case "$SKILLHUB_EXPECTED_REF" in
  refs/heads/*)
    CURRENT_REF=$(git -C "$PROJECT_DIR" symbolic-ref -q HEAD 2>/dev/null || true)
    if [ "$CURRENT_REF" != "$SKILLHUB_EXPECTED_REF" ]; then
      printf 'Checked-out branch mismatch: expected %s, observed %s\n' \
        "$SKILLHUB_EXPECTED_REF" "${CURRENT_REF:-detached HEAD}" >&2
      exit 65
    fi
    ;;
  refs/tags/*) ;;
esac
if [ "$(git -C "$PROJECT_DIR" status --porcelain=v1 --untracked-files=all)" != "" ]; then
  printf '%s\n' 'Official releases require a clean worktree, including no untracked files.' >&2
  exit 65
fi

DECLARED_VERSION=$("$PYTHON" -c \
  'import pathlib,sys,tomllib; print(tomllib.loads((pathlib.Path(sys.argv[1]) / "pyproject.toml").read_text())["project"]["version"])' \
  "$PROJECT_DIR")
SOURCE_ROOT="$PROJECT_DIR/src"
PYTHONPATH="$SOURCE_ROOT" "$PYTHON" -c \
  'import pathlib,sys,skillhub,skillhub_cli
root=pathlib.Path(sys.argv[1]).resolve()
for module in (skillhub, skillhub_cli):
    source=pathlib.Path(module.__file__).resolve()
    if not source.is_relative_to(root):
        raise SystemExit(f"{module.__name__} resolved outside reviewed source: {source}")' \
  "$SOURCE_ROOT" || {
    printf '%s\n' 'The CLI modules do not resolve from the reviewed source tree.' >&2
    exit 65
  }
VERSION=$(PYTHONPATH="$SOURCE_ROOT" \
  "$PYTHON" -c 'from skillhub import __version__; print(__version__)')
if [ "$DECLARED_VERSION" != "$SKILLHUB_EXPECTED_VERSION" ] || \
   [ "$VERSION" != "$SKILLHUB_EXPECTED_VERSION" ]; then
  printf 'Version mismatch: expected %s, pyproject=%s, installed=%s\n' \
    "$SKILLHUB_EXPECTED_VERSION" "$DECLARED_VERSION" "$VERSION" >&2
  exit 65
fi

PROFILE=${SKILLHUB_NOTARY_PROFILE:-}
API_KEY=${SKILLHUB_NOTARY_KEY:-}
API_KEY_ID=${SKILLHUB_NOTARY_KEY_ID:-}
API_ISSUER=${SKILLHUB_NOTARY_ISSUER:-}
if [ "$PROFILE" != "" ] && { [ "$API_KEY" != "" ] || [ "$API_KEY_ID" != "" ] || [ "$API_ISSUER" != "" ]; }; then
  printf '%s\n' 'Choose one notary authentication mode, not both.' >&2
  exit 64
fi
if [ "$PROFILE" = "" ] && { [ "$API_KEY" = "" ] || [ "$API_KEY_ID" = "" ] || [ "$API_ISSUER" = "" ]; }; then
  printf '%s\n' 'Configure a notary keychain profile or a complete team API key.' >&2
  exit 64
fi
if [ "$PROFILE" = "" ]; then
  if [ ! -f "$API_KEY" ] || [ -L "$API_KEY" ]; then
    printf 'Notary API private key is unavailable: %s\n' "$API_KEY" >&2
    exit 66
  fi
  API_KEY_MODE=$(stat -f '%Lp' "$API_KEY")
  case "$API_KEY_MODE" in
    400 | 600) ;;
    *)
      printf 'Notary API private key permissions must be 0400 or 0600, observed %s.\n' \
        "$API_KEY_MODE" >&2
      exit 65
      ;;
  esac
fi

security find-identity -v -p codesigning | grep -F "$SKILLHUB_CODESIGN_IDENTITY" >/dev/null || {
  printf 'Developer ID identity is unavailable: %s\n' "$SKILLHUB_CODESIGN_IDENTITY" >&2
  exit 65
}

ARTIFACT_DIR="$PROJECT_DIR/artifacts"
SIGNED_ZIP="$ARTIFACT_DIR/skillctl-$VERSION-macos13-arm64-signed-unnotarized.zip"
SIGNED_DMG="$ARTIFACT_DIR/skillctl-$VERSION-macos13-arm64-signed-unnotarized.dmg"
if [ "${SKILLHUB_PROVENANCE_FILE:-}" != "" ]; then
  case "$SKILLHUB_PROVENANCE_FILE" in
    /*) PROVENANCE_FILE=$SKILLHUB_PROVENANCE_FILE ;;
    *) PROVENANCE_FILE="$PROJECT_DIR/$SKILLHUB_PROVENANCE_FILE" ;;
  esac
else
  PROVENANCE_FILE="$ARTIFACT_DIR/skillctl-$VERSION-macos13-arm64.provenance.json"
fi
if [ -L "$PROVENANCE_FILE" ]; then
  printf 'Refusing a symlink provenance target: %s\n' "$PROVENANCE_FILE" >&2
  exit 65
fi
if [ -e "$PROVENANCE_FILE" ] && [ "${GITHUB_ACTIONS:-}" != "true" ]; then
  printf 'Refusing to overwrite prior local release provenance: %s\n' \
    "$PROVENANCE_FILE" >&2
  exit 73
fi
mkdir -p "$ARTIFACT_DIR"

SKILLHUB_PROVENANCE_FILE=$PROVENANCE_FILE
SKILLHUB_SOURCE_SHA=$EXPECTED_COMMIT
SKILLHUB_SOURCE_REF=$SKILLHUB_EXPECTED_REF
SKILLHUB_ARTIFACT_DIR=$ARTIFACT_DIR
SKILLHUB_PROJECT_DIR=$PROJECT_DIR
SKILLHUB_RELEASE_FORMAT=$FORMAT
export SKILLHUB_PROVENANCE_FILE SKILLHUB_SOURCE_SHA SKILLHUB_SOURCE_REF
export SKILLHUB_EXPECTED_VERSION SKILLHUB_RELEASE_FORMAT SKILLHUB_ARTIFACT_DIR SKILLHUB_PROJECT_DIR

PROVENANCE_READY=0
RELEASE_COMPLETED=0
finalize_release() {
  release_rc=$?
  trap - EXIT HUP INT TERM
  if [ "$PROVENANCE_READY" != "1" ]; then
    exit "$release_rc"
  fi

  if [ "$release_rc" -eq 0 ] && [ "$RELEASE_COMPLETED" = "1" ]; then
    SKILLHUB_RELEASE_FINAL_STATUS=success
  else
    SKILLHUB_RELEASE_FINAL_STATUS=failed
  fi
  SKILLHUB_RELEASE_EXIT_CODE=$release_rc
  export SKILLHUB_RELEASE_FINAL_STATUS SKILLHUB_RELEASE_EXIT_CODE

  set +e
  "$PYTHON" - <<'PY'
import datetime
import hashlib
import json
import os
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


target = Path(os.environ["SKILLHUB_PROVENANCE_FILE"])
artifact_dir = Path(os.environ["SKILLHUB_ARTIFACT_DIR"])
version = os.environ["SKILLHUB_EXPECTED_VERSION"]
stem = f"skillctl-{version}-macos13-arm64"
value = json.loads(target.read_text(encoding="utf-8"))
expected_bindings = {
    "source_commit": os.environ["SKILLHUB_SOURCE_SHA"],
    "source_ref": os.environ["SKILLHUB_SOURCE_REF"],
    "version": version,
    "requested_format": os.environ["SKILLHUB_RELEASE_FORMAT"],
}
for field, expected in expected_bindings.items():
    if value.get(field) != expected:
        raise SystemExit(f"provenance binding changed for {field}")

errors = []
artifacts = []
for artifact_format in ("zip", "dmg"):
    path = artifact_dir / f"{stem}.{artifact_format}"
    if not path.exists():
        continue
    if path.is_symlink() or not path.is_file():
        errors.append(f"unsafe public artifact: {path.name}")
        continue
    actual_digest = digest(path)
    sidecar = path.with_name(path.name + ".sha256")
    checksum_valid = False
    if sidecar.is_file() and not sidecar.is_symlink():
        fields = sidecar.read_text(encoding="utf-8").split()
        checksum_valid = (
            len(fields) == 2
            and fields[0].lower() == actual_digest
            and fields[1].lstrip("*") == path.name
        )
    if not checksum_valid:
        errors.append(f"missing or invalid checksum for {path.name}")
    artifacts.append({
        "format": artifact_format,
        "name": path.name,
        "sha256": actual_digest,
        "size": path.stat().st_size,
        "checksum_file": sidecar.name if sidecar.exists() else None,
        "checksum_valid": checksum_valid,
    })

submissions = []
for artifact_format in ("zip", "dmg"):
    result_path = artifact_dir / f"{stem}.notary-{artifact_format}.json"
    if not result_path.exists():
        continue
    if result_path.is_symlink() or not result_path.is_file():
        errors.append(f"unsafe notarization evidence: {result_path.name}")
        continue
    entry = {
        "format": artifact_format,
        "evidence": result_path.name,
        "sha256": digest(result_path),
    }
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        entry.update({"id": result.get("id"), "status": result.get("status")})
    except (OSError, ValueError) as error:
        entry["parse_error"] = str(error)
    log_path = artifact_dir / f"{stem}.notary-{artifact_format}-log.json"
    if log_path.is_file() and not log_path.is_symlink():
        entry.update({"log": log_path.name, "log_sha256": digest(log_path)})
    submissions.append(entry)

submitted_inputs = []
for artifact_format in ("zip", "dmg"):
    path = artifact_dir / f"{stem}-signed-unnotarized.{artifact_format}"
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.exists():
        continue
    if sidecar.is_symlink() or not sidecar.is_file():
        submitted_inputs.append({
            "format": artifact_format,
            "artifact": path.name,
            "checksum_file": sidecar.name,
            "sha256": None,
            "checksum_valid": False,
        })
        continue
    fields = sidecar.read_text(encoding="utf-8").split()
    recorded = fields[0].lower() if len(fields) == 2 else None
    valid = (
        path.is_file()
        and not path.is_symlink()
        and len(fields) == 2
        and fields[1].lstrip("*") == path.name
        and recorded == digest(path)
    )
    submitted_inputs.append({
        "format": artifact_format,
        "artifact": path.name,
        "checksum_file": sidecar.name,
        "sha256": recorded,
        "checksum_valid": valid,
    })

final_status = os.environ["SKILLHUB_RELEASE_FINAL_STATUS"]
if final_status == "success":
    requested = os.environ["SKILLHUB_RELEASE_FORMAT"]
    required_formats = {"zip", "dmg"} if requested == "both" else {requested}
    artifact_formats = {entry["format"] for entry in artifacts if entry["checksum_valid"]}
    accepted_formats = {
        entry["format"] for entry in submissions
        if entry.get("status") == "Accepted" and entry.get("id")
    }
    missing_artifacts = required_formats - artifact_formats
    missing_acceptance = required_formats - accepted_formats
    if missing_artifacts:
        errors.append("missing verified artifacts: " + ", ".join(sorted(missing_artifacts)))
    if missing_acceptance:
        errors.append("missing accepted notarization evidence: " + ", ".join(sorted(missing_acceptance)))

value.update({
    "status": "success" if final_status == "success" and not errors else "failed",
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "exit_code": int(os.environ["SKILLHUB_RELEASE_EXIT_CODE"]),
    "artifacts": artifacts,
    "notary_submissions": submissions,
    "submitted_inputs": submitted_inputs,
})
if errors:
    value["validation_errors"] = errors
else:
    value.pop("validation_errors", None)
temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(target)
if errors:
    raise SystemExit("; ".join(errors))
PY
  provenance_rc=$?
  if [ "$provenance_rc" -ne 0 ]; then
    printf 'Unable to finalize release provenance: %s\n' "$PROVENANCE_FILE" >&2
    exit 70
  fi
  exit "$release_rc"
}
trap finalize_release EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$PYTHON" - <<'PY'
import datetime
import hashlib
import json
import os
from pathlib import Path

target = Path(os.environ["SKILLHUB_PROVENANCE_FILE"])
project_dir = Path(os.environ["SKILLHUB_PROJECT_DIR"])
constraints_digest = hashlib.sha256((project_dir / "constraints.txt").read_bytes()).hexdigest()
bindings = {
    "schema": "skillhub.macos-release-provenance/v1",
    "source_commit": os.environ["SKILLHUB_SOURCE_SHA"],
    "source_ref": os.environ["SKILLHUB_SOURCE_REF"],
    "version": os.environ["SKILLHUB_EXPECTED_VERSION"],
    "requested_format": os.environ["SKILLHUB_RELEASE_FORMAT"],
    "constraints_sha256": constraints_digest,
}
if target.exists():
    value = json.loads(target.read_text(encoding="utf-8"))
    if value.get("status") != "prepared":
        raise SystemExit("existing CI provenance is not in prepared state")
    for field, expected in bindings.items():
        current = value.get(field)
        if current is not None and current != expected:
            raise SystemExit(f"existing provenance mismatch for {field}")
else:
    value = {}
value.update(bindings)
value.update({
    "status": "prepared",
    "source_worktree_clean": True,
    "invocation": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
})
value.setdefault("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
value.setdefault("artifacts", [])
value.setdefault("notary_submissions", [])
value.setdefault("submitted_inputs", [])
temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(target)
PY
PROVENANCE_READY=1

"$PROJECT_DIR/ops/macos/build_cli.sh"

if [ "$FORMAT" = "dmg" ] || [ "$FORMAT" = "both" ]; then
  "$PROJECT_DIR/ops/macos/create_dmg.sh" "$SIGNED_ZIP"
fi
if [ "$FORMAT" = "zip" ] || [ "$FORMAT" = "both" ]; then
  "$PROJECT_DIR/ops/macos/notarize_cli.sh" "$SIGNED_ZIP"
fi
if [ "$FORMAT" = "dmg" ] || [ "$FORMAT" = "both" ]; then
  "$PROJECT_DIR/ops/macos/notarize_cli.sh" "$SIGNED_DMG"
fi

RELEASE_COMPLETED=1
printf '%s\n' 'macOS release verification completed. No tag or GitHub Release was created.'
