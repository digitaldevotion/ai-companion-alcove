#!/bin/sh
echo "hostname:"
/usr/libexec/PlistBuddy -c "Print :System:Network:HostNames:LocalHostName" /Library/Preferences/SystemConfiguration/preferences.plist
killall -9 Python python python3
nohup python3 -u alcove.py 2>&1 | tee /tmp/alcove.out &
