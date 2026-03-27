#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:-hireme/env}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/venv/bin/python"
fi

aws sts get-caller-identity >/dev/null

SECRET_JSON="$(aws secretsmanager get-secret-value \
  --region "$AWS_REGION" \
  --secret-id "$SECRET_ID" \
  --query SecretString \
  --output text)"

if [[ -z "$SECRET_JSON" || "$SECRET_JSON" == "None" ]]; then
  echo "SecretString is empty for secret: $SECRET_ID"
  exit 1
fi

TMP_ENV="$(mktemp)"

"$PYTHON_BIN" - "$SECRET_JSON" > "$TMP_ENV" <<'PY'
import json
import re
import sys

raw = sys.argv[1]
obj = json.loads(raw)
if not isinstance(obj, dict):
    raise SystemExit("SecretString must be a JSON object")

def encode(value: object) -> str:
    s = "" if value is None else str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:@+,-]*", s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'

for key in sorted(obj.keys()):
    print(f"{key}={encode(obj[key])}")
PY

mv "$TMP_ENV" "$ENV_FILE"
echo "Wrote $ENV_FILE from secret: $SECRET_ID ($AWS_REGION)"
