#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="$ROOT_DIR/venv/bin/python"
PID_DIR="$ROOT_DIR/tmp"
mkdir -p "$PID_DIR"

USE_AWS_SECRETS_MANAGER="${USE_AWS_SECRETS_MANAGER:-false}"
if [[ "$USE_AWS_SECRETS_MANAGER" == "true" ]]; then
  "$ROOT_DIR/scripts/aws_secrets_to_env.sh"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing virtualenv python at $PYTHON_BIN"
  exit 1
fi

WORKER_NAMES=(
  "sqs_worker"
  "sqs_evaluation_worker"
  "sqs_research_enrichment_worker"
  "sqs_scheduling_worker"
  "interview_hold_expiry_worker"
)

start_worker() {
  local name="$1"
  local module="app.scripts.${name}"
  local pid_file="$PID_DIR/hireme_${name}.pid"
  local log_file="/tmp/hireme_${name}.log"

  if [[ -f "$pid_file" ]]; then
    local old_pid
    old_pid="$(cat "$pid_file" || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[skip] $name already running (pid=$old_pid)"
      return 0
    fi
    rm -f "$pid_file"
  fi

  nohup "$PYTHON_BIN" -m "$module" >"$log_file" 2>&1 &
  local pid=$!
  echo "$pid" >"$pid_file"
  echo "[ok] started $name (pid=$pid, log=$log_file)"
}

stop_worker() {
  local name="$1"
  local pid_file="$PID_DIR/hireme_${name}.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "[skip] $name not running (no pid file)"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" || true)"
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" || true
    fi
    echo "[ok] stopped $name (pid=$pid)"
  else
    echo "[skip] $name pid file exists but process is not running"
  fi
  rm -f "$pid_file"
}

status_worker() {
  local name="$1"
  local pid_file="$PID_DIR/hireme_${name}.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "[down] $name"
    return 0
  fi
  local pid
  pid="$(cat "$pid_file" || true)"
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "[up]   $name (pid=$pid)"
  else
    echo "[down] $name (stale pid file)"
  fi
}

action="${1:-start}"
case "$action" in
  start)
    for name in "${WORKER_NAMES[@]}"; do
      start_worker "$name"
    done
    ;;
  stop)
    for name in "${WORKER_NAMES[@]}"; do
      stop_worker "$name"
    done
    ;;
  status)
    for name in "${WORKER_NAMES[@]}"; do
      status_worker "$name"
    done
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  *)
    echo "Usage: $0 {start|stop|status|restart}"
    exit 1
    ;;
esac
