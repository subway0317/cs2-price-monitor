#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR="/home/zbw21/projects/cs2-price-monitor"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_sync.log"
LOCK_FILE="$LOG_DIR/daily_sync.lock"

mkdir -p "$LOG_DIR"

{
  echo "===== daily sync start $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
  cd "$PROJECT_DIR" || exit 2
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Another daily sync is already running."
    echo "===== daily sync skipped $(date -u +"%Y-%m-%dT%H:%M:%SZ") ====="
    exit 75
  fi
  nice -n 10 "$PYTHON_BIN" -m cs2_price_monitor.cli sync-all
  status=$?
  echo "===== daily sync end $(date -u +"%Y-%m-%dT%H:%M:%SZ") status=$status ====="
  exit "$status"
} >>"$LOG_FILE" 2>&1
