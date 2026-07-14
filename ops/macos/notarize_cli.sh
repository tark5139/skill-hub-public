#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <signed-unnotarized.zip|signed-unnotarized.dmg>\n' "$0" >&2
  exit 64
fi

: "${SKILLHUB_EXPECTED_TEAM_ID:?set the expected Apple Developer Team ID}"
: "${SKILLHUB_CODESIGN_IDENTITY:?set the exact Developer ID Application identity}"
case "$SKILLHUB_CODESIGN_IDENTITY" in
  'Developer ID Application: '?*) ;;
  *)
    printf '%s\n' \
      'Notarization requires an exact Developer ID Application identity.' >&2
    exit 65
    ;;
esac

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VERIFY_SCRIPT="$PROJECT_DIR/ops/macos/verify_cli.sh"
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  DEFAULT_PYTHON="$PROJECT_DIR/.venv/bin/python"
else
  DEFAULT_PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
fi
PYTHON=${PYTHON:-$DEFAULT_PYTHON}
INPUT=$1
INPUT_DIR=$(CDPATH= cd -- "$(dirname -- "$INPUT")" && pwd)
INPUT="$INPUT_DIR/$(basename "$INPUT")"
BASE=$(basename "$INPUT")

case "$BASE" in
  skillctl-*-macos13-arm64-signed-unnotarized.zip)
    FORMAT=zip
    STEM=${BASE%-signed-unnotarized.zip}
    ;;
  skillctl-*-macos13-arm64-signed-unnotarized.dmg)
    FORMAT=dmg
    STEM=${BASE%-signed-unnotarized.dmg}
    ;;
  *)
    printf '%s\n' 'Refusing to notarize an artifact without the signed-unnotarized suffix.' >&2
    exit 65
    ;;
esac
if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' 'Apple notarization verification must run on macOS.' >&2
  exit 69
fi
if [ ! -s "$INPUT" ] || [ -L "$INPUT" ]; then
  printf 'Input is not a regular non-empty artifact: %s\n' "$INPUT" >&2
  exit 66
fi
INPUT_CHECKSUM="$INPUT.sha256"
if [ ! -s "$INPUT_CHECKSUM" ] || [ -L "$INPUT_CHECKSUM" ]; then
  printf 'Signed input checksum is unavailable: %s\n' "$INPUT_CHECKSUM" >&2
  exit 66
fi
CHECKSUM_LINE_COUNT=$(awk 'NF {count += 1} END {print count + 0}' "$INPUT_CHECKSUM")
if [ "$CHECKSUM_LINE_COUNT" -ne 1 ]; then
  printf 'Signed input checksum must contain exactly one non-empty record: %s\n' \
    "$INPUT_CHECKSUM" >&2
  exit 65
fi
EXPECTED_INPUT_DIGEST=$(awk 'NR == 1 {print $1}' "$INPUT_CHECKSUM")
CHECKSUM_INPUT_NAME=$(awk 'NR == 1 {print $2}' "$INPUT_CHECKSUM" | sed 's/^\*//')
case "$EXPECTED_INPUT_DIGEST" in
  *[!0-9A-Fa-f]* | '')
    printf 'Signed input checksum is malformed: %s\n' "$INPUT_CHECKSUM" >&2
    exit 65
    ;;
esac
if [ "${#EXPECTED_INPUT_DIGEST}" -ne 64 ] || [ "$CHECKSUM_INPUT_NAME" != "$BASE" ]; then
  printf 'Signed input checksum does not name the exact artifact: %s\n' "$INPUT_CHECKSUM" >&2
  exit 65
fi
ACTUAL_INPUT_DIGEST=$(shasum -a 256 "$INPUT" | awk '{print $1}')
if [ "$ACTUAL_INPUT_DIGEST" != "$EXPECTED_INPUT_DIGEST" ]; then
  printf 'Signed input checksum mismatch for %s.\n' "$INPUT" >&2
  exit 65
fi
if [ "$PYTHON" = "" ] || { [ ! -x "$PYTHON" ] && ! command -v "$PYTHON" >/dev/null 2>&1; }; then
  printf '%s\n' 'Python is unavailable; it is required to validate notarization evidence.' >&2
  exit 69
fi
if [ ! -x "$VERIFY_SCRIPT" ]; then
  printf '%s\n' 'The macOS signature verification helper is missing or not executable.' >&2
  exit 69
fi

OUTPUT="$INPUT_DIR/$STEM.$FORMAT"
RESULT_FILE="$INPUT_DIR/$STEM.notary-$FORMAT.json"
LOG_FILE="$INPUT_DIR/$STEM.notary-$FORMAT-log.json"
PUBLISH_CANDIDATE="$INPUT_DIR/.$STEM.$$.partial.$FORMAT"
CHECKSUM_CANDIDATE="$INPUT_DIR/.$STEM.$FORMAT.sha256.$$.partial"
for target in "$OUTPUT" "$OUTPUT.sha256" "$RESULT_FILE" "$LOG_FILE"; do
  if [ -e "$target" ] || [ -L "$target" ]; then
    printf 'Refusing to overwrite release evidence or output: %s\n' "$target" >&2
    exit 73
  fi
