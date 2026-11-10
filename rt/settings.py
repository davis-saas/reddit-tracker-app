"""The app's own settings file (settings.json): clients, the optional All clients Sheet and options.

Reddit Tracker is the only writer. Every change re-reads the file, checks the page's `rev` (so an
older page never overwrites a newer list), keeps a numbered backup and swaps the new file in
atomically, so a power cut can never leave it half-written.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Callable

from rt import csvfiles, paths

ALL = "all"  # set_sheet / clear_sheet target for the All clients Sheet
RESERVED_NAMES = ("All clients", "All accounts")
NAME_MAX = 60
KEEP_BACKUPS = 20
LAST_SAVED = "settings.last-saved.json"  # outside the settings.<seq>.json pattern, so never pruned
SEQ_NAME = re.compile(r"settings\.(\d+)\.json")
BROKEN_NAME = re.compile(r"settings\.broken\.(\d+)\.json")
STATUS = {"stale": 409, "broken_file": 409, "no_such_client": 404, "bad_request": 400, "write_failed": 500}
JSON_REASONS = (
    ("Expecting ',' delimiter", "a comma is missing"),
    ("Expecting property name enclosed in double quotes", "a name isn't in double quotes, or there's an extra comma"),
    ("Expecting value", "a value is missing, or there's an extra comma"),
    ("Unterminated string", "a closing quote is missing"),
    ("Invalid control character", "a line break inside quotes"),
    ("Extra data", "there's text after the closing }"),
)


class SettingsError(Exception):
    """A change that can't be made: code for the page, message in plain words, HTTP status for the server.
    For broken_file, `backup` is the copy that restore_last_saved() would put back (or None)."""

    def __init__(self, code: str, message: str, field: str | None = None, status: int | None = None,
                 backup: str | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field
        self.status = status or STATUS.get(code, 422)
        self.backup = backup


def empty() -> dict:
    return {"version": 1, "clients": [], "all_sheet": None, "options": {"access_key": None}}


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write via a temp file in the same folder and swap it in: the file is always the old or the new version."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        paths.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:  # make the swap itself survive a power cut (not possible on Windows)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        pass


def _layout_problem(data) -> str | None:
    """What's wrong with a parsed settings file, in plain words, or None."""
    if not isinstance(data, dict) or not isinstance(data.get("clients", []), list):
        return "it isn't laid out like Reddit Tracker's settings"
    ids = set()
    for i, c in enumerate(data.get("clients", []), 1):
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) and c[k].strip()
                                              for k in ("id", "name", "reddit_username")):
            return f"client {i} in the list has no id, name or Reddit username"
        if c["id"] in ids:
            return f"client {i} ({c['name']}) has the same id as another client"
        ids.add(c["id"])
        if any(c.get(k) is not None and not isinstance(c[k], str) for k in ("sheet_id", "sheet_title")):
            return f"client {i} ({c['name']}): its Sheet needs double quotes around it"
        if c.get("paused") is not None and not isinstance(c["paused"], bool):
            return (f"client {i} ({c['name']}): \"paused\" should be true or false"
                    + (", without quotes" if isinstance(c["paused"], str) else ""))
    sheet = data.get("all_sheet")
    if sheet is not None and not (isinstance(sheet, dict) and isinstance(sheet.get("id"), str) and sheet["id"]):
        return "the All clients Sheet isn't set up right"
    if not isinstance(data.get("options", {}), dict):
        return "its options aren't set up right"
    return None


