#!/bin/bash
# Reef Dashboard startup script
# Usage: ./start-dashboard.sh

DIR=/home/rob/reef-workspace
VENV=$DIR/venv/bin/python
LOG=$DIR/cron/dashboard.log

# Kill existing
pkill -f "reef-workspace/dashboard.py" 2>/dev/null

# Start fresh
cd $DIR
nohup $VENV dashboard.py >> $LOG 2>&1 &
echo "Reef Dashboard started on http://0.0.0.0:8891"
