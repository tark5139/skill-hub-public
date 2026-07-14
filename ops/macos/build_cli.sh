#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHON=${PYTHON:-"$PROJECT_DIR/.venv/bin/python"}
PYINSTALLER=${PYINSTALLER:-"$PROJECT_DIR/.venv/bin/pyinstaller"}
VERSION=$("$PYTHON" -c 'from skillhub import __version__; print(__version__)')
BUILD_ROOT="$PROJECT_DIR/build/macos-arm64"
STAGE="$BUILD_ROOT/skillctl-$VERSION-macos13-arm64"
ARTIFACT_DIR="$PROJECT_DIR/artifacts"
if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  ARCHIVE="$ARTIFACT_DIR/skillctl-$VERSION-macos13-arm64-signed-unnotarized.zip"
else
  ARCHIVE="$ARTIFACT_DIR/skillctl-$VERSION-macos13-arm64-adhoc.zip"
fi

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  printf '%s\n' 'The official first-release client must be built on Apple Silicon macOS.' >&2
  exit 69
fi
if [ ! -x "$PYINSTALLER" ]; then
  printf '%s\n' 'PyInstaller is missing; install the dev dependencies first.' >&2
  exit 69
fi

rm -rf "$BUILD_ROOT"
mkdir -p "$STAGE" "$ARTIFACT_DIR"
PYINSTALLER_CONFIG_DIR=${PYINSTALLER_CONFIG_DIR:-"$BUILD_ROOT/config"}
export PYINSTALLER_CONFIG_DIR

"$PYINSTALLER" \
  --clean \
  --noconfirm \
  --onefile \
  --name skillctl \
  --target-architecture arm64 \
  --distpath "$BUILD_ROOT/dist" \
  --workpath "$BUILD_ROOT/work" \
  --specpath "$BUILD_ROOT/spec" \
  --collect-submodules skillhub.adapters \
  "$PROJECT_DIR/ops/macos/skillctl_entry.py"

"$BUILD_ROOT/dist/skillctl" --version
"$BUILD_ROOT/dist/skillctl" --help >/dev/null

if [ "${SKILLHUB_CODESIGN_IDENTITY:-}" != "" ]; then
  codesign --force --timestamp --options runtime \
    --sign "$SKILLHUB_CODESIGN_IDENTITY" "$BUILD_ROOT/dist/skillctl"
  SIGNING_MODE=developer-id-unnotarized
else
  codesign --force --sign - "$BUILD_ROOT/dist/skillctl"
  SIGNING_MODE=adhoc
fi
codesign --verify --strict --verbose=2 "$BUILD_ROOT/dist/skillctl"

cp "$BUILD_ROOT/dist/skillctl" "$STAGE/skillctl"
cp "$PROJECT_DIR/LICENSE" "$STAGE/LICENSE"
cp "$PROJECT_DIR/ops/macos/INSTALL.txt" "$STAGE/INSTALL.txt"
printf '%s\n' "$SIGNING_MODE" > "$STAGE/SIGNING_MODE"

rm -f "$ARCHIVE" "$ARCHIVE.sha256"
(cd "$BUILD_ROOT" && /usr/bin/zip -X -q -r "$ARCHIVE" "$(basename "$STAGE")")
(cd "$ARTIFACT_DIR" && shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")

printf 'Created %s (%s signing)\n' "$ARCHIVE" "$SIGNING_MODE"
