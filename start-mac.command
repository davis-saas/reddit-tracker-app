#!/bin/bash
# Double-click this file to start Reddit Tracker on a Mac.
cd "$(dirname "$0")" || exit 1

if ! FOUND=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]); sys.exit(sys.version_info < (3, 9))' 2>/dev/null); then
  if [ -n "$FOUND" ]; then
    echo "This Mac's Python is too old for Reddit Tracker (it has Python $FOUND; Reddit Tracker needs 3.9 or newer). Download the newest Python free from python.org/downloads, install it, then double-click this file again."
  else
    echo "Reddit Tracker needs Python 3. Click Install in the box macOS shows, or get Python free from python.org/downloads, then double-click this file again."
  fi
  read -r -p "Press Return to close this window. " _
  exit 1
fi

python3 reddit_tracker.py "$@"
status=$?
if [ "$status" -ne 0 ]; then
  echo
  read -r -p "Reddit Tracker stopped because of the problem above. Press Return to close this window. " _
fi
exit "$status"
