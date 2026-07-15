#!/usr/bin/env bash
set -euo pipefail

# Prepare an Ubuntu/Debian Tencent Lighthouse host for the Compose deployment.
# The release trust root (public keys plus an independent fingerprint allowlist)
# must arrive through a root-controlled channel before this script is run.

APP_DIR=${APP_DIR:-/opt/skill-hub}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/skill-hub/postgres}
OPS_INSTALL_DIR=${OPS_INSTALL_DIR:-/usr/local/lib/skill-hub-ops}
DEPLOY_USER=${DEPLOY_USER:-${SUDO_USER:-skillhub}}
REPO_URL=${REPO_URL:-https://github.com/tark5139/skill-hub-public.git}
REPO_TAG=${REPO_TAG:-}
TRUSTED_TAG_SIGNERS_SOURCE=${TRUSTED_TAG_SIGNERS_SOURCE:-}
TRUSTED_TAG_PUBLIC_KEY_FILE=${TRUSTED_TAG_PUBLIC_KEY_FILE:-}
TRUSTED_TAG_SIGNERS=/etc/skill-hub/trusted-tag-signers
TRUSTED_TAG_PUBLIC_KEYS=/etc/skill-hub/release-tag-public-keys.asc
AWS_CONFIG=/etc/skill-hub/aws-config
REQUIRE_COS_BACKUP_BEFORE_BOOTSTRAP=${REQUIRE_COS_BACKUP_BEFORE_BOOTSTRAP:-1}
CONFIGURE_UFW=${CONFIGURE_UFW:-0}
ADMIN_CIDR=${ADMIN_CIDR:-}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-personal-skill-hub}
LOCK_FILE=/var/lock/skill-hub/ops.lock
BOOTSTRAP_LOCK_HELD=0

log() {
  printf '[bootstrap] %s\n' "$*"
}

