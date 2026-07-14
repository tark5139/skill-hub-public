#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: SKILLHUB_CODESIGN_IDENTITY=<Developer ID> %s <signed-unnotarized.zip>\n' \
    "$0" >&2
  exit 64
fi

: "${SKILLHUB_CODESIGN_IDENTITY:?set the Developer ID Application identity}"
: "${SKILLHUB_EXPECTED_TEAM_ID:?set the expected Apple Developer Team ID}"

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VERIFY_SCRIPT="$PROJECT_DIR/ops/macos/verify_cli.sh"
INPUT=$1
INPUT_DIR=$(CDPATH= cd -- "$(dirname -- "$INPUT")" && pwd)
INPUT="$INPUT_DIR/$(basename "$INPUT")"
BASE=$(basename "$INPUT")

case "$BASE" in
  skillctl-*-macos13-arm64-signed-unnotarized.zip) ;;
  *)
    printf '%s\n' 'DMG input must be a Developer ID-signed, unnotarized skillctl ZIP.' >&2
    exit 65
    ;;
esac
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  printf '%s\n' 'The official DMG must be created on Apple Silicon macOS.' >&2
  exit 69
fi
case "$SKILLHUB_CODESIGN_IDENTITY" in
  'Developer ID Application: '*) ;;
  *)
    printf '%s\n' 'DMG signing requires an exact Developer ID Application identity.' >&2
    exit 65
    ;;
esac
if [ ! -x "$VERIFY_SCRIPT" ]; then
  printf '%s\n' 'The macOS signature verification helper is missing or not executable.' >&2
  exit 69
fi
if [ ! -s "$INPUT" ] || [ -L "$INPUT" ]; then
  printf 'Input is not a regular non-empty ZIP: %s\n' "$INPUT" >&2
  exit 66
fi
INPUT_CHECKSUM="$INPUT.sha256"
if [ ! -s "$INPUT_CHECKSUM" ] || [ -L "$INPUT_CHECKSUM" ]; then
  printf 'Signed ZIP checksum is unavailable: %s\n' "$INPUT_CHECKSUM" >&2
  exit 66
fi
CHECKSUM_RECORDS=$(awk 'NF {count += 1} END {print count + 0}' "$INPUT_CHECKSUM")
EXPECTED_INPUT_DIGEST=$(awk 'NR == 1 {print $1}' "$INPUT_CHECKSUM")
CHECKSUM_INPUT_NAME=$(awk 'NR == 1 {print $2}' "$INPUT_CHECKSUM" | sed 's/^\*//')
case "$EXPECTED_INPUT_DIGEST" in
  *[!0-9A-Fa-f]* | '')
    printf 'Signed ZIP checksum is malformed: %s\n' "$INPUT_CHECKSUM" >&2
    exit 65
    ;;
esac
if [ "$CHECKSUM_RECORDS" -ne 1 ] || \
   [ "${#EXPECTED_INPUT_DIGEST}" -ne 64 ] || \
   [ "$CHECKSUM_INPUT_NAME" != "$BASE" ]; then
  printf 'Signed ZIP checksum does not contain one record for the exact input: %s\n' \
    "$INPUT_CHECKSUM" >&2
  exit 65
fi
ACTUAL_INPUT_DIGEST=$(shasum -a 256 "$INPUT" | awk '{print $1}')
if [ "$ACTUAL_INPUT_DIGEST" != "$EXPECTED_INPUT_DIGEST" ]; then
  printf 'Signed ZIP checksum mismatch for %s.\n' "$INPUT" >&2
  exit 65
fi

