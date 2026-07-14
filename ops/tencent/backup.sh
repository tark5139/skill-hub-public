#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

BACKUP_DIR=${BACKUP_DIR:-/var/backups/skill-hub/postgres}
BACKUP_ENV_FILE=${BACKUP_ENV_FILE:-/etc/skill-hub/backup.env}
RETENTION_DAYS=${RETENTION_DAYS:-14}
LOCK_FILE=${LOCK_FILE:-/var/lock/skill-hub/ops.lock}
REQUIRE_COS_BACKUP=${REQUIRE_COS_BACKUP:-0}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-personal-skill-hub}
POSTGRES_SERVICE_NAME=${POSTGRES_SERVICE_NAME:-postgres}

fail() {
  printf '[backup] ERROR: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail 'docker is unavailable'
[[ ${RETENTION_DAYS} =~ ^[0-9]+$ ]] || fail 'RETENTION_DAYS must be a non-negative integer'
[[ ${REQUIRE_COS_BACKUP} =~ ^[01]$ ]] || fail 'REQUIRE_COS_BACKUP must be 0 or 1'
skillhub_acquire_lock "$LOCK_FILE" || \
  fail "cannot acquire exclusive operations lock: ${LOCK_FILE}"

umask 077
mkdir -p "$BACKUP_DIR"

if [[ -f ${BACKUP_ENV_FILE} ]]; then
  [[ ! -L ${BACKUP_ENV_FILE} ]] || fail "${BACKUP_ENV_FILE} must not be a symlink"
  [[ -r ${BACKUP_ENV_FILE} ]] || fail "${BACKUP_ENV_FILE} is not readable by this operator"
  permissions=$(stat -c '%a' "$BACKUP_ENV_FILE")
  [[ ${permissions} == 600 || ${permissions} == 640 ]] || \
    fail "${BACKUP_ENV_FILE} must have mode 0600 or 0640"
  if [[ ${BACKUP_ENV_FILE} == /etc/skill-hub/* ]]; then
    [[ $(stat -c '%U' "$BACKUP_ENV_FILE") == root ]] || \
      fail "${BACKUP_ENV_FILE} must be owned by root"
  fi
  if awk '!/^[[:space:]]*(#|$)/ && /REPLACE_/ { found = 1 } END { exit !found }' \
    "$BACKUP_ENV_FILE"; then
    fail "${BACKUP_ENV_FILE} still contains REPLACE_ placeholders"
  fi
  # shellcheck disable=SC1090
  set -a
  source "$BACKUP_ENV_FILE"
  set +a
fi

[[ ${RETENTION_DAYS} =~ ^[0-9]+$ ]] || \
  fail 'RETENTION_DAYS from backup.env must be a non-negative integer'
cos_uri=${BACKUP_COS_URI:-}
if [[ ${REQUIRE_COS_BACKUP} == 1 && -z ${cos_uri} ]]; then
  fail 'COS backup is required, but BACKUP_COS_URI is not configured'
fi

postgres_ids=$(skillhub_container_ids "$COMPOSE_PROJECT_NAME" "$POSTGRES_SERVICE_NAME")
postgres_count=$(printf '%s\n' "$postgres_ids" | awk 'NF { count += 1 } END { print count + 0 }')
[[ ${postgres_count} -eq 1 ]] || \
  fail "expected exactly one running PostgreSQL container, found ${postgres_count}"
IFS= read -r postgres_container <<<"$postgres_ids"
docker exec "$postgres_container" sh -c \
  'pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' >/dev/null || \
  fail 'the current PostgreSQL container is not ready'

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
final_path="${BACKUP_DIR}/skillhub-${timestamp}.dump"
temporary_path="${final_path}.partial"
checksum_temp="${final_path}.sha256.partial"
[[ ! -e ${final_path} && ! -e ${temporary_path} ]] || \
  fail "backup path already exists for timestamp ${timestamp}"

cleanup() {
  rm -f "$temporary_path" "$checksum_temp"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker exec "$postgres_container" sh -c '
  exec pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --format=custom --compress=9 --no-owner --no-privileges
' > "$temporary_path"
[[ -s ${temporary_path} ]] || fail 'pg_dump produced an empty archive'
mv "$temporary_path" "$final_path"

if command -v sha256sum >/dev/null 2>&1; then
  digest=$(sha256sum "$final_path" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  digest=$(shasum -a 256 "$final_path" | awk '{print $1}')
else
  fail 'neither sha256sum nor shasum is available'
fi
printf '%s  %s\n' "$digest" "$(basename -- "$final_path")" > "$checksum_temp"
mv "$checksum_temp" "${final_path}.sha256"
skillhub_verify_checksum "$final_path" || fail 'portable backup checksum verification failed'

if [[ -n ${cos_uri} ]]; then
  command -v aws >/dev/null 2>&1 || fail 'AWS CLI is required for the configured COS backup'
  : "${SKILLHUB_S3_ENDPOINT_URL:?set the Tencent COS S3 endpoint in backup.env}"
  AWS_ACCESS_KEY_ID=${BACKUP_COS_ACCESS_KEY_ID:-}
  AWS_SECRET_ACCESS_KEY=${BACKUP_COS_SECRET_ACCESS_KEY:-}
  : "${AWS_ACCESS_KEY_ID:?set BACKUP_COS_ACCESS_KEY_ID in backup.env}"
  : "${AWS_SECRET_ACCESS_KEY:?set BACKUP_COS_SECRET_ACCESS_KEY in backup.env}"
  AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-shanghai}
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION
  aws --endpoint-url "$SKILLHUB_S3_ENDPOINT_URL" s3 cp "$final_path" \
    "${cos_uri}/$(basename -- "$final_path")" --sse AES256 --only-show-errors
  aws --endpoint-url "$SKILLHUB_S3_ENDPOINT_URL" s3 cp "${final_path}.sha256" \
    "${cos_uri}/$(basename -- "$final_path").sha256" --sse AES256 --only-show-errors
  printf '[backup] encrypted COS copy uploaded under: %s\n' "$cos_uri"
fi

find "$BACKUP_DIR" -type f -name 'skillhub-*.dump' -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -type f -name 'skillhub-*.dump.sha256' -mtime "+$RETENTION_DAYS" -delete

trap - EXIT HUP INT TERM
printf '[backup] verified PostgreSQL backup: %s\n' "$final_path"
