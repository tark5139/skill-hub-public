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
HEALTHCHECK_ATTEMPTS=${HEALTHCHECK_ATTEMPTS:-30}
HEALTHCHECK_INTERVAL_SECONDS=${HEALTHCHECK_INTERVAL_SECONDS:-2}
HEALTHCHECK_TIMEOUT_SECONDS=${HEALTHCHECK_TIMEOUT_SECONDS:-5}
if [[ -z ${HEALTHCHECK_BASE_URL:-} ]]; then
  api_port=8080
  if [[ -f ${APP_DIR}/.env ]]; then
    if configured_port=$(skillhub_read_env_value "${APP_DIR}/.env" SKILLHUB_API_PORT); then
      api_port=$configured_port
    elif [[ $? -ne 1 ]]; then
      printf '[healthcheck] ERROR: SKILLHUB_API_PORT appears more than once in .env\n' >&2
      exit 1
    fi
  fi
  [[ ${api_port} =~ ^[0-9]+$ ]] && ((api_port >= 1 && api_port <= 65535)) || {
    printf '[healthcheck] ERROR: SKILLHUB_API_PORT must be an integer from 1 to 65535\n' >&2
    exit 1
  }
  HEALTHCHECK_BASE_URL="http://127.0.0.1:${api_port}"
fi
HEALTHCHECK_BASE_URL=${HEALTHCHECK_BASE_URL%/}

fail() {
  printf '[healthcheck] ERROR: %s\n' "$*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail 'curl is unavailable'
[[ ${HEALTHCHECK_BASE_URL} =~ ^https?:// ]] || \
  fail 'HEALTHCHECK_BASE_URL must use http:// or https://'
[[ ${HEALTHCHECK_ATTEMPTS} =~ ^[1-9][0-9]*$ ]] || \
  fail 'HEALTHCHECK_ATTEMPTS must be a positive integer'
[[ ${HEALTHCHECK_INTERVAL_SECONDS} =~ ^[0-9]+([.][0-9]+)?$ ]] || \
  fail 'HEALTHCHECK_INTERVAL_SECONDS must be a non-negative number'
[[ ${HEALTHCHECK_TIMEOUT_SECONDS} =~ ^[0-9]+([.][0-9]+)?$ ]] || \
  fail 'HEALTHCHECK_TIMEOUT_SECONDS must be a non-negative number'

probe() {
  local path=$1
  local expected=$2
  local attempt response

  for ((attempt = 1; attempt <= HEALTHCHECK_ATTEMPTS; attempt += 1)); do
    if response=$(curl --fail --silent --show-error \
      --max-time "$HEALTHCHECK_TIMEOUT_SECONDS" \
      "${HEALTHCHECK_BASE_URL}${path}" 2>/dev/null) && \
      [[ ${response} == *"${expected}"* ]]; then
      printf '[healthcheck] %s passed on attempt %d\n' "$path" "$attempt"
      return 0
    fi
    sleep "$HEALTHCHECK_INTERVAL_SECONDS"
  done
  return 1
}

probe '/health/live' '"status":"ok"' || fail 'liveness probe failed'
probe '/health/ready' '"status":"ready"' || {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose --project-directory "$APP_DIR" ps >&2 || true
  fi
  fail 'readiness failed; inspect API/worker logs through the approved secure log channel'
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && \
  [[ -f ${APP_DIR}/compose.yaml ]]; then
  running_services=$(docker compose --project-directory "$APP_DIR" ps --status running --services)
  for required_service in api worker postgres; do
    if ! grep -Fxq "$required_service" <<<"$running_services"; then
      fail "Compose service is not running: ${required_service}"
    fi
  done
fi

printf '[healthcheck] service is live and ready at %s\n' "$HEALTHCHECK_BASE_URL"
