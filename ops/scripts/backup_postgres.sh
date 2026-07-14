#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
BACKUP_DIR=${BACKUP_DIR:-"$PROJECT_DIR/ops/backups"}
RETENTION_DAYS=${RETENTION_DAYS:-14}
POSTGRES_USER=${POSTGRES_USER:-skillhub}
POSTGRES_DB=${POSTGRES_DB:-skillhub}
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL_PATH="$BACKUP_DIR/skillhub-$TIMESTAMP.dump"
TEMP_PATH="$FINAL_PATH.partial"

mkdir -p "$BACKUP_DIR"
umask 077

cleanup() {
  rm -f "$TEMP_PATH"
}
trap cleanup EXIT HUP INT TERM

docker compose --project-directory "$PROJECT_DIR" exec -T postgres \
  pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --format=custom --compress=9 --no-owner --no-privileges > "$TEMP_PATH"

test -s "$TEMP_PATH"
mv "$TEMP_PATH" "$FINAL_PATH"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$FINAL_PATH" > "$FINAL_PATH.sha256"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$FINAL_PATH" > "$FINAL_PATH.sha256"
else
  printf '%s\n' 'Neither sha256sum nor shasum is available.' >&2
  exit 69
fi

find "$BACKUP_DIR" -type f -name 'skillhub-*.dump' -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'skillhub-*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete

trap - EXIT HUP INT TERM
printf 'PostgreSQL backup created: %s\n' "$FINAL_PATH"

if [ "${BACKUP_COS_URI:-}" != "" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    printf '%s\n' 'BACKUP_COS_URI is set, but aws CLI is unavailable; local backup retained.' >&2
    exit 2
  fi
  : "${SKILLHUB_S3_ENDPOINT_URL:?set the Tencent COS S3 endpoint}"
  AWS_ACCESS_KEY_ID=${BACKUP_COS_ACCESS_KEY_ID:-${SKILLHUB_S3_ACCESS_KEY_ID:-}}
  AWS_SECRET_ACCESS_KEY=${BACKUP_COS_SECRET_ACCESS_KEY:-${SKILLHUB_S3_SECRET_ACCESS_KEY:-}}
  : "${AWS_ACCESS_KEY_ID:?set a least-privilege COS backup access key}"
  : "${AWS_SECRET_ACCESS_KEY:?set a least-privilege COS backup secret key}"
  AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-shanghai}
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION
  aws --endpoint-url "$SKILLHUB_S3_ENDPOINT_URL" s3 cp "$FINAL_PATH" \
    "$BACKUP_COS_URI/$(basename "$FINAL_PATH")" --sse AES256 --only-show-errors
  aws --endpoint-url "$SKILLHUB_S3_ENDPOINT_URL" s3 cp "$FINAL_PATH.sha256" \
    "$BACKUP_COS_URI/$(basename "$FINAL_PATH.sha256")" --sse AES256 --only-show-errors
  printf 'COS backup copy uploaded under: %s\n' "$BACKUP_COS_URI"
fi
