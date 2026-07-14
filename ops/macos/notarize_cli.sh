#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: SKILLHUB_NOTARY_PROFILE=<keychain-profile> %s <signed-unnotarized.zip>\n' \
    "$0" >&2
  exit 64
fi
: "${SKILLHUB_NOTARY_PROFILE:?set the xcrun notarytool keychain profile}"

INPUT=$1
case "$(basename "$INPUT")" in
  skillctl-*-macos13-arm64-signed-unnotarized.zip) ;;
  *)
    printf '%s\n' 'Refusing to notarize an archive without the signed-unnotarized suffix.' >&2
    exit 65
    ;;
esac
test -s "$INPUT"

RESULT=$(xcrun notarytool submit "$INPUT" \
  --keychain-profile "$SKILLHUB_NOTARY_PROFILE" \
  --wait \
  --output-format json)
printf '%s' "$RESULT" | python3 -c \
  'import json,sys; result=json.load(sys.stdin); status=result.get("status"); print("Notary status:", status); sys.exit(0 if status == "Accepted" else 1)'

OUTPUT=$(printf '%s' "$INPUT" | sed 's/-signed-unnotarized\.zip$/.zip/')
cp "$INPUT" "$OUTPUT"
(cd "$(dirname "$OUTPUT")" && shasum -a 256 "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256")
printf 'Created notarized public artifact: %s\n' "$OUTPUT"
