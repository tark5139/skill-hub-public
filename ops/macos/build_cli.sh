#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  DEFAULT_PYTHON="$PROJECT_DIR/.venv/bin/python"
else
  DEFAULT_PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
fi
if [ -x "$PROJECT_DIR/.venv/bin/pyinstaller" ]; then
  DEFAULT_PYINSTALLER="$PROJECT_DIR/.venv/bin/pyinstaller"
else
  DEFAULT_PYINSTALLER=$(command -v pyinstaller 2>/dev/null || true)
fi
PYTHON=${PYTHON:-$DEFAULT_PYTHON}
PYINSTALLER=${PYINSTALLER:-$DEFAULT_PYINSTALLER}
VERIFY_SCRIPT="$PROJECT_DIR/ops/macos/verify_cli.sh"
SOURCE_ROOT="$PROJECT_DIR/src"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  printf '%s\n' 'The official first-release client must be built on Apple Silicon macOS.' >&2
  exit 69
fi
if [ "$PYTHON" = "" ] || { [ ! -x "$PYTHON" ] && ! command -v "$PYTHON" >/dev/null 2>&1; }; then
  printf '%s\n' 'Python is missing; install Python 3.12 and the project dependencies first.' >&2
  exit 69
fi
if [ "$PYINSTALLER" = "" ] || { [ ! -x "$PYINSTALLER" ] && ! command -v "$PYINSTALLER" >/dev/null 2>&1; }; then
  printf '%s\n' 'PyInstaller is missing; install the dev dependencies first.' >&2
  exit 69
fi
if [ ! -x "$VERIFY_SCRIPT" ]; then
  printf '%s\n' 'The macOS signature verification helper is missing or not executable.' >&2
  exit 69
fi

if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  case "$SKILLHUB_CODESIGN_IDENTITY" in
    'Developer ID Application: '*) ;;
    *)
      printf '%s\n' 'Official builds require an exact Developer ID Application identity.' >&2
      exit 65
      ;;
  esac
  : "${SKILLHUB_EXPECTED_TEAM_ID:?set the expected Apple Developer Team ID}"
  security find-identity -v -p codesigning | grep -F "$SKILLHUB_CODESIGN_IDENTITY" >/dev/null || {
    printf 'The requested signing identity is not available: %s\n' \
      "$SKILLHUB_CODESIGN_IDENTITY" >&2
    exit 65
  }
fi

VERSION=$(PYTHONPATH="$SOURCE_ROOT" \
  "$PYTHON" -c 'from skillhub import __version__; print(__version__)')
BUILD_ROOT="$PROJECT_DIR/build/macos-arm64"
STEM="skillctl-$VERSION-macos13-arm64"
STAGE="$BUILD_ROOT/$STEM"
ARTIFACT_DIR="$PROJECT_DIR/artifacts"
if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  ARCHIVE="$ARTIFACT_DIR/$STEM-signed-unnotarized.zip"
else
  ARCHIVE="$ARTIFACT_DIR/$STEM-adhoc.zip"
fi

mkdir -p "$ARTIFACT_DIR"
if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  for existing in \
    "$ARCHIVE" \
    "$ARCHIVE.sha256" \
    "$ARTIFACT_DIR/$STEM-signed-unnotarized.dmg" \
    "$ARTIFACT_DIR/$STEM-signed-unnotarized.dmg.sha256" \
    "$ARTIFACT_DIR/$STEM.zip" \
    "$ARTIFACT_DIR/$STEM.zip.sha256" \
    "$ARTIFACT_DIR/$STEM.dmg" \
    "$ARTIFACT_DIR/$STEM.dmg.sha256" \
    "$ARTIFACT_DIR/$STEM".notary-*.json; do
    if [ -e "$existing" ] || [ -L "$existing" ]; then
      printf 'Refusing to overwrite an existing official artifact or its evidence: %s\n' \
        "$existing" >&2
      exit 73
    fi
  done
