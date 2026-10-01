#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT="/home/zbw21/projects/cs2-price-monitor"
APP="$PROJECT/src/cs2_price_monitor/app.py"
LOG="$PROJECT/logs/streamlit.log"

mkdir -p "$PROJECT/logs"
cd "$PROJECT"

# Do not start a duplicate Streamlit instance.
if pgrep -f "[s]treamlit.*$APP" >/dev/null; then
    exit 0
fi

# Keep Streamlit attached to the background WSL process.
exec nice -n 10 "$PROJECT/.venv/bin/streamlit" run "$APP" \
    --server.address 0.0.0.0 \
    --server.port 8502 \
    --server.headless true \
    --server.fileWatcherType none \
    >> "$LOG" 2>&1
