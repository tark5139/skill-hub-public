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
TARGET_TAG=${1:-${TARGET_TAG:-}}
BACKUP_BEFORE_DEPLOY=${BACKUP_BEFORE_DEPLOY:-1}
REQUIRE_COS_BACKUP_BEFORE_CHANGE=${REQUIRE_COS_BACKUP_BEFORE_CHANGE:-1}
CONFIRM_EMPTY_INITIAL_DEPLOY=${CONFIRM_EMPTY_INITIAL_DEPLOY:-}
ENABLE_HTTPS=${ENABLE_HTTPS:-1}
DEPLOY_STATE_DIR=${DEPLOY_STATE_DIR:-${APP_DIR}/.git/skillhub-deploy}
POSTGRES_WAIT_SECONDS=${POSTGRES_WAIT_SECONDS:-120}
LOCK_FILE=${LOCK_FILE:-/var/lock/skill-hub/ops.lock}
TRUSTED_TAG_SIGNERS=/etc/skill-hub/trusted-tag-signers
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-personal-skill-hub}
OPS_RUNTIME=
RESTART_IDS_ON_FAILURE=
FAIL_CLOSED_ON_ERROR=0
umask 077

log() {
  printf '[deploy] %s\n' "$*"
}

fail() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

restart_previous_services() {
  local container_id
  while IFS= read -r container_id; do
    [[ -n ${container_id} ]] || continue
    docker start "$container_id" >/dev/null 2>&1 || true
  done <<<"$RESTART_IDS_ON_FAILURE"
}

cleanup() {
  local status=$?
  if [[ ${status} -ne 0 ]]; then
    if [[ ${FAIL_CLOSED_ON_ERROR} == 1 ]]; then
      printf '%s\n' '[deploy] fail-closed: stopping Caddy, API, and worker' >&2
      skillhub_stop_services_by_label "$COMPOSE_PROJECT_NAME" caddy api worker || true
    elif [[ -n ${RESTART_IDS_ON_FAILURE} ]]; then
      restart_previous_services
    fi
  fi
  if [[ -n ${OPS_RUNTIME} ]]; then
    rm -rf "$OPS_RUNTIME" || true
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ -n ${TARGET_TAG} ]] || fail 'usage: ./deploy.sh <trusted-signed-tag>'
[[ ! ${TARGET_TAG} =~ ^[0-9a-fA-F]{40}$ ]] || \
  fail 'a raw 40-character commit is not a trusted release identity; use a signed tag'
[[ ${TARGET_TAG} != -* ]] || fail 'deployment tag must not begin with a hyphen'
/usr/bin/git check-ref-format "refs/tags/${TARGET_TAG}" >/dev/null 2>&1 || \
  fail 'deployment target must be one exact tag name; commits and branches are refused'
[[ -d ${APP_DIR}/.git ]] || fail "${APP_DIR} is not a Git checkout"
[[ -f ${APP_DIR}/.env && ! -L ${APP_DIR}/.env && -s ${APP_DIR}/.env ]] || \
  fail "${APP_DIR}/.env must be a non-empty, regular, non-symlink file"
[[ $(stat -c '%a' "${APP_DIR}/.env") == 600 ]] || \
  fail "${APP_DIR}/.env must have mode 0600"
if awk '!/^[[:space:]]*(#|$)/ && /REPLACE_/ { found = 1 } END { exit !found }' \
  "${APP_DIR}/.env"; then
  fail "${APP_DIR}/.env still contains REPLACE_ placeholders"
fi
[[ ${BACKUP_BEFORE_DEPLOY} =~ ^[01]$ ]] || fail 'BACKUP_BEFORE_DEPLOY must be 0 or 1'
[[ ${REQUIRE_COS_BACKUP_BEFORE_CHANGE} =~ ^[01]$ ]] || \
  fail 'REQUIRE_COS_BACKUP_BEFORE_CHANGE must be 0 or 1'
[[ ${ENABLE_HTTPS} =~ ^[01]$ ]] || fail 'ENABLE_HTTPS must be 0 or 1'
[[ ${POSTGRES_WAIT_SECONDS} =~ ^[1-9][0-9]*$ ]] || \
  fail 'POSTGRES_WAIT_SECONDS must be a positive integer'