else
  rm -f "$ARCHIVE" "$ARCHIVE.sha256"
fi

rm -rf "$BUILD_ROOT"
mkdir -p "$STAGE"
PYINSTALLER_CONFIG_DIR=${PYINSTALLER_CONFIG_DIR:-"$BUILD_ROOT/config"}
MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-13.0}
export PYINSTALLER_CONFIG_DIR
export MACOSX_DEPLOYMENT_TARGET

set -- \
  --clean \
  --noconfirm \
  --onefile \
  --name skillctl \
  --target-architecture arm64 \
  --distpath "$BUILD_ROOT/dist" \
  --workpath "$BUILD_ROOT/work" \
  --specpath "$BUILD_ROOT/spec" \
  --paths "$SOURCE_ROOT" \
  --collect-submodules skillhub.adapters
if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  set -- "$@" --codesign-identity "$SKILLHUB_CODESIGN_IDENTITY"
fi
set -- "$@" "$PROJECT_DIR/ops/macos/skillctl_entry.py"
PYTHONPATH="$SOURCE_ROOT" "$PYINSTALLER" "$@"

smoke_test() {
  binary=$1
  observed_version=$("$binary" --version)
  if [ "$observed_version" != "$VERSION" ]; then
    printf 'Built CLI version mismatch: expected %s, observed %s\n' \
      "$VERSION" "$observed_version" >&2
    exit 65
  fi
  "$binary" --help >/dev/null
}
smoke_test "$BUILD_ROOT/dist/skillctl"

if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  codesign --force --timestamp --options runtime \
    --sign "$SKILLHUB_CODESIGN_IDENTITY" "$BUILD_ROOT/dist/skillctl"
  SKILLHUB_REQUIRE_DEVELOPER_ID=1 "$VERIFY_SCRIPT" "$BUILD_ROOT/dist/skillctl"
  SIGNING_MODE=developer-id
else
  codesign --force --sign - "$BUILD_ROOT/dist/skillctl"
  codesign --verify --strict --verbose=2 "$BUILD_ROOT/dist/skillctl"
  SIGNING_MODE=adhoc
fi

# Exercise the final, signed Mach-O. This is intentionally repeated after signing.
smoke_test "$BUILD_ROOT/dist/skillctl"

cp "$BUILD_ROOT/dist/skillctl" "$STAGE/skillctl"
cp "$PROJECT_DIR/LICENSE" "$STAGE/LICENSE"
cp "$PROJECT_DIR/ops/macos/INSTALL.txt" "$STAGE/INSTALL.txt"
printf '%s\n' "$SIGNING_MODE" > "$STAGE/SIGNING_MODE"

ARCHIVE_PARTIAL="$ARTIFACT_DIR/.$(basename "$ARCHIVE").$$.partial.zip"
CHECKSUM_PARTIAL="$ARTIFACT_DIR/.$(basename "$ARCHIVE").sha256.$$.partial"
cleanup_partial() {
  rm -f "$ARCHIVE_PARTIAL" "$CHECKSUM_PARTIAL"
}
trap cleanup_partial EXIT HUP INT TERM
(cd "$BUILD_ROOT" && /usr/bin/zip -X -q -r "$ARCHIVE_PARTIAL" "$(basename "$STAGE")")
ARCHIVE_DIGEST=$(shasum -a 256 "$ARCHIVE_PARTIAL" | awk '{print $1}')
printf '%s  %s\n' "$ARCHIVE_DIGEST" "$(basename "$ARCHIVE")" > "$CHECKSUM_PARTIAL"
mv "$CHECKSUM_PARTIAL" "$ARCHIVE.sha256"
# The archive rename is the commit point: whenever it exists, its checksum does too.
mv "$ARCHIVE_PARTIAL" "$ARCHIVE"
trap - EXIT HUP INT TERM

printf 'Created %s (%s signing)\n' "$ARCHIVE" "$SIGNING_MODE"
