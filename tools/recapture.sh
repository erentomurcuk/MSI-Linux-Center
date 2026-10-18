#!/bin/bash
set -e
rm -f /var/lib/gs66-center/profiles/factory.json
python3 -c '
import sys; sys.path.insert(0, "/usr/local/lib/gs66-center")
import gs66_common as core
print("capture:", core.call({"cmd": "capture-factory"}))
print("show:   ", core.call({"cmd": "profile", "action": "show", "name": "factory"}))
'
