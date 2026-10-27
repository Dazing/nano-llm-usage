#!/bin/sh
# Open the dashboard in a Chromium app window: normal resizable window, no tabs or address bar.
# Usage: docs/open-dashboard-app.sh [URL]
# Without URL, the address comes from DASHBOARD_BIND_ADDRESS/DASHBOARD_HOST_PORT in the repo .env.
set -eu

env_file="$(dirname "$0")/../.env"
env_value() {
    [ -f "$env_file" ] && sed -n "s/^$1=//p" "$env_file" | tail -n 1
}

address="$(env_value DASHBOARD_BIND_ADDRESS)"
port="$(env_value DASHBOARD_HOST_PORT)"
url="${1:-http://${address:-127.0.0.1}:${port:-8080}/}"

# A dedicated profile keeps a running Chromium from taking over the launch.
exec "${CHROMIUM:-chromium}" \
    --app="$url" \
    --user-data-dir="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis-dashboard-app"
