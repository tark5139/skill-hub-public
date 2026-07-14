#!/usr/bin/env bash
set -euo pipefail

# Shared helpers for Tencent operations. This file may be sourced; it performs no
# operation on its own and never prints environment values.

skillhub_read_env_value() {
  local env_file=$1
  local key=$2
  local value

  value=$(awk -v wanted="${key}=" '
    index($0, wanted) == 1 {
      values += 1
      value = substr($0, length(wanted) + 1)
    }
    END {
      if (values != 1) {
        exit(values == 0 ? 1 : 2)
      }
      print value
    }
  ' "$env_file") || return $?
  value=${value%$'\r'}
  if [[ ${value} == \"*\" && ${value} == *\" ]]; then
    value=${value:1:${#value}-2}
  elif [[ ${value} == \'*\' && ${value} == *\' ]]; then
    value=${value:1:${#value}-2}
  fi
  printf '%s' "$value"
}

skillhub_acquire_lock() {
  local lock_file=$1

  if [[ ${SKILLHUB_OPS_LOCK_HELD:-0} == 1 ]]; then
    return 0
  fi
  command -v flock >/dev/null 2>&1 || return 69
  [[ -f ${lock_file} && ! -L ${lock_file} ]] || return 72
  exec {SKILLHUB_OPS_LOCK_FD}>"$lock_file" || return 73
  flock --nonblock "$SKILLHUB_OPS_LOCK_FD" || return 75
  export SKILLHUB_OPS_LOCK_FD
}

skillhub_verify_checksum() {
  local backup_path=$1
  local backup_name recorded_digest recorded_name extra actual_digest

  [[ -f ${backup_path}.sha256 && ! -L ${backup_path}.sha256 && -s ${backup_path}.sha256 ]] || \
    return 66
  backup_name=$(basename -- "$backup_path")
  read -r recorded_digest recorded_name extra < "${backup_path}.sha256" || return 65
  [[ -z ${extra:-} && ${recorded_name} == "$backup_name" ]] || return 65
  [[ ${recorded_digest} =~ ^[0-9a-fA-F]{64}$ ]] || return 65
  if command -v sha256sum >/dev/null 2>&1; then
    actual_digest=$(sha256sum "$backup_path" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then
    actual_digest=$(shasum -a 256 "$backup_path" | awk '{print $1}')
  else
    return 69
  fi
  recorded_digest=$(printf '%s' "$recorded_digest" | tr '[:upper:]' '[:lower:]')
  [[ ${recorded_digest} == "$actual_digest" ]]
}

skillhub_verify_trusted_tag() {
  local repository=$1
  local tag=$2
  local allowlist=$3
  local permissions verification fingerprints fingerprint allowed

  [[ -f ${allowlist} && ! -L ${allowlist} ]] || return 72
  [[ $(stat -c '%U' "$allowlist") == root ]] || return 77
  permissions=$(stat -c '%a' "$allowlist")
  [[ ${permissions} == 644 || ${permissions} == 640 || ${permissions} == 600 ]] || return 77
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
  ' "$allowlist" || return 65

  [[ -x /usr/bin/git && -x /usr/bin/gpg ]] || return 69
  /usr/bin/git -C "$repository" show-ref --verify --quiet "refs/tags/${tag}" || return 66
  verification=$(/usr/bin/git -c gpg.program=/usr/bin/gpg -C "$repository" \
    verify-tag --raw -- "$tag" 2>&1) || return 67
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
  [[ -n ${fingerprints} ]] || return 67

  allowed=$(awk '
    !/^[[:space:]]*(#|$)/ {
      value = toupper($0)
      gsub(/[[:space:]]/, "", value)
      print value
    }
  ' "$allowlist")
  while IFS= read -r fingerprint; do
    if grep -Fxq "$fingerprint" <<<"$allowed"; then
      return 0
    fi
  done <<<"$fingerprints"
  return 77
}

skillhub_container_ids() {
  local project=$1
  local service=$2
  local include_stopped=${3:-0}
  local -a command=(docker ps)

  if [[ ${include_stopped} == 1 ]]; then
    command+=(--all)
  fi
  command+=(
    --filter "label=com.docker.compose.project=${project}"
    --filter "label=com.docker.compose.service=${service}"
    --format '{{.ID}}'
  )
  "${command[@]}"
}

skillhub_stop_services_by_label() {
  local project=$1
  shift
  local service container_ids container_id
  local failed=0

  for service in "$@"; do
    if ! container_ids=$(skillhub_container_ids "$project" "$service"); then
      failed=1
      continue
    fi
    while IFS= read -r container_id; do
      [[ -n ${container_id} ]] || continue
      if ! docker stop "$container_id" >/dev/null 2>&1; then
        failed=1
      fi
    done <<<"$container_ids"
  done
  return "$failed"
}
