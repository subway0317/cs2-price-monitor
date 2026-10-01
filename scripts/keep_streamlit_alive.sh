#!/usr/bin/env bash

set -Eeuo pipefail

SERVICE="cs2-price-monitor.service"
LOCK_FILE="/run/cs2-price-monitor-keeper.lock"

# Prevent two keeper processes from running at the same time.
exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    exit 0
fi

systemctl start "$SERVICE"

# Wait up to 30 seconds for the service to become active.
for _ in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE"; then
        break
    fi

    sleep 1
done

if ! systemctl is-active --quiet "$SERVICE"; then
    journalctl -u "$SERVICE" -n 50 --no-pager >&2
    exit 1
fi

# Keep this WSL process alive while Streamlit is running.
while systemctl is-active --quiet "$SERVICE"; do
    sleep 2
done
