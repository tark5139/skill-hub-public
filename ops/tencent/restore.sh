#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

DEFAULT_APP_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
if [[ ! -f ${DEFAULT_APP_DIR}/compose.yaml && -f /opt/skill-hub/compose.yaml ]]; then
  DEFAULT_APP_DIR=/opt/skill-hub
fi
APP_DIR=${APP_DIR:-$DEFAULT_APP_DIR}
LOCK_FILE=${LOCK_FILE:-/var/lock/skill-hub/ops.lock}
CONFIRM_RESTORE=${CONFIRM_RESTORE:-}
CONFIRM_DATABASE=${CONFIRM_DATABASE:-}
ENABLE_HTTPS=${ENABLE_HTTPS:-1}
POSTGRES_WAIT_SECONDS=${POSTGRES_WAIT_SECONDS:-120}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-personal-skill-hub}
FAIL_CLOSED_ON_ERROR=0

fail() {
  printf '[restore] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local status=$?
  if [[ ${status} -ne 0 && ${FAIL_CLOSED_ON_ERROR} == 1 ]]; then
    printf '%s\n' '[restore] fail-closed: stopping Caddy, API, and worker' >&2
    skillhub_stop_services_by_label "$COMPOSE_PROJECT_NAME" caddy api worker || true
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ $# -eq 1 ]] || \
  fail 'usage: CONFIRM_RESTORE=skillhub CONFIRM_DATABASE=skillhub ./ops/tencent/restore.sh <backup.dump>'
[[ -d ${APP_DIR}/.git ]] || fail "${APP_DIR} is not a Git checkout"
[[ -f ${APP_DIR}/.env && ! -L ${APP_DIR}/.env && -s ${APP_DIR}/.env ]] || \
  fail "${APP_DIR}/.env must be a non-empty, regular, non-symlink file"
[[ $(stat -c '%a' "${APP_DIR}/.env") == 600 ]] || \
  fail "${APP_DIR}/.env must have mode 0600"
[[ ${ENABLE_HTTPS} =~ ^[01]$ ]] || fail 'ENABLE_HTTPS must be 0 or 1'
[[ ${POSTGRES_WAIT_SECONDS} =~ ^[1-9][0-9]*$ ]] || \
  fail 'POSTGRES_WAIT_SECONDS must be a positive integer'
command -v docker >/dev/null 2>&1 || fail 'docker is unavailable'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is unavailable'
skillhub_acquire_lock "$LOCK_FILE" || \
  fail "cannot acquire exclusive operations lock: ${LOCK_FILE}"

POSTGRES_USER=$(skillhub_read_env_value "${APP_DIR}/.env" POSTGRES_USER) || \
  fail 'POSTGRES_USER must appear exactly once in the Compose .env file'
POSTGRES_DB=$(skillhub_read_env_value "${APP_DIR}/.env" POSTGRES_DB) || \
  fail 'POSTGRES_DB must appear exactly once in the Compose .env file'
[[ ${POSTGRES_DB} != postgres && ${POSTGRES_DB} != template0 && ${POSTGRES_DB} != template1 ]] || \
  fail 'refusing to recreate a PostgreSQL system database'
[[ ${CONFIRM_RESTORE} == skillhub && ${CONFIRM_DATABASE} == "$POSTGRES_DB" ]] || \
  fail 'restore confirmation does not match the configured database'

backup_dir=$(cd -- "$(dirname -- "$1")" && pwd) || fail 'backup directory is unavailable'
backup_path="${backup_dir}/$(basename -- "$1")"
[[ -f ${backup_path} && ! -L ${backup_path} && -s ${backup_path} ]] || \
  fail 'backup must be a non-empty, regular, non-symlink file'
skillhub_verify_checksum "$backup_path" || \
  fail 'backup checksum is missing, invalid, or cannot be verified'

docker compose --project-directory "$APP_DIR" config --quiet
docker compose --project-directory "$APP_DIR" up -d postgres
deadline=$((SECONDS + POSTGRES_WAIT_SECONDS))
until docker compose --project-directory "$APP_DIR" exec -T postgres \
  sh -c 'pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' \
  >/dev/null 2>&1; do
  ((SECONDS < deadline)) || fail 'PostgreSQL did not become ready before the timeout'
  sleep 2
done

docker compose --project-directory "$APP_DIR" exec -T postgres \
  pg_restore --list >/dev/null < "$backup_path" || fail 'backup is not a readable pg_dump archive'

printf '%s\n' '[restore] stopping public ingress, API, and worker; failures leave them stopped'
FAIL_CLOSED_ON_ERROR=1
skillhub_stop_services_by_label "$COMPOSE_PROJECT_NAME" caddy api worker || \
  fail 'could not confirm Caddy, API, and worker are stopped; database was not modified'

docker compose --project-directory "$APP_DIR" exec -T postgres sh -eu -c '
  dropdb --force --username "$POSTGRES_USER" "$POSTGRES_DB"
  createdb --username "$POSTGRES_USER" --owner "$POSTGRES_USER" "$POSTGRES_DB"
' || fail 'database recreation failed; API and worker remain stopped'

if ! docker compose --project-directory "$APP_DIR" exec -T postgres \
  pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --no-owner --no-privileges --exit-on-error < "$backup_path"; then
  fail 'pg_restore failed; the partially restored database is isolated and services remain stopped'
fi

docker compose --project-directory "$APP_DIR" run --rm migrate || \
  fail 'post-restore migration failed; services remain stopped'
docker compose --project-directory "$APP_DIR" exec -T postgres sh -c \
  'psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=on --command "DELETE FROM worker_heartbeats;"'

docker compose --project-directory "$APP_DIR" up -d --no-deps --force-recreate api worker
APP_DIR="$APP_DIR" "${SCRIPT_DIR}/healthcheck.sh"

if [[ ${ENABLE_HTTPS} == 1 ]]; then
  docker compose --project-directory "$APP_DIR" --profile https up -d --no-deps \
    --force-recreate caddy
  public_base_url=$(skillhub_read_env_value "${APP_DIR}/.env" SKILLHUB_PUBLIC_BASE_URL) || \
    fail 'SKILLHUB_PUBLIC_BASE_URL must appear exactly once in .env'
  [[ ${public_base_url} =~ ^https:// ]] || \
    fail 'SKILLHUB_PUBLIC_BASE_URL must use HTTPS when Caddy is enabled'
  if ! APP_DIR="$APP_DIR" HEALTHCHECK_BASE_URL="$public_base_url" \
    "${SCRIPT_DIR}/healthcheck.sh"; then
    docker compose --project-directory "$APP_DIR" --profile https stop caddy \
      >/dev/null 2>&1 || true
    fail 'external HTTPS check failed; Caddy was stopped'
  fi
fi

printf '[restore] restore, migration, worker heartbeat, and health checks succeeded: %s\n' \
  "$backup_path"
FAIL_CLOSED_ON_ERROR=0