command -v docker >/dev/null 2>&1 || fail 'docker is unavailable'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is unavailable'
skillhub_acquire_lock "$LOCK_FILE" || \
  fail "cannot acquire exclusive operations lock: ${LOCK_FILE}"

if [[ -n $(/usr/bin/git -C "$APP_DIR" status --porcelain --untracked-files=no) ]]; then
  fail 'tracked files contain local changes; deployment refuses to overwrite them'
fi

previous_commit=
previous_ref=
if [[ -s ${DEPLOY_STATE_DIR}/current_revision || -s ${DEPLOY_STATE_DIR}/current_ref ]]; then
  [[ -s ${DEPLOY_STATE_DIR}/current_revision && -s ${DEPLOY_STATE_DIR}/current_ref ]] || \
    fail 'deployment state is incomplete: current_revision/current_ref must both exist'
  previous_commit=$(<"${DEPLOY_STATE_DIR}/current_revision")
  previous_ref=$(<"${DEPLOY_STATE_DIR}/current_ref")
  [[ ${previous_commit} =~ ^[0-9a-f]{40}$ ]] || \
    fail 'recorded current_revision is not a full lowercase commit'

  api_ids=$(skillhub_container_ids "$COMPOSE_PROJECT_NAME" api)
  worker_ids=$(skillhub_container_ids "$COMPOSE_PROJECT_NAME" worker)
  caddy_ids=$(skillhub_container_ids "$COMPOSE_PROJECT_NAME" caddy)
  RESTART_IDS_ON_FAILURE=$(printf '%s\n%s\n%s\n' "$api_ids" "$worker_ids" "$caddy_ids" | \
    awk 'NF && !seen[$0]++')
  if [[ -n ${RESTART_IDS_ON_FAILURE} ]]; then
    log 'stopping current ingress, API, and worker before the trusted backup'
    while IFS= read -r container_id; do
      [[ -n ${container_id} ]] || continue
      docker stop "$container_id" >/dev/null
    done <<<"$RESTART_IDS_ON_FAILURE"
  fi

  [[ ${BACKUP_BEFORE_DEPLOY} == 1 ]] || \
    fail 'existing deployments cannot skip the pre-change backup'
  log 'creating a stable pre-fetch PostgreSQL/COS recovery point'
  APP_DIR="$APP_DIR" LOCK_FILE="$LOCK_FILE" SKILLHUB_OPS_LOCK_HELD=1 \
    REQUIRE_COS_BACKUP="$REQUIRE_COS_BACKUP_BEFORE_CHANGE" \
    "${SCRIPT_DIR}/backup.sh" || fail 'stable pre-change backup failed'
else
  existing_postgres=$(skillhub_container_ids "$COMPOSE_PROJECT_NAME" postgres 1)
  existing_volumes=$(docker volume ls \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter 'label=com.docker.compose.volume=postgres_data' --format '{{.Name}}')
  [[ -z ${existing_postgres} && -z ${existing_volumes} ]] || \
    fail 'no recorded deployment exists, but PostgreSQL state is present; manual recovery is required'
  [[ ${CONFIRM_EMPTY_INITIAL_DEPLOY} == yes ]] || \
    fail 'first deployment has no database to back up; set CONFIRM_EMPTY_INITIAL_DEPLOY=yes after verifying the host is empty'
  log 'confirmed empty first deployment; no pre-existing database recovery point is possible'
fi

# No network fetch, target checkout, Dockerfile build, or target Compose command
# occurs before the recovery point above succeeds.
log 'fetching signed release tags after the recovery point is secured'
/usr/bin/git -C "$APP_DIR" fetch --tags --prune origin
skillhub_verify_trusted_tag "$APP_DIR" "$TARGET_TAG" "$TRUSTED_TAG_SIGNERS" || \
  fail "tag is unsigned, invalid, or not signed by an allowed fingerprint: ${TARGET_TAG}"
target_commit=$(/usr/bin/git -C "$APP_DIR" rev-parse --verify "refs/tags/${TARGET_TAG}^{commit}")
log "trusted tag ${TARGET_TAG} resolves to ${target_commit}"

if [[ -z ${previous_commit} ]]; then
  previous_commit=$target_commit
  previous_ref=$TARGET_TAG
fi

OPS_RUNTIME=$(mktemp -d)
cp "${SCRIPT_DIR}/common.sh" "${SCRIPT_DIR}/healthcheck.sh" "$OPS_RUNTIME/"
chmod 0700 "$OPS_RUNTIME/common.sh" "$OPS_RUNTIME/healthcheck.sh"

