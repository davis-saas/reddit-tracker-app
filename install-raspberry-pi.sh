#!/bin/bash
# Keeps Reddit Tracker running on a Raspberry Pi (it starts by itself after a restart), so every
# computer and phone on your network can use it.
#   Install or upgrade:  bash install-raspberry-pi.sh
#   Stop and remove:     bash install-raspberry-pi.sh --uninstall   (your clients and numbers stay)
set -u
SERVICE=reddit-tracker
UNIT="/etc/systemd/system/$SERVICE.service"
PORT=8765
PY=/usr/bin/python3
OTHER_COPY="Reddit Tracker is already open on this Raspberry Pi, but only for the Raspberry Pi itself. Close that Reddit Tracker window, then run this again."

fail() { echo "$*"; exit 1; }

[ "$(id -u)" -ne 0 ] || fail "Run it without sudo; it will ask for your password once:  bash install-raspberry-pi.sh"
command -v systemctl >/dev/null 2>&1 || fail "This installer is for Raspberry Pi OS. On other computers use:  bash start-linux.sh"
APP=$(cd "$(dirname "$0")" && pwd)
RUN_USER=$(id -un)

if [ "${1:-}" = "--uninstall" ]; then
  echo "Type your Raspberry Pi password if asked (nothing shows while you type)."
  sudo systemctl disable --now "$SERVICE" >/dev/null 2>&1
  { sudo rm -f "$UNIT" && sudo systemctl daemon-reload; } || fail "Couldn't remove Reddit Tracker's service."
  echo "Reddit Tracker no longer runs by itself. Your clients and numbers are still saved on this Raspberry Pi."
  exit 0
fi

if ! FOUND=$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3]); sys.exit(sys.version_info < (3, 9))' 2>/dev/null); then
  fail "This Raspberry Pi's system is too old for Reddit Tracker${FOUND:+ (it has Python $FOUND)}. Reddit Tracker needs Python 3.9 or newer, which comes with Raspberry Pi OS Bullseye and later; updating the old system won't bring it. Put the newest Raspberry Pi OS on the memory card with Raspberry Pi Imager (raspberrypi.com/software), then run this again. That erases the card, so copy anything you need off it first."
fi
[ -f "$APP/reddit_tracker.py" ] || fail "reddit_tracker.py isn't next to this file. Unzip the whole folder, then run this again."
case "$APP" in
  *[\"\\]*) fail "Move this folder somewhere without quotes or backslashes in its name (your home folder is fine), then run this again." ;;
esac
UNIT_APP=${APP//%/%%}            # systemd treats % and $ specially
EXEC_APP=${UNIT_APP//\$/\$\$}

echo "Type your Raspberry Pi password if asked (nothing shows while you type)."
# Exit code 3 means another Reddit Tracker window holds the port: restarting can't help, so systemd doesn't.
# Any other stop (even "already running" from a window opened by hand) is restarted, so it takes over later.
sudo tee "$UNIT" >/dev/null <<EOF || fail "Couldn't set up Reddit Tracker's service, so nothing was started."
[Unit]
Description=Reddit Tracker
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$UNIT_APP
ExecStart=$PY "$EXEC_APP/reddit_tracker.py" --network --no-browser --port $PORT
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
RestartPreventExitStatus=3

[Install]
WantedBy=multi-user.target
EOF
{ sudo systemctl daemon-reload && sudo systemctl enable "$SERVICE" >/dev/null 2>&1 && sudo systemctl restart "$SERVICE"; } \
  || fail "Couldn't start Reddit Tracker's service. To see why, run:  journalctl -u $SERVICE -n 30"

who_answers() {  # "yes": Reddit Tracker answers for other devices; "no": only for this Raspberry Pi; nothing: no answer
  "$PY" - "$PORT" 2>/dev/null <<'PING'
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:%s/api/ping" % sys.argv[1], timeout=2) as r:
    answer = json.loads(r.read().decode("utf-8"))
if answer.get("app") == "reddit-tracker":
    print("no" if answer.get("network") is False else "yes")
PING
}

echo "Starting Reddit Tracker..."
ANSWER=
for _ in {1..20}; do
  ANSWER=$(who_answers)
  [ "$ANSWER" = no ] && fail "$OTHER_COPY"
  [ "$ANSWER" = yes ] && break
  [ "$(systemctl show -p ActiveState --value "$SERVICE" 2>/dev/null)" = failed ] && break
  sleep 1
done
if [ "$ANSWER" != yes ]; then
  [ "$(systemctl show -p ExecMainStatus --value "$SERVICE" 2>/dev/null)" = 3 ] && fail "$OTHER_COPY"
  fail "Reddit Tracker didn't start. To see why, run:  journalctl -u $SERVICE -n 30"
fi
LINK=$(env -u RT_DATA_DIR -u XDG_DATA_HOME "$PY" "$APP/reddit_tracker.py" --show-link 2>&1)
case "$LINK" in
  *"?key="*) ;;
  *) fail "Reddit Tracker didn't make its link. If a Reddit Tracker window is open on this Raspberry Pi, close it and run this again. Otherwise, to see why, run:  journalctl -u $SERVICE -n 30" ;;
esac
echo
echo "$LINK"
echo
echo "Open this link on your computer or phone and bookmark it. Don't move or delete this folder."
