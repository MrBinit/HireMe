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

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE"
  exit 1
fi

aws sts get-caller-identity >/dev/null

SECRET_JSON="$("$PYTHON_BIN" - "$ENV_FILE" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
values = {}
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[len("export "):]
    if "=" not in line:
        continue
    key, val = line.split("=", 1)
    key = key.strip()
    val = val.strip()
    if not key:
        continue
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    values[key] = val
print(json.dumps(values, separators=(",", ":"), ensure_ascii=False))
PY
)"

if aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$SECRET_ID" \
    --secret-string "$SECRET_JSON" >/dev/null
  echo "Updated secret: $SECRET_ID ($AWS_REGION)"
else
  aws secretsmanager create-secret \
    --region "$AWS_REGION" \
    --name "$SECRET_ID" \
    --secret-string "$SECRET_JSON" >/dev/null
  echo "Created secret: $SECRET_ID ($AWS_REGION)"
fi
