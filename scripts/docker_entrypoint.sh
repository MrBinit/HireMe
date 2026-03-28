#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/venv/bin/python"
fi

USE_AWS_SECRETS_MANAGER="${USE_AWS_SECRETS_MANAGER:-false}"
if [[ "$USE_AWS_SECRETS_MANAGER" == "true" ]]; then
  # By default keep existing environment values for local overrides.
  # Set SECRETS_MANAGER_PREFER_ENV=false to force secret values.
  SECRETS_MANAGER_PREFER_ENV="${SECRETS_MANAGER_PREFER_ENV:-true}"
  for key in AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_REGION; do
    if [[ -z "${!key:-}" ]]; then
      unset "$key"
    fi
  done
  if [[ -z "${AWS_PROFILE:-}" ]]; then
    unset AWS_PROFILE
  fi
  AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
  SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:?AWS_SECRETS_MANAGER_SECRET_ID is required}"

  SECRET_JSON="$(aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$SECRET_ID" \
    --query SecretString \
    --output text)"

  if [[ -z "$SECRET_JSON" || "$SECRET_JSON" == "None" ]]; then
    echo "SecretString is empty for secret: $SECRET_ID"
    exit 1
  fi

  TMP_KV="$(mktemp)"
  "$PYTHON_BIN" - "$SECRET_JSON" > "$TMP_KV" <<'PY'
import json
import re
import sys

obj = json.loads(sys.argv[1])
if not isinstance(obj, dict):
    raise SystemExit("SecretString must be a JSON object")

key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
for key, value in obj.items():
    if not isinstance(key, str) or not key_re.match(key):
        raise SystemExit(f"Invalid env key in secret: {key!r}")
    out = "" if value is None else str(value)
    sys.stdout.write(key)
    sys.stdout.write("\0")
    sys.stdout.write(out)
    sys.stdout.write("\0")
PY

  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    if [[ "$SECRETS_MANAGER_PREFER_ENV" == "true" && -n "${!key-}" ]]; then
      continue
    fi
    export "$key=$value"
  done < "$TMP_KV"
  rm -f "$TMP_KV"

  echo "Loaded runtime environment from AWS Secrets Manager: $SECRET_ID ($AWS_REGION)"
fi

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
