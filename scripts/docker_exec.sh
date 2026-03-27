#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/scripts/load_docker_runtime_env.sh"
load_docker_runtime_env

SERVICE="${1:-api}"
shift || true

if [[ "$#" -eq 0 ]]; then
  set -- /bin/bash
fi

COMPOSE_DISABLE_ENV_FILE=1 docker compose exec "$SERVICE" "$@"
