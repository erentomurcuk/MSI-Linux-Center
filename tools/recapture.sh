#!/bin/bash
set -e
rm -f /var/lib/msi-control/profiles/factory.json
python3 -c '
import sys; sys.path.insert(0, "/usr/local/lib/msi-control")
import msi_control as core
print("capture:", core.call({"cmd": "capture-factory"}))
print("show:   ", core.call({"cmd": "profile", "action": "show", "name": "factory"}))
'