mkdir -p "$DEPLOY_STATE_DIR"
chmod 0700 "$DEPLOY_STATE_DIR"
printf '%s\n' "$target_commit" > "${DEPLOY_STATE_DIR}/pending_revision"
printf '%s\n' "$TARGET_TAG" > "${DEPLOY_STATE_DIR}/pending_ref"

# From this point forward, a failure remains fail-closed. The current services
# are not automatically restarted because target code may already have changed
# the database or container topology; the verified COS recovery point is used.
RESTART_IDS_ON_FAILURE=
FAIL_CLOSED_ON_ERROR=1
log "checking out trusted release ${TARGET_TAG}"
/usr/bin/git -C "$APP_DIR" checkout --detach "$target_commit"
docker compose --project-directory "$APP_DIR" config --quiet

log 'building the trusted release image'
docker compose --project-directory "$APP_DIR" build --pull api worker migrate
docker compose --project-directory "$APP_DIR" up -d postgres
deadline=$((SECONDS + POSTGRES_WAIT_SECONDS))
until docker compose --project-directory "$APP_DIR" exec -T postgres \
  sh -c 'pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' \
  >/dev/null 2>&1; do
  ((SECONDS < deadline)) || fail 'PostgreSQL did not become ready before the timeout'
  sleep 2
done

log 'applying forward-only Alembic migrations'
docker compose --project-directory "$APP_DIR" run --rm migrate
docker compose --project-directory "$APP_DIR" exec -T postgres sh -c \
  'psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=on --command "DELETE FROM worker_heartbeats;"'

docker compose --project-directory "$APP_DIR" up -d --no-deps --force-recreate api worker
APP_DIR="$APP_DIR" "$OPS_RUNTIME/healthcheck.sh"

if [[ ${ENABLE_HTTPS} == 1 ]]; then
  docker compose --project-directory "$APP_DIR" --profile https up -d --no-deps \
    --force-recreate caddy
  public_base_url=$(skillhub_read_env_value "${APP_DIR}/.env" SKILLHUB_PUBLIC_BASE_URL) || \
    fail 'SKILLHUB_PUBLIC_BASE_URL must appear exactly once in .env'
  [[ ${public_base_url} =~ ^https:// ]] || \
    fail 'SKILLHUB_PUBLIC_BASE_URL must use HTTPS when Caddy is enabled'
  if ! APP_DIR="$APP_DIR" HEALTHCHECK_BASE_URL="$public_base_url" \
    "$OPS_RUNTIME/healthcheck.sh"; then
    docker compose --project-directory "$APP_DIR" --profile https stop caddy \
      >/dev/null 2>&1 || true
    fail 'external HTTPS check failed; Caddy was stopped'
  fi
fi

docker compose --project-directory "$APP_DIR" images > "${DEPLOY_STATE_DIR}/images.txt.partial"
mv "${DEPLOY_STATE_DIR}/images.txt.partial" "${DEPLOY_STATE_DIR}/images.txt"
printf '%s\n' "$previous_commit" > "${DEPLOY_STATE_DIR}/previous_revision.partial"
mv "${DEPLOY_STATE_DIR}/previous_revision.partial" "${DEPLOY_STATE_DIR}/previous_revision"
printf '%s\n' "$previous_ref" > "${DEPLOY_STATE_DIR}/previous_ref.partial"
mv "${DEPLOY_STATE_DIR}/previous_ref.partial" "${DEPLOY_STATE_DIR}/previous_ref"
printf '%s\n' "$target_commit" > "${DEPLOY_STATE_DIR}/current_revision.partial"
mv "${DEPLOY_STATE_DIR}/current_revision.partial" "${DEPLOY_STATE_DIR}/current_revision"
printf '%s\n' "$TARGET_TAG" > "${DEPLOY_STATE_DIR}/current_ref.partial"
mv "${DEPLOY_STATE_DIR}/current_ref.partial" "${DEPLOY_STATE_DIR}/current_ref"
rm -f "${DEPLOY_STATE_DIR}/pending_revision" "${DEPLOY_STATE_DIR}/pending_ref"
log "deployment completed: ${TARGET_TAG} (${target_commit})"
FAIL_CLOSED_ON_ERROR=0
