#!/bin/bash
# For the person who publishes Reddit Tracker: builds dist/reddit-tracker-<version>.zip with the app
# folder and google_app.json inside. It never uploads or pushes anything; publish the zip by hand.
set -euo pipefail
cd "$(dirname "$0")"
NAME=reddit-tracker-app
FILES=(reddit_tracker.py README.md LICENSE google_app.json start-mac.command start-windows.bat start-linux.sh
       install-raspberry-pi.sh)
SCRIPTS=(start-mac.command start-linux.sh install-raspberry-pi.sh)

fail() { echo "$*" >&2; exit 1; }

[ -f google_app.json ] || fail "google_app.json is missing. Make it in Google Cloud (README: 'For the person who publishes this app') and save it next to reddit_tracker.py."
python3 - google_app.json <<'PY' || fail "google_app.json doesn't look like the file Google gives you for a Desktop app."
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    app = json.load(f)
app = app.get("installed", app)
sys.exit(0 if app.get("client_id") and app.get("client_secret") else 1)
PY
for f in "${FILES[@]}" rt/__init__.py rt/page.html rt/fonts/montserrat.woff2 rt/fonts/opensans.woff2; do
  [ -f "$f" ] || fail "$f is missing, so no release was made."
done
if grep -qv $'\r$' start-windows.bat; then fail "start-windows.bat needs Windows line endings (CRLF) on every line."; fi
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' rt/__init__.py)
[ -n "$VERSION" ] || fail "Couldn't read VERSION from rt/__init__.py."
command -v zip >/dev/null 2>&1 && command -v zipinfo >/dev/null 2>&1 || fail "This needs the zip and zipinfo tools (macOS and most Linux systems have them)."

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME/rt/fonts"
cp "${FILES[@]}" "$STAGE/$NAME/"
cp rt/*.py rt/page.html "$STAGE/$NAME/rt/"
cp rt/fonts/*.woff2 rt/fonts/OFL-*.txt "$STAGE/$NAME/rt/fonts/"
chmod -R u=rwX,go=rX "$STAGE/$NAME"
for f in "${SCRIPTS[@]}"; do chmod 755 "$STAGE/$NAME/$f"; done

mkdir -p dist
OUT="$PWD/dist/reddit-tracker-$VERSION.zip"
rm -f "$OUT"
(cd "$STAGE" && zip -rqX "$OUT" "$NAME")
for f in "${SCRIPTS[@]}"; do  # double-click and `./` need the "can run" bit inside the zip
  [[ $(zipinfo "$OUT" "$NAME/$f") == -rwx* ]] || fail "$f lost its 'can run' setting in the zip, so no release was made."
done
echo "Made dist/reddit-tracker-$VERSION.zip ($(du -k "$OUT" | cut -f1) KB). Check it, then publish it by hand."
