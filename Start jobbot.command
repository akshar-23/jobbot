#!/bin/bash
# Double-click to run (macOS). The first time, right-click → Open if macOS asks.
cd "$(dirname "$0")"
PY=python3
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "jobbot needs Python 3.11 or newer: https://www.python.org/downloads/ (or: brew install python)"
  read -n 1 -s -r -p "Press any key to close."; exit 1
fi
echo "Starting jobbot. Keep this window open while you use it; close it to stop."
$PY -m jobbot serve
