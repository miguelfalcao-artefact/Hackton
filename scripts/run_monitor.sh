#!/usr/bin/env bash
# Start and supervise the collector and dashboard as one local service.
set -euo pipefail

readonly project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly env_file="$project_dir/.env"

cd "$project_dir"

# Export .env assignments so both Python processes share one configuration.
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$env_file"
  set +a
fi

readonly collector_interval="${COLLECTOR_INTERVAL_SECONDS:-30}"
readonly dashboard_interval="${DASHBOARD_INTERVAL_SECONDS:-2}"
readonly dashboard_host="${DASHBOARD_HOST:-127.0.0.1}"
readonly dashboard_port="${DASHBOARD_PORT:-8000}"

collector_pid=""
dashboard_pid=""

cleanup() {
  # Stop whichever children started; wait prevents zombie processes.
  local pid
  for pid in "$collector_pid" "$dashboard_pid"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  for pid in "$collector_pid" "$dashboard_pid"; do
    [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
  done
}

trap cleanup EXIT INT TERM

echo "Starting CSV collector and single-HTML dashboard agents. Press Ctrl+C to stop."

# CLI timing values override Python defaults; paths come from the environment.
python3 agent/monitor.py \
  --interval "$collector_interval" \
  &
collector_pid=$!

python3 agent/dashboard.py \
  --interval "$dashboard_interval" \
  --serve \
  --host "$dashboard_host" \
  --port "$dashboard_port" \
  &
dashboard_pid=$!

# Exit when either child exits; the trap then stops the remaining process.
wait -n "$collector_pid" "$dashboard_pid"
