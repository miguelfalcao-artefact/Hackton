#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

echo "Starting CSV collector and single-HTML dashboard agents. Press Ctrl+C to stop."
python3 agent/monitor.py \
  --api-log "$project_dir/error_generator/logs/api.jsonl" \
  --api-log "$project_dir/mock_api/logs/api.jsonl" \
  --csv "$project_dir/runtime/logs.csv" \
  --interval 30 \
  &
collector_pid=$!

python3 agent/dashboard.py \
  --csv "$project_dir/runtime/logs.csv" \
  --html "$project_dir/reports/ecommerce-mock-api/dashboard.html" \
  --source-root "$project_dir/mock_api" \
  --interval 2 \
  --serve \
  --host 127.0.0.1 \
  --port 8000 \
  &
dashboard_pid=$!

cleanup() {
  kill "$collector_pid" "$dashboard_pid" 2>/dev/null || true
  wait "$collector_pid" "$dashboard_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
wait -n "$collector_pid" "$dashboard_pid"
