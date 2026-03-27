#!/usr/bin/env bash
set -euo pipefail

load_docker_runtime_env() {
  local config_file="${DOCKER_RUNTIME_CONFIG_FILE:-$ROOT_DIR/config/docker_runtime.yaml}"
  if [[ ! -f "$config_file" ]]; then
    echo "Missing Docker runtime config: $config_file" >&2
    return 1
  fi

  local region default_region secret_id profile
  region="$(sed -nE 's/^[[:space:]]*region:[[:space:]]*"?([^"#]+)"?[[:space:]]*(#.*)?$/\1/p' "$config_file" | head -n 1 | xargs)"
  default_region="$(sed -nE 's/^[[:space:]]*default_region:[[:space:]]*"?([^"#]+)"?[[:space:]]*(#.*)?$/\1/p' "$config_file" | head -n 1 | xargs)"
  secret_id="$(sed -nE 's/^[[:space:]]*secrets_manager_secret_id:[[:space:]]*"?([^"#]+)"?[[:space:]]*(#.*)?$/\1/p' "$config_file" | head -n 1 | xargs)"
  profile="$(sed -nE 's/^[[:space:]]*profile:[[:space:]]*"?([^"#]+)"?[[:space:]]*(#.*)?$/\1/p' "$config_file" | head -n 1 | xargs)"

  export AWS_REGION="${AWS_REGION:-$region}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$default_region}"
  export AWS_SECRETS_MANAGER_SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:-$secret_id}"
  export AWS_PROFILE="${AWS_PROFILE:-$profile}"

  : "${AWS_REGION:?Missing aws.region in $config_file}"
  : "${AWS_DEFAULT_REGION:?Missing aws.default_region in $config_file}"
  : "${AWS_SECRETS_MANAGER_SECRET_ID:?Missing aws.secrets_manager_secret_id in $config_file}"
  : "${AWS_PROFILE:?Missing aws.profile in $config_file}"
}
