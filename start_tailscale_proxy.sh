#!/bin/bash
# Start the tailscale reverse proxy if not already running
PIDFILE="/home/rob/reef-workspace/data/tailscale_proxy.pid"
LOGFILE="/home/rob/reef-workspace/data/tailscale_proxy.log"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0
fi

cd /home/rob/reef-workspace
nohup python3 tailscale_proxy.py >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
