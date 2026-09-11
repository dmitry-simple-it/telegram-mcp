#!/usr/bin/env bash
set -euo pipefail

LABEL="com.telegram-mcp.server"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_TEMPLATE="$SCRIPT_DIR/com.telegram-mcp.server.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/telegram-mcp"
HEALTH_URL="http://127.0.0.1:8765/health"

usage() {
    echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs|health}"
    exit 1
}

cmd_install() {
    echo "Installing $LABEL..."

    # Resolve paths
    local uv_path
    uv_path="$(which uv 2>/dev/null || true)"
    if [[ -z "$uv_path" ]]; then
        echo "Error: uv not found in PATH" >&2
        exit 1
    fi

    # Create log directory
    mkdir -p "$LOG_DIR"

    # Unload if already loaded
    launchctl list "$LABEL" &>/dev/null && launchctl unload "$PLIST_DST" 2>/dev/null || true

    # Generate plist with resolved paths
    sed \
        -e "s|__UV_PATH__|$uv_path|g" \
        -e "s|__PROJECT_PATH__|$SCRIPT_DIR|g" \
        -e "s|__HOME__|$HOME|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DST"

    # Load and start
    launchctl load "$PLIST_DST"
    echo "Installed and started. Check: $0 health"
}

cmd_uninstall() {
    echo "Uninstalling $LABEL..."
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl unload "$PLIST_DST"
    fi
    rm -f "$PLIST_DST"
    echo "Uninstalled."
}

cmd_start() {
    launchctl start "$LABEL"
    echo "Started $LABEL"
}

cmd_stop() {
    launchctl stop "$LABEL"
    echo "Stopped $LABEL"
}

cmd_restart() {
    echo "Restarting $LABEL..."
    launchctl stop "$LABEL" 2>/dev/null || true
    sleep 2
    launchctl start "$LABEL"
    echo "Restarted. Check: $0 health"
}

cmd_status() {
    echo "=== launchd status ==="
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl list "$LABEL"
    else
        echo "Not loaded"
    fi
    echo ""
    echo "=== health check ==="
    curl -s --max-time 3 "$HEALTH_URL" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Server not responding"
}

cmd_logs() {
    tail -f "$LOG_DIR/stderr.log"
}

cmd_health() {
    curl -s --max-time 3 "$HEALTH_URL" | python3 -m json.tool
}

[[ $# -lt 1 ]] && usage

case "$1" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    health)    cmd_health ;;
    *)         usage ;;
esac