fail() {
  printf '[bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

as_deploy_user() {
  runuser --user "$DEPLOY_USER" -- "$@"
}

acquire_bootstrap_lock() {
  [[ ${BOOTSTRAP_LOCK_HELD} == 0 ]] || return 0
  [[ -x /usr/bin/flock ]] || fail '/usr/bin/flock is required before bootstrap can lock an existing host'
  [[ -f ${LOCK_FILE} && ! -L ${LOCK_FILE} ]] || \
    fail "${LOCK_FILE} must be a regular, non-symlink file"
  exec {SKILLHUB_OPS_LOCK_FD}>"$LOCK_FILE" || \
    fail "cannot open the operations lock: ${LOCK_FILE}"
  /usr/bin/flock --nonblock "$SKILLHUB_OPS_LOCK_FD" || \
    fail 'another deploy, rollback, backup, restore, or bootstrap operation is running'
  export SKILLHUB_OPS_LOCK_FD
  export SKILLHUB_OPS_LOCK_HELD=1
  BOOTSTRAP_LOCK_HELD=1
}

validate_root_trust_source() {
  local source_file=$1
  local description=$2
  local permissions

  [[ ${source_file} == /* ]] || fail "${description} must use an absolute path"
  [[ -f ${source_file} && ! -L ${source_file} ]] || \
    fail "${description} must be a regular, non-symlink file"
  [[ $(stat -c '%U' "$source_file") == root ]] || \
    fail "${description} must be owned by root"
  permissions=$(stat -c '%a' "$source_file")
  case "$permissions" in
    400 | 440 | 444 | 600 | 640 | 644) ;;
    *) fail "${description} must not be writable by group or others" ;;
  esac
}

validate_fingerprint_allowlist() {
  local allowlist=$1

  awk '
    /^[[:space:]]*(#|$)/ { next }
    {
      value = toupper($0)
      gsub(/[[:space:]]/, "", value)
      if (value !~ /^[0-9A-F]+$/ || (length(value) != 40 && length(value) != 64)) {
        exit 1
      }
      found = 1
    }
    END { if (!found) exit 1 }
  ' "$allowlist" || fail 'trusted signer allowlist must contain valid full fingerprints'
}

verify_trusted_tag_as_deploy_user() {
  local repository=$1
  local tag=$2
  local verification fingerprints allowed fingerprint

  as_deploy_user /usr/bin/git -C "$repository" show-ref --verify --quiet \
    "refs/tags/${tag}" || return 1
  verification=$(as_deploy_user /usr/bin/git -c gpg.program=/usr/bin/gpg -C "$repository" \
    verify-tag --raw -- "$tag" 2>&1) || return 1
  fingerprints=$(printf '%s\n' "$verification" | awk '
    $1 == "[GNUPG:]" && $2 == "VALIDSIG" {
      for (field = 3; field <= NF; field += 1) {
        value = toupper($field)
        if (value ~ /^[0-9A-F]+$/ && (length(value) == 40 || length(value) == 64)) {
          print value
        }
      }
    }
  ')
  [[ -n ${fingerprints} ]] || return 1
  allowed=$(awk '
    !/^[[:space:]]*(#|$)/ {
      value = toupper($0)
      gsub(/[[:space:]]/, "", value)
      print value
    }
  ' "$TRUSTED_TAG_SIGNERS")
  while IFS= read -r fingerprint; do
    if grep -Fxq "$fingerprint" <<<"$allowed"; then
      return 0
    fi
  done <<<"$fingerprints"
  return 1
}

if [[ ${EUID} -ne 0 ]]; then
  fail 'run as root (for example: sudo -E ./bootstrap-lighthouse.sh)'
fi
[[ -n ${REPO_TAG} ]] || fail 'set REPO_TAG to one exact trusted signed tag'
[[ ! ${REPO_TAG} =~ ^[0-9a-fA-F]{40}$ ]] || \
  fail 'a raw 40-character commit is not a trusted release identity; use a signed tag'
[[ ${REPO_TAG} != -* ]] || fail 'REPO_TAG must not begin with a hyphen'
[[ -n ${TRUSTED_TAG_SIGNERS_SOURCE} ]] || \
  fail 'set TRUSTED_TAG_SIGNERS_SOURCE to the root-owned fingerprint allowlist'
[[ -n ${TRUSTED_TAG_PUBLIC_KEY_FILE} ]] || \
  fail 'set TRUSTED_TAG_PUBLIC_KEY_FILE to the independently obtained OpenPGP public-key bundle'
[[ ${REQUIRE_COS_BACKUP_BEFORE_BOOTSTRAP} =~ ^[01]$ ]] || \
  fail 'REQUIRE_COS_BACKUP_BEFORE_BOOTSTRAP must be 0 or 1'
if [[ ${REPO_URL} =~ ^https?://[^/]*@ ]]; then
  fail 'REPO_URL must not embed credentials; use a public URL or configured SSH credentials'
fi
validate_root_trust_source "$TRUSTED_TAG_SIGNERS_SOURCE" 'trusted signer allowlist source'
validate_root_trust_source "$TRUSTED_TAG_PUBLIC_KEY_FILE" 'trusted tag public-key source'
validate_fingerprint_allowlist "$TRUSTED_TAG_SIGNERS_SOURCE"

if [[ ! -r /etc/os-release ]]; then
  fail '/etc/os-release is unavailable; only Ubuntu and Debian are supported'
fi
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
  ubuntu | debian) ;;
  *) fail "unsupported operating system: ${ID:-unknown}" ;;
esac

# Re-running bootstrap on a live host may replace the root-owned operations
# control plane. Secure a recovery point before package updates or any Git fetch.
if [[ -d ${APP_DIR}/.git ]]; then
  # Existing hosts already have util-linux and the root-owned lock from the
  # previous bootstrap. Hold the same lock through backup, apt, checkout, and
  # replacement of the stable operations control plane.
  acquire_bootstrap_lock
  command -v docker >/dev/null 2>&1 || \
    fail 'existing checkout found but Docker is unavailable; inspect the host manually'
  postgres_ids=$(docker ps \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter 'label=com.docker.compose.service=postgres' --format '{{.ID}}')
  postgres_count=$(printf '%s\n' "$postgres_ids" | awk 'NF { count += 1 } END { print count + 0 }')
  existing_volumes=$(docker volume ls \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter 'label=com.docker.compose.volume=postgres_data' --format '{{.Name}}')
  if [[ ${postgres_count} -eq 1 ]]; then
    [[ -x ${OPS_INSTALL_DIR}/backup.sh ]] || \
      fail "existing deployment requires stable ${OPS_INSTALL_DIR}/backup.sh before bootstrap"
    log 'creating a stable pre-bootstrap PostgreSQL/COS recovery point'
    LOCK_FILE="$LOCK_FILE" SKILLHUB_OPS_LOCK_HELD=1 \
      REQUIRE_COS_BACKUP="$REQUIRE_COS_BACKUP_BEFORE_BOOTSTRAP" \
      "${OPS_INSTALL_DIR}/backup.sh" || fail 'pre-bootstrap recovery point failed'
  elif [[ ${postgres_count} -ne 0 || -n ${existing_volumes} ]]; then
    fail 'PostgreSQL state exists without exactly one running container; recover it before bootstrap'
  fi
fi

export DEBIAN_FRONTEND=noninteractive
log 'installing host prerequisites'
apt-get update
apt-get install --yes --no-install-recommends \
  awscli ca-certificates curl git gnupg ufw util-linux
/usr/bin/git check-ref-format "refs/tags/${REPO_TAG}" >/dev/null 2>&1 || \
  fail 'REPO_TAG must be one exact tag name; branches and malformed refs are refused'

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  log 'installing Docker Engine and Compose v2 from the Docker apt repository'
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  architecture=$(dpkg --print-architecture)
  codename=${VERSION_CODENAME:-}
  [[ -n ${codename} ]] || fail 'VERSION_CODENAME is missing from /etc/os-release'
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$architecture" "$ID" "$codename" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install --yes --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  log "creating non-root deployment user: ${DEPLOY_USER}"
  useradd --create-home --user-group --shell /bin/bash "$DEPLOY_USER"
fi
usermod --append --groups docker "$DEPLOY_USER"
DEPLOY_GROUP=$(id -gn "$DEPLOY_USER")

install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$APP_DIR"
install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$BACKUP_DIR"
install -d -m 0750 -o root -g "$DEPLOY_GROUP" /etc/skill-hub
install -m 0644 -o root -g root "$TRUSTED_TAG_SIGNERS_SOURCE" "$TRUSTED_TAG_SIGNERS"
install -m 0644 -o root -g root "$TRUSTED_TAG_PUBLIC_KEY_FILE" "$TRUSTED_TAG_PUBLIC_KEYS"

public_key_fingerprints=$(as_deploy_user /usr/bin/gpg --batch --no-tty --with-colons \
  --import-options show-only --import "$TRUSTED_TAG_PUBLIC_KEYS" 2>/dev/null | \
  awk -F: '$1 == "fpr" { print toupper($10) }') || \
  fail 'failed to parse the supplied OpenPGP public-key bundle'
[[ -n ${public_key_fingerprints} ]] || fail 'the release public-key bundle contains no OpenPGP key'
allowed_fingerprints=$(awk '
  !/^[[:space:]]*(#|$)/ {
    value = toupper($0)
    gsub(/[[:space:]]/, "", value)
    print value
  }
' "$TRUSTED_TAG_SIGNERS")
matching_fingerprint=0
while IFS= read -r fingerprint; do
  if grep -Fxq "$fingerprint" <<<"$allowed_fingerprints"; then
    matching_fingerprint=1
    break
  fi
done <<<"$public_key_fingerprints"
[[ ${matching_fingerprint} == 1 ]] || \
  fail 'the supplied public-key material does not match the trusted fingerprint allowlist'
as_deploy_user /usr/bin/gpg --batch --no-tty --import "$TRUSTED_TAG_PUBLIC_KEYS" \
  >/dev/null 2>&1 || \
  fail "failed to import release public keys into ${DEPLOY_USER}'s GnuPG keyring"

printf 'd /run/lock/skill-hub 0750 root %s -\nf /run/lock/skill-hub/ops.lock 0660 root %s -\n' \
  "$DEPLOY_GROUP" "$DEPLOY_GROUP" > /etc/tmpfiles.d/skill-hub.conf
systemd-tmpfiles --create /etc/tmpfiles.d/skill-hub.conf
install -d -m 0750 -o root -g "$DEPLOY_GROUP" /var/lock/skill-hub
if [[ ! -e /var/lock/skill-hub/ops.lock ]]; then
  install -m 0660 -o root -g "$DEPLOY_GROUP" /dev/null /var/lock/skill-hub/ops.lock
else
  [[ -f /var/lock/skill-hub/ops.lock && ! -L /var/lock/skill-hub/ops.lock ]] || \
    fail '/var/lock/skill-hub/ops.lock must be a regular, non-symlink file'
  chown "root:$DEPLOY_GROUP" /var/lock/skill-hub/ops.lock
  chmod 0660 /var/lock/skill-hub/ops.lock
fi
acquire_bootstrap_lock

if [[ ! -d ${APP_DIR}/.git ]]; then
  initial_postgres=$(docker ps --all \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter 'label=com.docker.compose.service=postgres' --format '{{.ID}}')
  initial_volumes=$(docker volume ls \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter 'label=com.docker.compose.volume=postgres_data' --format '{{.Name}}')
  [[ -z ${initial_postgres} && -z ${initial_volumes} ]] || \
    fail 'an unowned Skill Hub PostgreSQL container or volume exists; recover it before bootstrap'
  if [[ -n $(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
    fail "${APP_DIR} is not empty and is not a Git checkout"
  fi
  log "cloning ${REPO_URL} without checking out unverified content"
  as_deploy_user /usr/bin/git clone --no-checkout "$REPO_URL" "$APP_DIR"
else
  as_deploy_user test -w "${APP_DIR}/.git" || \
    fail "${DEPLOY_USER} cannot write to ${APP_DIR}/.git"
  actual_remote=$(as_deploy_user /usr/bin/git -C "$APP_DIR" remote get-url origin)
  [[ ${actual_remote} == "$REPO_URL" ]] || \
    fail "origin is ${actual_remote}; expected ${REPO_URL}"
fi

log "fetching and verifying requested release tag: ${REPO_TAG}"
as_deploy_user /usr/bin/git -C "$APP_DIR" fetch --tags --prune origin
verify_trusted_tag_as_deploy_user "$APP_DIR" "$REPO_TAG" || \
  fail "tag is unsigned, invalid, or not signed by an allowed fingerprint: ${REPO_TAG}"
resolved_ref=$(as_deploy_user /usr/bin/git -C "$APP_DIR" rev-parse --verify \
  "refs/tags/${REPO_TAG}^{commit}")
as_deploy_user /usr/bin/git -C "$APP_DIR" checkout --detach "$resolved_ref"

for required_script in common.sh backup.sh deploy.sh healthcheck.sh restore.sh rollback.sh; do
  [[ -f ${APP_DIR}/ops/tencent/${required_script} ]] || \
    fail "verified release lacks ops/tencent/${required_script}"
done
install -d -m 0750 -o root -g "$DEPLOY_GROUP" "$OPS_INSTALL_DIR"
for required_script in common.sh backup.sh deploy.sh healthcheck.sh restore.sh rollback.sh; do
  install -m 0750 -o root -g "$DEPLOY_GROUP" \
    "${APP_DIR}/ops/tencent/${required_script}" "${OPS_INSTALL_DIR}/${required_script}"
done
[[ -f ${APP_DIR}/ops/tencent/aws-config && ! -L ${APP_DIR}/ops/tencent/aws-config ]] || \
  fail 'verified release lacks a regular ops/tencent/aws-config file'
install -m 0644 -o root -g root "${APP_DIR}/ops/tencent/aws-config" "$AWS_CONFIG"

if [[ ! -e ${APP_DIR}/.env ]]; then
  install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" /dev/null "${APP_DIR}/.env"
  log "created an empty ${APP_DIR}/.env; populate it from ops/tencent/.env.example"
else
  [[ -f ${APP_DIR}/.env && ! -L ${APP_DIR}/.env ]] || \
    fail "${APP_DIR}/.env must be a regular, non-symlink file"
  chown "$DEPLOY_USER:$DEPLOY_GROUP" "${APP_DIR}/.env"
  chmod 0600 "${APP_DIR}/.env"
fi

if [[ ${CONFIGURE_UFW} == 1 ]]; then
  [[ -n ${ADMIN_CIDR} ]] || \
    fail 'CONFIGURE_UFW=1 requires ADMIN_CIDR (for example 203.0.113.10/32)'
  log 'configuring an opt-in deny-by-default UFW policy'
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow proto tcp from "$ADMIN_CIDR" to any port 22
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw allow 443/udp
  ufw --force enable
else
  log 'host firewall unchanged; Tencent security groups are never modified by this script'
fi

command -v aws >/dev/null 2>&1 || fail 'AWS CLI installation did not produce an aws executable'

cat <<EOF

Bootstrap complete.
  Application: ${APP_DIR}
  Deployment user: ${DEPLOY_USER}
  Trusted release tag: ${REPO_TAG}
  Resolved revision: ${resolved_ref}
  Signer allowlist: ${TRUSTED_TAG_SIGNERS}
  Public-key bundle: ${TRUSTED_TAG_PUBLIC_KEYS}
  Backups: ${BACKUP_DIR}
  Stable operations: ${OPS_INSTALL_DIR}

Before deployment:
  1. Configure the Tencent security group: SSH only from the management CIDR; public 80/443 only.
  2. Do not open 5432 or 8080.
  3. Populate ${APP_DIR}/.env and keep mode 0600.
  4. Start a new login session so Docker group membership takes effect.
EOF
