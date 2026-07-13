#!/bin/sh
cd "$(dirname "$0")"
echo "hostname:"
/usr/libexec/PlistBuddy -c "Print :System:Network:HostNames:LocalHostName" /Library/Preferences/SystemConfiguration/preferences.plist
PIDFILE="alcove.pid"
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null
        echo "Killed previous Alcove process (PID $OLD_PID)"
        sleep 1
    else
        echo "Stale PID file found (PID $OLD_PID not running) — cleaning up"
    fi
    rm -f "$PIDFILE"
else
    pkill -f "modules.main" 2>/dev/null; pkill -f "modules/alcove.py" 2>/dev/null
    sleep 1
fi
nohup python3 -u modules/alcove.py >> /tmp/alcove_placeholder.log 2>&1 &
PID=$!
LOGFILE="/tmp/alcove_${PID}.log"
mv /tmp/alcove_placeholder.log "$LOGFILE"
echo "$PID" > "$PIDFILE"
echo "Alcove started (PID $PID, log: $LOGFILE)"
tail -f "$LOGFILE" &

