#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="$(cd ~ && pwd)"
LOG_FILE="${ZTSYNC_LOG_FILE:-${USER_HOME}/ztsync.log}"

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv não está disponível no PATH\n' >&2
    exit 1
fi

cd "$PROJECT_DIR"
nohup uv run --no-dev --no-sync ztsync service >>"$LOG_FILE" 2>&1 </dev/null &
SERVICE_PID=$!

printf 'ztsync iniciado em background (PID %s)\n' "$SERVICE_PID"
printf 'logs: %s\n' "$LOG_FILE"
