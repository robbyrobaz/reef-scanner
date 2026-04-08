#!/bin/bash
# Start the copy engine and tailscale proxy if not already running

WORKSPACE="/home/rob/reef-workspace"
CE_PIDFILE="$WORKSPACE/data/copy_engine.pid"
CE_LOGFILE="$WORKSPACE/cron/copy_engine.log"
PROXY_PIDFILE="$WORKSPACE/data/tailscale_proxy.pid"
PROXY_LOGFILE="$WORKSPACE/data/tailscale_proxy.log"

# Start copy engine
if [ -f "$CE_PIDFILE" ] && kill -0 "$(cat "$CE_PIDFILE")" 2>/dev/null; then
    echo "copy_engine already running"
else
    cd "$WORKSPACE"
    nohup venv/bin/python copy_engine.py >> "$CE_LOGFILE" 2>&1 &
    echo $! > "$CE_PIDFILE"
    echo "copy_engine started"
fi

# Start tailscale proxy
if [ -f "$PROXY_PIDFILE" ] && kill -0 "$(cat "$PROXY_PIDFILE")" 2>/dev/null; then
    echo "tailscale_proxy already running"
else
    cd "$WORKSPACE"
    nohup python3 tailscale_proxy.py >> "$PROXY_LOGFILE" 2>&1 &
    echo $! > "$PROXY_PIDFILE"
    echo "tailscale_proxy started"
fi