def _parse(raw: bytes) -> tuple[dict | None, str | None]:
    """(settings with defaults filled in, None) or (None, what's wrong in plain words)."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))  # Notepad and PowerShell may add a BOM
    except UnicodeDecodeError:
        return None, "it isn't a text file"
    except json.JSONDecodeError as e:
        reason = next((r for start, r in JSON_REASONS if e.msg.startswith(start)), e.msg)
        return None, f"line {e.lineno}, column {e.colno}: {reason}"
    problem = _layout_problem(data)
    if problem:
        return None, problem
    data = {**empty(), **data}
    data["options"] = {"access_key": None, **data["options"]}
    for c in data["clients"]:
        c.setdefault("sheet_id", None)
        c.setdefault("sheet_title", None)
        c["paused"] = bool(c.get("paused"))
    return data, None


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


def _rev_of(raw: bytes | None) -> str:
    return hashlib.sha1(raw).hexdigest()[:12] if raw is not None else ""


def _default_name(username: str) -> str:
    """The name for a client whose name box was left blank: 'Acme_Jane' -> 'Acme Jane', '___' -> '___'."""
    return csvfiles.default_name(username) or username


def _text(value, field: str) -> None:
    if value is not None and not isinstance(value, str):
        raise SettingsError("bad_request", "That needs to be text.", field)


_NO_REV = object()  # changes made after Google answers: there is no page rev to check


class Settings:
    def __init__(self, path: Path, backups_dir: Path):
        self.path, self.backups_dir = Path(path), Path(backups_dir)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ reading

    def _raw(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _load(self) -> tuple[bytes | None, dict]:
        """(file bytes, settings). A missing file is an empty list; a damaged one raises broken_file."""
        with self._lock:
            try:
                raw = self._raw()
            except OSError:
                raise self._broken("it can't be read")
            if raw is None:
                return None, empty()
            data, problem = _parse(raw)
            if problem:
                raise self._broken(problem)
            return raw, data

    def _good_backup(self) -> Path | None:
        """The newest backup that reads fine: the copy as last saved, else the newest numbered one."""
        numbered = sorted(self._seqs(SEQ_NAME), reverse=True)
        for p in [self.backups_dir / LAST_SAVED] + [self.backups_dir / f"settings.{n}.json" for n in numbered]:
            try:
                if _parse(p.read_bytes())[0] is not None:
                    return p
            except OSError:
                continue
        return None

    def _broken(self, reason: str) -> SettingsError:
        backup = self._good_backup()
        if backup:
            message = (f"Reddit Tracker's settings file is damaged ({reason}), so nothing can be changed right now. "
                       f"Restore the last good copy to carry on (it's kept in {backup}).")
        else:
            message = (f"Reddit Tracker's settings file is damaged ({reason}), and there's no earlier copy to put "
                       f"back. Restoring starts an empty client list and keeps the damaged file in {self.backups_dir}.")
        return SettingsError("broken_file", message, backup=str(backup) if backup else None)

    def data(self) -> dict:
        return copy.deepcopy(self._load()[1])

    def rev(self) -> str:
        """A short fingerprint of the file; the page sends it back with every change."""
        try:
            return _rev_of(self._raw())
        except OSError:
            return ""

    def clients(self, active_only: bool = False) -> list[dict]:
        return [c for c in self.data()["clients"] if not (active_only and c["paused"])]

    def client(self, client_id: str) -> dict:
        return copy.deepcopy(self._find(self._load()[1], client_id))

    def option(self, key: str, default=None):
        value = self._load()[1]["options"].get(key)
        return default if value is None else value

    # ------------------------------------------------------------------ writing

    def _seqs(self, pattern: re.Pattern) -> list[int]:
        try:
            names = [p.name for p in self.backups_dir.iterdir()]
        except OSError:
            return []
        return [int(m.group(1)) for m in map(pattern.fullmatch, names) if m]

    def _backups_folder(self) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.backups_dir, 0o700)
        except OSError:
            pass
        return self.backups_dir

    def _write(self, data: dict, raw_before: bytes | None) -> None:
        """Back up the current file (numbered, newest 20 kept), save, then refresh the last-saved copy."""
        text = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            folder = self._backups_folder()
            if raw_before is not None:
                seqs = self._seqs(SEQ_NAME)
                seq = max(seqs, default=0) + 1  # numbers, not times: a Raspberry Pi's clock can jump back
                atomic_write(folder / f"settings.{seq}.json", raw_before)
                for old in sorted(seqs + [seq])[:-KEEP_BACKUPS]:
                    (folder / f"settings.{old}.json").unlink(missing_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, text)
        except OSError:
            raise SettingsError("write_failed", "Couldn't save the change (the disk may be full or the folder "
                                                "locked). Nothing was changed.")
        try:
            atomic_write(self.backups_dir / LAST_SAVED, text)
        except OSError:
            pass  # the change itself is saved; the last-saved copy is only a spare

    def _change(self, change: Callable[[dict], object], rev=_NO_REV):
        """Re-read the file, refuse a stale page, apply change(data) and save if anything changed."""
        with self._lock:
            raw, data = self._load()
            if rev is not _NO_REV and rev != _rev_of(raw):
                raise SettingsError("stale", "Your settings changed in another window, so nothing was saved. The "
                                             "page now shows the latest version: check it, then try again.")
            before = copy.deepcopy(data)
            result = change(data)
            if data != before:
                self._write(data, raw)
            return copy.deepcopy(result)

    @staticmethod
    def _find(data: dict, client_id: str) -> dict:
        found = next((c for c in data["clients"] if c["id"] == client_id), None)
        if found is None:
            raise SettingsError("no_such_client", "This client isn't in your list any more.")
        return found

    @staticmethod
    def _username(data: dict, text: str | None, canonical: Callable[[str], str] | None,
                  editing: dict | None = None) -> str:
        """A checked username from what was typed, spelled the way the database already has it."""
        username, err = csvfiles.username_from(text)
        if err:
            raise SettingsError("bad_username", err, "username")
        if canonical:
            username = canonical(username)
        if editing and _same(username, editing["reddit_username"]):
            return username  # unchanged: a mix-up made earlier never blocks other edits
        owner = next((c for c in data["clients"] if c is not editing and _same(c["reddit_username"], username)), None)
        if owner and owner["paused"]:
            raise SettingsError("username_taken", f"u/{username} belongs to {owner['name']}, which is paused. Resume it "
                                                  "instead of adding it again.", "username")
        if owner:
            raise SettingsError("username_taken", f"u/{username} is already set up as {owner['name']}.", "username")
        return username

    @staticmethod
    def _name(data: dict, name: str, editing: dict | None = None) -> str:
        if not name.strip():
            raise SettingsError("name_missing", "Type a name for this client.", "name")
        if len(name) > NAME_MAX:
            raise SettingsError("name_too_long", f"Use a shorter name ({NAME_MAX} characters at most).", "name")
        if any(_same(name, r) for r in RESERVED_NAMES):
            raise SettingsError("name_reserved", f"Reddit Tracker uses “{name}” for the combined Sheet. Pick another "
                                                 "name for this client.", "name")
        other = next((c for c in data["clients"] if c is not editing and _same(c["name"], name)), None)
        if other:
            raise SettingsError("name_taken", f"You already have a client called {other['name']}.", "name")
        return name

    def add_client(self, rev: str | None, username_text: str, name: str | None = None,
                   canonical: Callable[[str], str] | None = None) -> dict:
        """Add a client; the name defaults to the username with _ and - as spaces."""
        _text(username_text, "username")
        _text(name, "name")

        def change(data):
            username = self._username(data, username_text, canonical)
            client_name = self._name(data, " ".join((name or "").split()) or _default_name(username))
            taken = {c["id"] for c in data["clients"]}
            client_id = "c-" + secrets.token_hex(4)
            while client_id in taken:
                client_id = "c-" + secrets.token_hex(4)
            client = {"id": client_id, "name": client_name, "reddit_username": username,
                      "sheet_id": None, "sheet_title": None, "paused": False}
            data["clients"].append(client)
            return client
        return self._change(change, rev)

    def update_client(self, rev: str | None, client_id: str, name: str | None = None,
                      username_text: str | None = None, items_count: int | None = None,
                      canonical: Callable[[str], str] | None = None) -> dict:
        """Rename a client or change its username. items_count = rows saved under the current username;
        a username with saved numbers can't be changed unless another client shares it."""
        _text(username_text, "username")
        _text(name, "name")

        def change(data):
            c = self._find(data, client_id)
            username = c["reddit_username"]
            if username_text is not None:
                username = self._username(data, username_text, canonical, editing=c)
                if not _same(username, c["reddit_username"]):
                    if items_count is None:
                        raise SettingsError("bad_request", "Reddit Tracker couldn't check this client's saved "
                                                           "numbers, so nothing was changed.")
                    shared = any(o is not c and _same(o["reddit_username"], c["reddit_username"])
                                 for o in data["clients"])
                    if items_count > 0 and not shared:
                        raise SettingsError("username_locked", f"u/{c['reddit_username']} already has saved numbers, "
                                                               "so it can't be changed. A different Reddit account? "
                                                               "Add it as a new client.", "username")
            if name is not None:
                new_name = " ".join(name.split()) or _default_name(username)
                if new_name != c["name"]:
                    c["name"] = self._name(data, new_name, editing=c)
            c["reddit_username"] = username
            return c
        return self._change(change, rev)

    def set_paused(self, rev: str | None, client_id: str, paused: bool) -> dict:
        if not isinstance(paused, bool):
            raise SettingsError("bad_request", "Pause needs to be yes or no.")

        def change(data):
            c = self._find(data, client_id)
            c["paused"] = paused
            return c
        return self._change(change, rev)

    def remove_client(self, rev: str | None, client_id: str) -> None:
        """Take a client off the list. Its numbers stay saved in the database."""
        def change(data):
            data["clients"].remove(self._find(data, client_id))
        self._change(change, rev)

    def set_sheet(self, target: str, sheet_id: str, title: str | None) -> None:
        """Give a client (or ALL, the All clients Sheet) its Sheet. One Sheet never serves two targets."""
        if not isinstance(sheet_id, str) or not sheet_id.strip():
            raise SettingsError("bad_request", "That isn't a Google Sheet.")

        def change(data):
            all_sheet = data["all_sheet"] or {}
            if target == ALL:
                user = next((c for c in data["clients"] if c["sheet_id"] == sheet_id), None)
                if user:
                    raise SettingsError("sheet_in_use", f"{user['name']} already uses this Sheet. The All clients Sheet "
                                                        "needs a Sheet of its own: choose another one, or make a new one.")
                data["all_sheet"] = {"id": sheet_id, "title": title}
                return
            c = self._find(data, target)
            user = next((o for o in data["clients"] if o is not c and o["sheet_id"] == sheet_id), None)
            if user:
                raise SettingsError("sheet_in_use", f"{user['name']} already uses this Sheet. Choose a different Sheet "
                                                    "for this client, or make a new one.")
            if all_sheet.get("id") == sheet_id:
                raise SettingsError("sheet_in_use", "This Sheet is already your All clients Sheet. Choose a different "
                                                    "Sheet for this client, or make a new one.")
            c["sheet_id"], c["sheet_title"] = sheet_id, title
        self._change(change)

    def clear_sheet(self, target: str) -> None:
        """Stop writing to a client's Sheet (or the All clients Sheet). The Sheet itself is left as it is."""
        def change(data):
            if target == ALL:
                data["all_sheet"] = None
            else:
                c = self._find(data, target)
                c["sheet_id"], c["sheet_title"] = None, None
        self._change(change)

    def set_option(self, key: str, value) -> None:
        def change(data):
            data["options"][key] = value
        self._change(change)

    def new_access_key(self) -> str:
        """Make (or replace) the key in the link other devices use. 12 url-safe characters."""
        key = secrets.token_urlsafe(9)
        self.set_option("access_key", key)
        return key

    def restore_last_saved(self) -> dict:
        """Put the last good copy back over a damaged settings file, keeping the damaged one in backups.
        Does nothing when the file reads fine. {"restored": backup path or None, "kept": path or None}."""
        with self._lock:
            try:
                self._load()
                return {"restored": None, "kept": None}
            except SettingsError as e:
                if e.code != "broken_file":
                    raise
            backup = self._good_backup()
            try:
                folder = self._backups_folder()
                kept = folder / f"settings.broken.{max(self._seqs(BROKEN_NAME), default=0) + 1}.json"
                atomic_write(kept, self._raw() or b"")
                if backup:
                    good = backup.read_bytes()
                    atomic_write(self.path, good)
                    if backup.name != LAST_SAVED:
                        atomic_write(folder / LAST_SAVED, good)
                else:
                    self.path.unlink(missing_ok=True)
            except OSError:
                raise SettingsError("write_failed", "Couldn't put the last good copy back (the disk may be full or "
                                                    "the folder locked). Nothing was changed.")
            return {"restored": str(backup) if backup else None, "kept": str(kept)}
