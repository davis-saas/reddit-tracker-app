#!/bin/bash
# Start Reddit Tracker on Linux, or on a Raspberry Pi with its own screen:  bash start-linux.sh
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Reddit Tracker needs Python 3.9 or newer. Install it with your system's software tool (for example: sudo apt install python3), then run this again."
  exit 1
fi
if ! FOUND=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]); sys.exit(sys.version_info < (3, 9))' 2>/dev/null); then
  echo "This computer's Python is too old for Reddit Tracker${FOUND:+ (it has Python $FOUND; Reddit Tracker needs 3.9 or newer)}, and the usual updates won't bring a newer one. Upgrade to a newer version of your Linux (on a Raspberry Pi: put the newest Raspberry Pi OS on the memory card with Raspberry Pi Imager, from raspberrypi.com/software; that erases the card), then run this again."
  exit 1
fi

exec python3 reddit_tracker.py "$@"
