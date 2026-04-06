#!/bin/bash
# Start the copy engine if not already running
PIDFILE="/home/rob/reef-workspace/data/copy_engine.pid"
LOGFILE="/home/rob/reef-workspace/cron/copy_engine.log"

if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
    exit 0
fi

cd /home/rob/reef-workspace
nohup venv/bin/python copy_engine.py >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
