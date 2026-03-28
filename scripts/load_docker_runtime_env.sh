#!/usr/bin/env bash
set -euo pipefail

_read_yaml_scalar() {
  local file="$1"
  local key="$2"
  local value

  value="$(
    awk -F':' -v k="$key" '
      $1 ~ "^[[:space:]]*" k "[[:space:]]*$" {
        sub(/^[[:space:]]+/, "", $2)
        sub(/[[:space:]]+$/, "", $2)
        gsub(/^["'\''"]|["'\''"]$/, "", $2)
        print $2
        exit
      }
    ' "$file"
  )"
  printf "%s" "$value"
}

load_docker_runtime_env() {
  local root_dir
  root_dir="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  local config_file
  config_file="${DOCKER_RUNTIME_CONFIG:-$root_dir/config/docker_runtime.yaml}"

  if [[ ! -f "$config_file" ]]; then
    return 0
  fi

  local region default_region secret_id profile
  region="$(_read_yaml_scalar "$config_file" "region")"
  default_region="$(_read_yaml_scalar "$config_file" "default_region")"
  secret_id="$(_read_yaml_scalar "$config_file" "secrets_manager_secret_id")"
  profile="$(_read_yaml_scalar "$config_file" "profile")"

  if [[ -n "${region:-}" && -z "${AWS_REGION:-}" ]]; then
    export AWS_REGION="$region"
  fi
  if [[ -n "${default_region:-}" && -z "${AWS_DEFAULT_REGION:-}" ]]; then
    export AWS_DEFAULT_REGION="$default_region"
  fi
  if [[ -n "${secret_id:-}" && -z "${AWS_SECRETS_MANAGER_SECRET_ID:-}" ]]; then
    export AWS_SECRETS_MANAGER_SECRET_ID="$secret_id"
  fi
  if [[ -n "${profile:-}" && -z "${AWS_PROFILE:-}" ]]; then
    export AWS_PROFILE="$profile"
  fi
}

