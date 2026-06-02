#!/bin/sh
cd "$(dirname "$0")"
echo "hostname:"
/usr/libexec/PlistBuddy -c "Print :System:Network:HostNames:LocalHostName" /Library/Preferences/SystemConfiguration/preferences.plist
killall -9 Python
python3 -u alcove.py 2>&1 | tee /tmp/alcove.out
