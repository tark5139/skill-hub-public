#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <signed-skillctl-binary>\n' "$0" >&2
  exit 64
fi

BINARY=$1
EXPECTED_TEAM_ID=${SKILLHUB_EXPECTED_TEAM_ID:-}
EXPECTED_IDENTITY=${SKILLHUB_CODESIGN_IDENTITY:-}
REQUIRE_DEVELOPER_ID=${SKILLHUB_REQUIRE_DEVELOPER_ID:-1}
REQUIRE_NOTARIZED=${SKILLHUB_REQUIRE_NOTARIZED:-0}

if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' 'Code-signing verification must run on macOS.' >&2
  exit 69
fi
if [ ! -f "$BINARY" ] || [ -L "$BINARY" ]; then
  printf 'Signed CLI is not a regular file: %s\n' "$BINARY" >&2
  exit 66
fi

codesign --verify --strict --verbose=2 "$BINARY"
DETAILS=$(codesign --display --verbose=4 "$BINARY" 2>&1)

if [ "$REQUIRE_DEVELOPER_ID" = "1" ]; then
  printf '%s\n' "$DETAILS" | grep -F 'Authority=Developer ID Application:' >/dev/null || {
    printf '%s\n' 'Signature is not from a Developer ID Application certificate.' >&2
    exit 65
  }
  printf '%s\n' "$DETAILS" | grep -E '^flags=.*\(runtime\)' >/dev/null || {
    printf '%s\n' 'Signed CLI does not enable the hardened runtime.' >&2
    exit 65
  }
  printf '%s\n' "$DETAILS" | grep -E '^Timestamp=' >/dev/null || {
    printf '%s\n' 'Signed CLI has no secure timestamp.' >&2
    exit 65
  }
fi

OBSERVED_IDENTITY=$(printf '%s\n' "$DETAILS" | sed -n 's/^Authority=//p' | head -n 1)
if [ "$EXPECTED_IDENTITY" != "" ] && [ "$OBSERVED_IDENTITY" != "$EXPECTED_IDENTITY" ]; then
  printf 'Developer ID identity mismatch: expected %s, observed %s\n' \
    "$EXPECTED_IDENTITY" "${OBSERVED_IDENTITY:-<missing>}" >&2
  exit 65
fi
OBSERVED_TEAM_ID=$(printf '%s\n' "$DETAILS" | sed -n 's/^TeamIdentifier=//p' | head -n 1)
if [ "$EXPECTED_TEAM_ID" != "" ] && [ "$OBSERVED_TEAM_ID" != "$EXPECTED_TEAM_ID" ]; then
  printf 'Developer Team ID mismatch: expected %s, observed %s\n' \
    "$EXPECTED_TEAM_ID" "${OBSERVED_TEAM_ID:-<missing>}" >&2
  exit 65
fi

if [ "$REQUIRE_NOTARIZED" = "1" ]; then
  codesign --verify --strict --verbose=4 \
    -R='notarized' \
    --check-notarization \
    "$BINARY"
fi

printf 'Verified skillctl signature (team=%s, online-notarization=%s).\n' \
  "${OBSERVED_TEAM_ID:-unknown}" "$REQUIRE_NOTARIZED"
