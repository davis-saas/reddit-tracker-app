"""Where Reddit Tracker keeps its files, and the one timestamp format it uses."""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent  # the folder with reddit_tracker.py


def app_dir() -> Path:
    return APP_DIR


def default_data_dir() -> Path:
    """The per-user folder for settings, the Google sign-in and the database."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Reddit Tracker"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Reddit Tracker"
    xdg = os.environ.get("XDG_DATA_HOME", "")
    return (Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local" / "share") / "reddit-tracker"


def data_dir(override: str | Path | None = None) -> Path:
    """--data DIR, else $RT_DATA_DIR, else the per-user folder. Made on demand, and kept private to this user
    (only this computer account may open it), even when it already existed."""
    chosen = override or os.environ.get("RT_DATA_DIR")
    path = Path(chosen).expanduser() if chosen else default_data_dir()
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path.resolve()


def find_google_app_file(data: Path | None = None) -> Path | None:
    """The Google connection file: $RT_GOOGLE_APP_FILE, next to reddit_tracker.py, or in the data folder."""
    candidates = [os.environ.get("RT_GOOGLE_APP_FILE"), APP_DIR / "google_app.json",
                  (data if data is not None else data_dir()) / "google_app.json"]
    return next((Path(c) for c in candidates if c and Path(c).is_file()), None)


def replace(src: str | Path, dst: str | Path) -> None:
    """os.replace, retried briefly on Windows, where a reader or the virus scanner holding dst open
    makes it fail with PermissionError for a moment."""
    for attempt in range(20):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def now_utc() -> str:
    """Now as UTC 'YYYY-MM-DDTHH:MM:SSZ' (every timestamp in the API and status files)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
