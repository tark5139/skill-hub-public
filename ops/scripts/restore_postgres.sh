#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: CONFIRM_RESTORE=skillhub <backup.dump>\n' >&2
  exit 64
fi
if [ "${CONFIRM_RESTORE:-}" != "skillhub" ]; then
  printf '%s\n' 'Restore refused. Set CONFIRM_RESTORE=skillhub after reviewing the runbook.' >&2
  exit 65
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
BACKUP_PATH=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
POSTGRES_USER=${POSTGRES_USER:-skillhub}
POSTGRES_DB=${POSTGRES_DB:-skillhub}

test -s "$BACKUP_PATH"
if [ -f "$BACKUP_PATH.sha256" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$(dirname -- "$BACKUP_PATH")" && sha256sum -c "$(basename -- "$BACKUP_PATH").sha256")
  elif command -v shasum >/dev/null 2>&1; then
    (cd "$(dirname -- "$BACKUP_PATH")" && shasum -a 256 -c "$(basename -- "$BACKUP_PATH").sha256")
  else
    printf '%s\n' 'Neither sha256sum nor shasum is available.' >&2
    exit 69
  fi
else
  printf '%s\n' 'Warning: checksum file is missing; restore continues only by explicit confirmation.' >&2
fi

docker compose --project-directory "$PROJECT_DIR" stop api worker

restore_failed=0
docker compose --project-directory "$PROJECT_DIR" exec -T postgres \
  pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --clean --if-exists --no-owner --no-privileges < "$BACKUP_PATH" || restore_failed=$?

docker compose --project-directory "$PROJECT_DIR" start api worker

if [ "$restore_failed" -ne 0 ]; then
  printf 'Restore failed with exit code %s; inspect PostgreSQL logs.\n' "$restore_failed" >&2
  exit "$restore_failed"
fi
printf 'Restore completed from: %s\n' "$BACKUP_PATH"