done

PROFILE=${SKILLHUB_NOTARY_PROFILE:-}
API_KEY=${SKILLHUB_NOTARY_KEY:-}
API_KEY_ID=${SKILLHUB_NOTARY_KEY_ID:-}
API_ISSUER=${SKILLHUB_NOTARY_ISSUER:-}
if [ "$PROFILE" != "" ] && { [ "$API_KEY" != "" ] || [ "$API_KEY_ID" != "" ] || [ "$API_ISSUER" != "" ]; }; then
  printf '%s\n' 'Choose one notary authentication mode: keychain profile or team API key.' >&2
  exit 64
fi
if [ "$PROFILE" = "" ]; then
  if [ "$API_KEY" = "" ] || [ "$API_KEY_ID" = "" ] || [ "$API_ISSUER" = "" ]; then
    printf '%s\n' 'Set SKILLHUB_NOTARY_PROFILE or all three team API key variables.' >&2
    exit 64
  fi
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

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/skillhub-notary.XXXXXX")
MOUNT_POINT=
cleanup() {
  if [ "$MOUNT_POINT" != "" ]; then
    hdiutil detach -quiet "$MOUNT_POINT" >/dev/null 2>&1 || true
  fi
  rm -f "$PUBLISH_CANDIDATE" "$CHECKSUM_CANDIDATE"
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

mount_image() {
  image=$1
  mount_name=$2
  MOUNT_POINT="$WORK_DIR/$mount_name"
  mkdir -p "$MOUNT_POINT"
  hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT_POINT" "$image"
}

detach_image() {
  if [ "$MOUNT_POINT" != "" ]; then
    hdiutil detach -quiet "$MOUNT_POINT"
    MOUNT_POINT=
  fi
}

verify_dmg_signature() {
  dmg=$1
  codesign --verify --strict --verbose=2 "$dmg"
  details=$(codesign --display --verbose=4 "$dmg" 2>&1)
  observed_identity=$(printf '%s\n' "$details" | sed -n 's/^Authority=//p' | head -n 1)
  observed_team_id=$(printf '%s\n' "$details" | sed -n 's/^TeamIdentifier=//p' | head -n 1)
  if [ "$observed_identity" != "$SKILLHUB_CODESIGN_IDENTITY" ]; then
    printf 'DMG signing identity mismatch: expected %s, observed %s\n' \
      "$SKILLHUB_CODESIGN_IDENTITY" "${observed_identity:-<missing>}" >&2
    exit 65
  fi
  if [ "$observed_team_id" != "$SKILLHUB_EXPECTED_TEAM_ID" ]; then
    printf 'DMG Team ID mismatch: expected %s, observed %s\n' \
      "$SKILLHUB_EXPECTED_TEAM_ID" "${observed_team_id:-<missing>}" >&2
    exit 65
  fi
  printf '%s\n' "$details" | grep -E '^Timestamp=' >/dev/null || {
    printf '%s\n' 'DMG signature has no secure timestamp.' >&2
    exit 65
  }
}

if [ "$FORMAT" = "zip" ]; then
  mkdir -p "$WORK_DIR/preflight"
  /usr/bin/ditto -x -k "$INPUT" "$WORK_DIR/preflight"
  PREFLIGHT_BINARY="$WORK_DIR/preflight/$STEM/skillctl"
  MODE_FILE="$WORK_DIR/preflight/$STEM/SIGNING_MODE"
else
  verify_dmg_signature "$INPUT"
  mount_image "$INPUT" preflight-mount
  PREFLIGHT_BINARY="$MOUNT_POINT/$STEM/skillctl"
  MODE_FILE="$MOUNT_POINT/$STEM/SIGNING_MODE"
fi
if [ ! -f "$PREFLIGHT_BINARY" ] || [ ! -f "$MODE_FILE" ]; then
  printf '%s\n' 'Artifact does not contain the expected signed skillctl layout.' >&2
  exit 65
fi
if [ "$(sed -n '1p' "$MODE_FILE")" != "developer-id" ]; then
  printf '%s\n' 'Artifact is not marked as a Developer ID build.' >&2
  exit 65
fi
SKILLHUB_CODESIGN_IDENTITY="$SKILLHUB_CODESIGN_IDENTITY" \
SKILLHUB_EXPECTED_TEAM_ID="$SKILLHUB_EXPECTED_TEAM_ID" \
SKILLHUB_REQUIRE_DEVELOPER_ID=1 \
  "$VERIFY_SCRIPT" "$PREFLIGHT_BINARY"
detach_image

set +e
if [ "$PROFILE" != "" ]; then
  xcrun notarytool submit \
    --keychain-profile "$PROFILE" \
    --wait \
    --timeout 60m \
    --output-format json \
    "$INPUT" > "$RESULT_FILE"
  SUBMIT_RC=$?
else
  xcrun notarytool submit \
    --key "$API_KEY" \
    --key-id "$API_KEY_ID" \
    --issuer "$API_ISSUER" \
    --wait \
    --timeout 60m \
    --output-format json \
    "$INPUT" > "$RESULT_FILE"
  SUBMIT_RC=$?
fi
set -e

if ! PARSED=$("$PYTHON" -c \
  'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(str(value.get("status", "")) + "|" + str(value.get("id", "")))' \
  "$RESULT_FILE"); then
  printf 'notarytool did not return valid JSON; evidence retained at %s\n' "$RESULT_FILE" >&2
  exit 70
fi
STATUS=${PARSED%%|*}
SUBMISSION_ID=${PARSED#*|}

if [ "$SUBMISSION_ID" != "" ]; then
  set +e
  if [ "$PROFILE" != "" ]; then
    xcrun notarytool log --keychain-profile "$PROFILE" "$SUBMISSION_ID" "$LOG_FILE"
  else
    xcrun notarytool log \
      --key "$API_KEY" --key-id "$API_KEY_ID" --issuer "$API_ISSUER" \
      "$SUBMISSION_ID" "$LOG_FILE"
  fi
  LOG_RC=$?
  set -e
  if [ "$LOG_RC" -ne 0 ]; then
    printf 'Unable to retrieve the required notarization log for %s.\n' "$SUBMISSION_ID" >&2
    exit 70
  fi
  "$PYTHON" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$LOG_FILE" || {
    printf 'Notarization log is not valid JSON: %s\n' "$LOG_FILE" >&2
    exit 70
  }
else
  printf '%s\n' 'notarytool returned no submission ID; refusing to prepare a release artifact.' >&2
  exit 70
fi

printf 'Notary status: %s (submission=%s)\n' "$STATUS" "${SUBMISSION_ID:-unknown}"
if [ "$SUBMIT_RC" -ne 0 ] || [ "$STATUS" != "Accepted" ]; then
  printf 'Notarization was not accepted; evidence retained at %s\n' "$RESULT_FILE" >&2
  exit 65
fi

cp -p "$INPUT" "$PUBLISH_CANDIDATE"
if [ "$FORMAT" = "dmg" ]; then
  xcrun stapler staple -v "$PUBLISH_CANDIDATE"
  xcrun stapler validate -v "$PUBLISH_CANDIDATE"
  verify_dmg_signature "$PUBLISH_CANDIDATE"
  spctl --status | grep -F 'assessments enabled' >/dev/null || {
    printf '%s\n' 'Gatekeeper assessments must be enabled to validate the DMG.' >&2
    exit 65
  }
  spctl --assess --type open --context context:primary-signature --verbose=4 \
    "$PUBLISH_CANDIDATE"
  mount_image "$PUBLISH_CANDIDATE" postflight-mount
  POSTFLIGHT_BINARY="$MOUNT_POINT/$STEM/skillctl"
else
  printf '%s\n' 'ZIP notarization uses Apple online tickets; ZIP files cannot be stapled.'
  mkdir -p "$WORK_DIR/postflight"
  /usr/bin/ditto -x -k "$PUBLISH_CANDIDATE" "$WORK_DIR/postflight"
  POSTFLIGHT_BINARY="$WORK_DIR/postflight/$STEM/skillctl"
fi

SKILLHUB_CODESIGN_IDENTITY="$SKILLHUB_CODESIGN_IDENTITY" \
SKILLHUB_EXPECTED_TEAM_ID="$SKILLHUB_EXPECTED_TEAM_ID" \
SKILLHUB_REQUIRE_DEVELOPER_ID=1 \
SKILLHUB_REQUIRE_NOTARIZED=1 \
  "$VERIFY_SCRIPT" "$POSTFLIGHT_BINARY"
detach_image

DIGEST=$(shasum -a 256 "$PUBLISH_CANDIDATE" | awk '{print $1}')
printf '%s  %s\n' "$DIGEST" "$(basename "$OUTPUT")" > "$CHECKSUM_CANDIDATE"
# Publish the checksum first and make the suffix-free artifact rename the commit
# point.  Thus the public artifact can never appear without its checksum, even
# if the runner is interrupted between the two renames.
mv "$CHECKSUM_CANDIDATE" "$OUTPUT.sha256"
mv "$PUBLISH_CANDIDATE" "$OUTPUT"
printf 'Created verified public artifact: %s\n' "$OUTPUT"
printf 'SHA-256: %s\n' "$DIGEST"
