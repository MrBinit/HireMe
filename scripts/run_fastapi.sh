#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

UVICORN_BIN="$ROOT_DIR/venv/bin/uvicorn"
PID_FILE="$ROOT_DIR/tmp/hireme_api.pid"
LOG_FILE="/tmp/hireme_api.log"
UVICORN_RELOAD="${UVICORN_RELOAD:-false}"

mkdir -p "$ROOT_DIR/tmp"

USE_AWS_SECRETS_MANAGER="${USE_AWS_SECRETS_MANAGER:-false}"
if [[ "$USE_AWS_SECRETS_MANAGER" == "true" ]]; then
  "$ROOT_DIR/scripts/aws_secrets_to_env.sh"
fi

if [[ ! -x "$UVICORN_BIN" ]]; then
  echo "Missing uvicorn at $UVICORN_BIN"
  exit 1
fi

start_api() {
  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid="$(cat "$PID_FILE" || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[skip] API already running (pid=$old_pid)"
      return 0
    fi
    rm -f "$PID_FILE"
  fi

  local uvicorn_args=(app.main:app --host 0.0.0.0 --port 8000)
  if [[ "$UVICORN_RELOAD" == "true" ]]; then
    uvicorn_args+=(--reload)
  fi

  nohup "$UVICORN_BIN" "${uvicorn_args[@]}" >"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  echo "[ok] started FastAPI (pid=$pid, reload=$UVICORN_RELOAD, log=$LOG_FILE)"
}

stop_api() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "[skip] API not running (no pid file)"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE" || true)"
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" || true
    fi
    echo "[ok] stopped API (pid=$pid)"
  else
    echo "[skip] API pid file exists but process is not running"
  fi
  rm -f "$PID_FILE"
}

status_api() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "[down] API"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE" || true)"
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "[up]   API (pid=$pid)"
  else
    echo "[down] API (stale pid file)"
  fi
}

action="${1:-start}"
case "$action" in
  start)
    start_api
    ;;
  stop)
    stop_api
    ;;
  status)
    status_api
    ;;
  restart)
    stop_api
    start_api
    ;;
  logs)
    tail -n 80 -f "$LOG_FILE"
    ;;
  *)
    echo "Usage: $0 {start|stop|status|restart|logs}"
    exit 1
    ;;
esac