STEM=${BASE%-signed-unnotarized.zip}
VERSION=${STEM#skillctl-}
VERSION=${VERSION%-macos13-arm64}
OUTPUT="$INPUT_DIR/$STEM-signed-unnotarized.dmg"
if [ -e "$OUTPUT" ] || [ -L "$OUTPUT" ] || \
   [ -e "$OUTPUT.sha256" ] || [ -L "$OUTPUT.sha256" ]; then
  printf 'Refusing to overwrite an existing DMG or checksum: %s\n' "$OUTPUT" >&2
  exit 73
fi

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/skillhub-dmg.XXXXXX")
PUBLISH_CANDIDATE="$INPUT_DIR/.$STEM.$$.partial.dmg"
CHECKSUM_CANDIDATE="$INPUT_DIR/.$STEM-signed-unnotarized.dmg.sha256.$$.partial"
cleanup() {
  rm -f "$PUBLISH_CANDIDATE" "$CHECKSUM_CANDIDATE"
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$WORK_DIR/payload"
/usr/bin/ditto -x -k "$INPUT" "$WORK_DIR/payload"
BINARY="$WORK_DIR/payload/$STEM/skillctl"
MODE_FILE="$WORK_DIR/payload/$STEM/SIGNING_MODE"
if [ ! -f "$BINARY" ] || [ ! -f "$MODE_FILE" ]; then
  printf '%s\n' 'The signed ZIP does not contain the expected skillctl layout.' >&2
  exit 65
fi
if [ "$(sed -n '1p' "$MODE_FILE")" != "developer-id" ]; then
  printf '%s\n' 'The signed ZIP is not marked as a Developer ID build.' >&2
  exit 65
fi
SKILLHUB_REQUIRE_DEVELOPER_ID=1 "$VERIFY_SCRIPT" "$BINARY"

hdiutil create \
  -quiet \
  -format UDZO \
  -volname "Skill Hub CLI $VERSION" \
  -srcfolder "$WORK_DIR/payload" \
  "$PUBLISH_CANDIDATE"
codesign --force --timestamp --sign "$SKILLHUB_CODESIGN_IDENTITY" "$PUBLISH_CANDIDATE"
codesign --verify --strict --verbose=2 "$PUBLISH_CANDIDATE"

DMG_DETAILS=$(codesign --display --verbose=4 "$PUBLISH_CANDIDATE" 2>&1)
printf '%s\n' "$DMG_DETAILS" | grep -F 'Authority=Developer ID Application:' >/dev/null || {
  printf '%s\n' 'DMG is not signed with a Developer ID Application certificate.' >&2
  exit 65
}
OBSERVED_IDENTITY=$(printf '%s\n' "$DMG_DETAILS" | sed -n 's/^Authority=//p' | head -n 1)
if [ "$OBSERVED_IDENTITY" != "$SKILLHUB_CODESIGN_IDENTITY" ]; then
  printf 'DMG signing identity mismatch: expected %s, observed %s\n' \
    "$SKILLHUB_CODESIGN_IDENTITY" "${OBSERVED_IDENTITY:-<missing>}" >&2
  exit 65
fi
OBSERVED_TEAM_ID=$(printf '%s\n' "$DMG_DETAILS" | sed -n 's/^TeamIdentifier=//p' | head -n 1)
if [ "$OBSERVED_TEAM_ID" != "$SKILLHUB_EXPECTED_TEAM_ID" ]; then
  printf 'DMG Team ID mismatch: expected %s, observed %s\n' \
    "$SKILLHUB_EXPECTED_TEAM_ID" "${OBSERVED_TEAM_ID:-<missing>}" >&2
  exit 65
fi
printf '%s\n' "$DMG_DETAILS" | grep -E '^Timestamp=' >/dev/null || {
  printf '%s\n' 'DMG signature has no secure timestamp.' >&2
  exit 65
}

DIGEST=$(shasum -a 256 "$PUBLISH_CANDIDATE" | awk '{print $1}')
printf '%s  %s\n' "$DIGEST" "$(basename "$OUTPUT")" > "$CHECKSUM_CANDIDATE"
mv "$CHECKSUM_CANDIDATE" "$OUTPUT.sha256"
# The suffix-bearing signed DMG is the commit point for this intermediate pair.
mv "$PUBLISH_CANDIDATE" "$OUTPUT"
printf 'Created signed, unnotarized DMG: %s\n' "$OUTPUT"
