#!/bin/bash
# Start the copy engine if not already running
# Run: bash start_copy_engine.sh

WORKSPACE="/home/rob/reef-workspace"
CE_PIDFILE="$WORKSPACE/data/copy_engine.pid"
CE_LOGFILE="$WORKSPACE/cron/copy_engine.log"

# Kill stale process if pidfile exists but process is dead
if [ -f "$CE_PIDFILE" ]; then
    PID=$(cat "$CE_PIDFILE")
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "Stale PID $PID — cleaning up"
        rm -f "$CE_PIDFILE"
    fi
fi

# Start copy engine
if [ -f "$CE_PIDFILE" ] && kill -0 "$(cat "$CE_PIDFILE")" 2>/dev/null; then
    echo "copy_engine already running (PID $(cat "$CE_PIDFILE"))"
else
    cd "$WORKSPACE"
    rm -rf __pycache__
    nohup venv/bin/python -u copy_engine.py >> "$CE_LOGFILE" 2>&1 &
    echo $! > "$CE_PIDFILE"
    echo "copy_engine started (PID $!)"
    sleep 3
    tail -15 "$CE_LOGFILE"
fi
