"""The local web server: the page, its API, Google's sign-in answer, and who may use it.

Only this computer may use it, unless it was started with --network: then other devices need the
link with the key (it sets a cookie). Every change the Sheets must show is counted (job.bump()
while holding db_lock) and then the Sheets job is started.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import functools
import hmac
import html
import ipaddress
import itertools
import json
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from . import VERSION, engine, paths, store
from .csvfiles import KINDS
from .google import Google, GoogleError
from .settings import ALL, Settings, SettingsError
from .sheets import LASTING, META_FIELDS, TABS, SheetsJob, _tabs

DEFAULT_PORT = 8765
MAX_BODY = 40_000_000
MAX_FILES = 200
LOG_BYTES = 1_000_000
COOKIE = "rt_key"
PAGE = Path(__file__).with_name("page.html")
FONTS = {f"/fonts/{name}.woff2": Path(__file__).with_name("fonts") / f"{name}.woff2" for name in ("montserrat", "opensans")}
# The Data Studio report a client's "Connect a Data Studio report" copies, unless Settings names another
REPORT_TEMPLATE = "76a8c6b4-f966-4c6c-a50a-49d9aa5bdc03"
REPORT_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
REPORT_LINK = re.compile(r"/reporting/([A-Za-z0-9_-]{8,100})")
SECURITY = (("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                        "img-src data:; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
                                        "frame-ancestors 'none'"),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store"))
QUIET = ("/api/status", "/api/ping")  # polled all day: only their failures go in the log
SECRET = re.compile(r"((?:code|state|key|token)=)[^&\s\"']+|ya29\.[\w.-]+|1//[\w.-]+")
HIDDEN = r"\1…"
TOO_BIG = "These files are too big to send at once. Drop fewer files at a time."
SHARED = "Another client uses this Sheet too. Choose a different Sheet for one of them."
FIX = {**dict.fromkeys(("not_found", "trashed", "not_a_sheet", "excel_file", "read_only"), "choose"),
       **dict.fromkeys(("rejected", "signed_out"), "signin"), "shrink": "replace",
       **dict.fromkeys(("too_big", "drive_full"), "new"), "app_refused": None, "too_much": None,
       **dict.fromkeys(("offline", "quota", "google_down", "protected", "cert", "other", "storage_full"), "retry")}
GOOGLE_STATUS = {**dict.fromkeys(("bad_paste", "bad_state", "code_expired", "denied", "exchange_failed"), 400),
                 **dict.fromkeys(("wrong_account", "signed_out", "rejected", "no_app_file", "bad_app_file",
                                  "app_refused"), 409)}
CANT_SAVE = engine.UNWRITABLE + "."
DAMAGED = ("the database file is damaged or missing (a copy from before each import is in the backups folder in "
           "Reddit Tracker's data folder)")
TEST_BANNER = ('<div role="note" style="background:#fde68a;color:#3b2f00;padding:6px 16px;text-align:center;'
               'font:14px system-ui,sans-serif">Test mode: Google is simulated on this computer. Nothing goes to the '
               'real Google.</div>').encode("utf-8")


class Refused(Exception):
    """A request the server turns down, with a plain message for the page."""

    def __init__(self, code: str, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.field = code, message, status, field


def say(text: str) -> None:
    """Print to the console window; never fail on characters it can't show, or when there is none."""
    try:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(text.encode(enc, "replace").decode(enc), flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def peer_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    addr = str(handler.client_address[0])
    try:
        return ipaddress.ip_address(addr[7:] if addr.startswith("::ffff:") else addr).is_loopback
    except ValueError:
        return False


def lan_hosts() -> list[str]:
    """This computer's name on the local network (name.local) and its network address."""
    name = socket.gethostname().split(".")[0]
    hosts = [f"{name}.local"] if name and name != "localhost" else []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # picks the address the computer uses on its network; sends nothing
            ip = s.getsockname()[0]
        if not ip.startswith(("127.", "0.", "169.254.")):
            hosts.append(ip)
    except OSError:
        pass
    return hosts


def key_in_backup(path: str | None) -> str | None:
    """The access key in a settings backup (used while settings.json is damaged)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))["options"]["access_key"] if path else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


@functools.lru_cache(maxsize=None)
def tab_headers() -> tuple:
    """((tab, header), ...) for a new Sheet, taken from the tables themselves."""
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(store.SCHEMA)
        tables = store.build_tables(con)
        return tuple((name, next(iter(tables[name]))) for name in TABS)
    finally:
        con.close()


def report_template_id(value) -> str | None:
    """The template's id from the option: None (never set) = ours, "" = off."""
    if value is None:
        return REPORT_TEMPLATE
    return value or None


def report_id_from(text) -> str:
    """A pasted Data Studio / Looker Studio report link (or just its id): the id, else Refused."""
    text = str(text or "").strip()
    found = REPORT_LINK.search(text)
    rid = found.group(1) if found else text
    if not REPORT_ID.match(rid):
        raise Refused("bad_report_link", "That doesn't look like a link to a Data Studio report. Open the report, "
                                         "copy the address from the browser's address bar and paste it here.",
                      field="link")
    return rid


def report_link(template: str, sheet_id: str, content_tab: int, client_name: str) -> str:
    """Data Studio's Linking API: a copy of the template, its data source pointed at this Sheet's content tab.
    The copy is made (and owned) by whoever opens it, with their own Google access to the Sheet."""
    query = urllib.parse.urlencode([
        ("c.reportId", template), ("r.reportName", f"{client_name} | Reddit organic report"),
        ("ds.*.connector", "googleSheets"), ("ds.*.spreadsheetId", sheet_id),
        ("ds.*.worksheetId", str(content_tab)), ("ds.*.hasHeader", "true"), ("ds.*.refreshFields", "false")])
    return "https://datastudio.google.com/reporting/create?" + query


def sheet_card(sheet_id: str | None, title: str | None, saved: dict | None, live: dict | None, running: bool,
               shared: bool, signed_in: bool, pending: bool, configured: bool = True) -> dict:
    """What a client's (or the All clients) Sheet shows: state none|ok|waiting|writing|problem and its fix.
    saved = the job's last result for this Sheet; live = this target in the current or last pass.
    configured False (Google isn't set up, or no longer accepts this copy): signing in can't help."""
    if not sheet_id:
        return {"id": None, "title": None, "url": None, "state": "none", "updated_at": None, "message": None,
                "code": None, "fix": "choose"}
    saved = saved if saved and "ok" in saved else None
    card = {"id": sheet_id, "title": (saved or {}).get("title") or title,
            "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
            "updated_at": (saved or {}).get("ok_at"), "message": None, "code": None, "fix": None}
    if shared:
        return {**card, "state": "problem", "code": "shared", "message": SHARED}
    live = live if live and live.get("sheet_id") == sheet_id else None
    if live and running and live["state"] in ("waiting", "writing"):
        return {**card, "state": "writing"}
    if live and live["state"] in ("done", "failed"):
        saved = {"ok": live["state"] == "done", "code": live["code"], "message": live["message"]}
    if live and live["state"] == "skipped":  # the pass stopped before it (offline, Google said wait)
        card.update(state="waiting", message=live.get("message") or "Not updated yet: waiting for Google.")
    elif saved is None:
        card.update(state="waiting", message="Not filled yet")
    elif saved["ok"]:
        card["state"] = "ok"
    else:
        code = saved.get("code") or "other"
        card.update(state="problem", code=code, message=saved.get("message"), fix=FIX.get(code))
        if code in ("rejected", "signed_out") and signed_in:
            card.update(state="waiting", code=None, fix=None, message="Will update now that you're signed in.")
    if not configured:
        card["fix"] = None if card["fix"] == "signin" else card["fix"]
    elif not signed_in and pending and not (card["state"] == "problem" and card["code"] in LASTING):
        card.update(state="waiting", code=None, fix="signin", message="Waiting for you to sign in with Google.")
    return card


def checked_files(body: dict) -> list[dict]:
    """The dropped files from a preview or import request, or a 400."""
    files = body.get("files")

    def good(f) -> bool:
        return (isinstance(f, dict) and all(isinstance(f.get(k), str) for k in ("id", "name", "text"))
                and (f.get("modified") is None or type(f["modified"]) is int)
                and all(f.get(k) is None or isinstance(f[k], str) for k in ("client", "expect")))
    if not (isinstance(files, list) and 1 <= len(files) <= MAX_FILES and all(map(good, files))
            and len({f["id"] for f in files}) == len(files)):
        raise Refused("bad_request", f"The page sent the files in a way Reddit Tracker can't read. Reload the page "
                                     f"and drop them again ({MAX_FILES} files at most).")
    return [{k: f.get(k) for k in ("id", "name", "text", "modified", "client", "expect")} for f in files]


def sentence(e: Exception) -> str:
    """A problem's plain message as a whole sentence."""
    text = getattr(e, "message", None) or plain(e)
    return text if getattr(e, "message", None) else text[:1].upper() + text[1:] + "."


def plain(e: Exception) -> str:
    """An unexpected error in plain words (no full stop: it goes inside a sentence)."""
    if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
        return "Reddit Tracker's database is busy. Try again in a moment"
    if engine.unwritable(e):
        return CANT_SAVE.rstrip(".")
    if isinstance(e, sqlite3.DatabaseError) and any(w in str(e) for w in ("malformed", "not a database", "missing")):
        return DAMAGED
    return "something unexpected went wrong (the details are in log.txt in the data folder)"


class App:
    """Everything the server works with: settings, the database, Google and the Sheets job."""

    def __init__(self, data: Path, network: bool = False, google: Google | None = None, timer=None,
                 echo: bool = True):
        self.data, self.network, self.echo = Path(data), network, echo
        self.port = DEFAULT_PORT
        self.db_path = self.data / "reddit.db"
        self.db_lock = threading.RLock()
        self._log_lock = threading.Lock()
        self.settings = Settings(self.data / "settings.json", self.data / "backups")
        store.connect(self.db_path).close()  # tables exist before the page asks; ForeignDatabase stops here
        # Held open (after one read) until close(): SQLite deletes reddit.db-wal and -shm when its last
        # connection closes, and would make them again at every status poll (wear on a Pi's SD card).
        self._held = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._held.execute("SELECT 1 FROM meta LIMIT 1").fetchall()
        self.google = google or Google(self.data, paths.find_google_app_file(self.data), log=self.log)
        self.job = SheetsJob(self.data / "sheets_status.json", self.db_lock, self.build_targets, self.google,
                             self.log, **({"timer": timer} if timer else {}))
        self._key = None
        if network:
            self.access_key()

    # ---------------------------------------------------------------- basics

    def close(self, stopping: bool = True) -> None:
        """stopping: it ran and is being stopped on purpose (not a start that failed), so a Sheets update
        cut off now resumes at once at the next start."""
        if stopping:
            self.job.close()
        self._held.close()

    def log(self, text: str, echo: bool = True) -> None:
        """A line in log.txt (rotated at 1 MB); also shown in the console window unless echo is False.
        Never fails: a log that can't be written (full or read-only disk) must not break a request."""
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {SECRET.sub(HIDDEN, text)}\n"
        with self._log_lock:
            try:
                path = self.data / "log.txt"
                if path.exists() and path.stat().st_size > LOG_BYTES:
                    paths.replace(path, self.data / "log.old.txt")
                with open(path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
            except Exception:
                pass
        if echo and self.echo:
            say(SECRET.sub(HIDDEN, text))

    def access_key(self) -> str | None:
        """The key in the link for other devices (made on the first --network start). While settings.json
        is damaged: the last one known, or the one in its backup."""
        try:
            key = self.settings.option("access_key")
            if key is None and self.network:
                key = self.settings.new_access_key()
        except SettingsError as e:
            key = self._key or key_in_backup(e.backup)
        self._key = key
        return key

    def links(self) -> list[str]:
        key = self.access_key() if self.network else None
        return [f"http://{host}:{self.port}/?key={key}" for host in lan_hosts()] if key else []

    def build_targets(self) -> list[dict]:
        """The Sheets to write. Each one's tables are read only when it's written (read_tables)."""
        data = self.settings.data()
        con = store.open_read(self.db_path)
        try:
            targets = store.sheet_targets(con, data)
        finally:
            con.close()
        for t in targets:
            t["read_tables"] = functools.partial(self.read_tables, t["users"])
        return targets

    @contextlib.contextmanager
    def read_tables(self, users: set, tab: str | None = None):
        """These usernames' tables (or only `tab`), all from one database snapshot: a read transaction kept
        until they're used up (content_history is read from it as it's used). No db_lock is needed: a change
        saved meanwhile is counted and goes in the next pass."""
        con = store.open_read(self.db_path)
        try:
            con.execute("BEGIN")
            yield store.build_tables(con, users, tab)
        finally:
            con.close()

    def update_sheets(self, change=None, reason: str = "", force: bool = False):
        """(change(), how the Sheets job started): the change is made and counted under db_lock.
        force: rewrite every Sheet, even one whose numbers haven't changed."""
        with self.db_lock:
            result = change() if change else None
            self.job.bump()
        return result, self.job.start(reason, force=force)

    def target_sheet(self, target) -> str | None:
        """The Sheet id of a client (or of the All clients Sheet, target "all")."""
        if not isinstance(target, str) or not target:
            raise Refused("bad_request", "Choose which client this Sheet is for.")
        if target == ALL:
            return (self.settings.data()["all_sheet"] or {}).get("id")
        return self.settings.client(target)["sheet_id"]

    def canonical(self, con):
        configured = [c["reddit_username"] for c in self.settings.clients()]
        return lambda name: store.canonical_user(con, name, configured)

    # ---------------------------------------------------------------- status

    def status(self, remote: bool) -> dict:
        g = self.google.status()
        job = self.job.state()
        out = {"app": {"version": VERSION, "network": self.network, "remote": remote, "data_dir": str(self.data),
                       "links": self.links()},
               "rev": self.settings.rev(),
               "google": {**g, "busy": self.google.busy, "last": self.google.last},
               "clients": [], "all_sheet": None,
               "sheets": {k: job[k] for k in ("running", "done", "total", "pending", "finished_at", "next_retry_at")},
               "last_import": self.job.last_import(), "first_run": True, "report": None}
        try:
            data = self.settings.data()
        except SettingsError as e:
            if e.code != "broken_file":
                raise
            out["settings_problem"] = {"message": e.message, "backup": e.backup}
            return out
        live = {t["key"]: t for t in job["targets"]}
        template = report_template_id(data["options"].get("report_template"))
        out["report"] = {"id": template, "is_default": template == REPORT_TEMPLATE,
                         "url": f"https://datastudio.google.com/reporting/{template}" if template else None}
        clients, all_id = data["clients"], (data["all_sheet"] or {}).get("id")
        uses: dict = {}
        for c in clients:
            uses[c["sheet_id"]] = uses.get(c["sheet_id"], 0) + 1

        def card(key: str, sheet_id: str | None, title: str | None, shared: bool = False) -> dict:
            saved = self.job.sheet_status(sheet_id) if sheet_id else None
            return sheet_card(sheet_id, title, saved, live.get(key), job["running"], shared, g["signed_in"],
                              job["pending"], g["configured"])
        con = store.open_read(self.db_path)
        try:
            for c in clients:
                name, users, sid = c["reddit_username"], {c["reddit_username"].lower()}, c["sheet_id"]
                seen = store.client_activity(con, users)
                shared_name = any(o is not c and o["reddit_username"].lower() == name.lower() for o in clients)
                out["clients"].append({
                    "id": c["id"], "name": c["name"], "username": name, "paused": c["paused"],
                    "last": {k: seen[k] for k in KINDS}, "items": seen["items"], "comment_reach": seen["comment_reach"],
                    "username_locked": not shared_name and store.items_for_usernames(con, users) > 0,
                    "sheet": {**card(c["id"], sid, c["sheet_title"], bool(sid) and (uses[sid] > 1 or sid == all_id)),
                              "report": self.report_for(c, template)}})
            if all_id:
                out["all_sheet"] = card(ALL, all_id, data["all_sheet"].get("title"))
            out["first_run"] = not clients or not store.ever_imported(con)
        finally:
            con.close()
        return out

    # ---------------------------------------------------------------- files

    def preview(self, body: dict) -> dict:
        files = checked_files(body)
        clients = self.settings.clients()
        con = store.open_read(self.db_path)
        try:
            return engine.preview(con, clients, files, dt.date.today(), lock=self.db_lock)
        finally:
            con.close()

    def import_files(self, body: dict) -> dict:
        files = checked_files(body)
        with self.db_lock:
            clients = self.settings.clients()
            con = store.connect(self.db_path)
            try:
                plan = engine.run_import(con, clients, files, dt.date.today(), self.data / "backups",
                                         self.data / "uploads", on_saved=self.job.bump)
            finally:
                con.close()
            if plan["imported"]:  # the whole batch is in: a pass that began part-way through must not count
                self.job.bump()
        if plan["imported"]:
            self.job.set_last_import(paths.now_utc(), plan["receipt"])
        plan["sheets"] = self.job.start("new numbers") if plan["imported"] or self.job.pending() else None
        return plan

    # ---------------------------------------------------------------- Google

    def begin(self, body: dict) -> dict:
        purpose, target, file_id = body.get("purpose"), body.get("target"), body.get("file_id")
        if purpose not in ("signin", "pick") or not all(v is None or isinstance(v, str) for v in (target, file_id)):
            raise Refused("bad_request", "The page asked Google for something Reddit Tracker doesn't know. Reload it.")
        if purpose == "pick":
            self.target_sheet(target)
        return {"url": self.google.begin(purpose, target, self.port, file_id or None)["url"]}

    def finish(self, params: dict) -> dict:
        """Complete a sign-in or a Sheet choice from Google's answer (the /oauth address, or a pasted one)."""
        try:
            result = self.google.finish(params)
        except GoogleError:
            raise  # Google's own answer: already published for the page
        except Exception as e:  # e.g. the sign-in can't be saved: the page waits for this answer
            self.google.last = {"ok": False, "message": sentence(e), "at": paths.now_utc(), "purpose": None,
                                "target": None}
            raise
        target = result["target"]
        if result["purpose"] == "signin":
            self.update_sheets(reason="signed in", force=True)
            return {"ok": True, "purpose": "signin", "target": target}
        try:
            if not result["picked"]:
                raise Refused("nothing_picked", "No Sheet was chosen. Click Choose Sheet again and pick one.", 422)
            picked = result["picked"][0]
            check = self.google.check_file(picked)
            if check["code"] != "ok":
                raise Refused(check["code"], check["message"], 422)
            title = check["title"] or "Google Sheet"
            # filled anew even if unchanged (it may be chosen again because it looks wrong); the others aren't
            self.update_sheets(lambda: (self.settings.set_sheet(target, picked, title), self.job.rewrite(picked))[0],
                               "Sheet chosen")
        except Exception as e:  # the page waits for this answer: never leave it spinning
            self.google.last = {"ok": False, "message": sentence(e), "at": paths.now_utc(), "purpose": "pick",
                                "target": target}
            raise
        self.google.last = {"ok": True, "message": f"Chose “{title}”. Filling it now.", "at": paths.now_utc(),
                            "purpose": "pick", "target": target}
        return {"ok": True, "purpose": "pick", "target": target, "picked_title": title}

    def finish_pasted(self, body: dict) -> dict:
        text = body.get("url")
        return self.finish(self.google.parse_pasted(text if isinstance(text, str) else ""))

    def sign_out(self, body: dict) -> dict:
        self.google.sign_out()
        return {"ok": True}

    # ---------------------------------------------------------------- clients

    def add_client(self, body: dict) -> dict:
        with self.db_lock:
            con = store.open_read(self.db_path)
            try:
                client = self.settings.add_client(body.get("rev"), body.get("username"), body.get("name"),
                                                  canonical=self.canonical(con))
                has_data = store.items_for_usernames(con, {client["reddit_username"].lower()}) > 0
            finally:
                con.close()
            if has_data:  # numbers saved earlier for this account: its Sheets need them
                self.job.bump()
        return {"client": client, "rev": self.settings.rev(), "sheets": self.job.start("client added") if has_data
                else None}

    def update_client(self, body: dict) -> dict:
        username = body.get("username")
        with self.db_lock:
            con = store.open_read(self.db_path)
            try:
                old = self.settings.client(body.get("id"))
                items = store.items_for_usernames(con, {old["reddit_username"].lower()}) if username is not None \
                    else None
                client = self.settings.update_client(body.get("rev"), old["id"], name=body.get("name"),
                                                     username_text=username, items_count=items,
                                                     canonical=self.canonical(con))
            finally:
                con.close()
            moved = client["reddit_username"].lower() != old["reddit_username"].lower()
            if moved:
                self.job.bump()
        return {"client": client, "rev": self.settings.rev(), "sheets": self.job.start("username changed") if moved
                else None}

    def pause(self, body: dict) -> dict:
        client, sheets = self.update_sheets(
            lambda: self.settings.set_paused(body.get("rev"), body.get("id"), body.get("paused")), "paused or resumed")
        return {"client": client, "rev": self.settings.rev(), "sheets": sheets}

    def remove(self, body: dict) -> dict:
        _, sheets = self.update_sheets(lambda: self.settings.remove_client(body.get("rev"), body.get("id")),
                                       "client removed")
        return {"rev": self.settings.rev(), "sheets": sheets}

    # ---------------------------------------------------------------- Sheets

    def new_sheet(self, body: dict) -> dict:
        target = body.get("target")
        self.target_sheet(target)
        name = "All clients" if target == ALL else self.settings.client(target)["name"]
        made = self.google.create_spreadsheet(f"Reddit Tracker – {name}", list(tab_headers()))
        _, sheets = self.update_sheets(lambda: self.settings.set_sheet(target, made["id"], made["title"]), "new Sheet")
        return {"sheet": made, "sheets": sheets}

    def stop_sheet(self, body: dict) -> dict:
        target = body.get("target")
        self.target_sheet(target)
        with self.db_lock:
            self.settings.clear_sheet(target)
        return {"ok": True}

    def update_now(self, body: dict) -> dict:
        self.settings.data()  # a damaged settings file is refused here (409)
        return {"sheets": self.update_sheets(reason="asked", force=True)[1]}

    def check_sheet(self, body: dict) -> dict:
        sheet_id = self.target_sheet(body.get("target"))
        if not sheet_id:
            raise Refused("no_sheet", "There's no Sheet to check yet. Choose one first.", 422)
        check = self.google.check_file(sheet_id)
        if check["code"] != "ok":
            raise Refused(check["code"], check["message"], 422)
        sheets = self.update_sheets(lambda: self.job.rewrite(sheet_id), "Sheet checked")[1]
        return {"sheet": {"id": sheet_id, "title": check["title"], "code": "ok"}, "sheets": sheets}

    def use_sheet(self, body: dict) -> dict:
        """"Keep the Sheet's numbers": take back what a Sheet (e.g. filled by an older tracker) holds for its
        clients, then write it as usual. What this computer has is never replaced."""
        target = body.get("target")
        sheet_id = self.target_sheet(target)
        if not sheet_id:
            raise Refused("no_sheet", "There's no Sheet to read.", 422)
        data = self.settings.data()
        clients = [c for c in data["clients"] if not c["paused"] and c.get("reddit_username")]
        if target != ALL:
            clients = [c for c in clients if c["id"] == target]
        if not clients:
            raise Refused("no_client", "This client is paused or has no Reddit username, so nothing can be kept.",
                          422)
        tabs = _tabs(self.google.get_meta(sheet_id, META_FIELDS))
        read = {name: (self.google.values_get(sheet_id, tabs[name]["title"]).get("values") or [])
                if name in tabs else [] for name in TABS}
        with self.db_lock:
            con = store.connect(self.db_path)
            try:
                configured = [c["reddit_username"] for c in data["clients"]]
                users = {c["reddit_username"].lower(): store.canonical_user(con, c["reddit_username"], configured)
                         for c in clients}
                store.backup_db(con, self.data / "backups")
                try:
                    got = store.import_sheet_tables(con, users, read["content"], read["content_history"],
                                                    read["account_daily"])
                except store.SheetLayoutError:
                    con.rollback()
                    raise Refused("not_ours", "This Sheet's tabs aren't laid out the way Reddit Tracker writes "
                                              "them, so its numbers can't be read. Make a new Sheet instead.", 422)
                con.commit()
            finally:
                con.close()
            self.job.bump()
        if not got["items"] and not got["days"]:
            raise Refused("nothing_to_keep", "This Sheet has no rows for this client's Reddit username, so "
                                             "there was nothing to keep.", 422)
        self.job.allow_shrink_once(sheet_id)  # what's left over (other accounts' rows) may go now
        sheets = self.update_sheets(lambda: self.job.rewrite(sheet_id), "kept the Sheet's numbers")[1]
        return {"kept": got, "sheets": sheets}

    def replace_anyway(self, body: dict) -> dict:
        sheet_id = self.target_sheet(body.get("target"))
        if not sheet_id:
            raise Refused("no_sheet", "There's no Sheet to replace.", 422)
        self.job.allow_shrink_once(sheet_id)
        return {"sheets": self.update_sheets(reason="Replace anyway")[1]}

    # ---------------------------------------------------------------- Data Studio

    def report_for(self, client: dict, template: str | None) -> dict | None:
        """None: no button (no template, or no Sheet). url None: the content tab's id isn't known yet."""
        sid = client["sheet_id"]
        if not template or not sid:
            return None
        tab = ((self.job.sheet_status(sid) or {}).get("tabs") or {}).get("content")
        return {"url": report_link(template, sid, tab, client["name"]) if isinstance(tab, int) else None}

    def report(self, body: dict) -> dict:
        """The Data Studio link for a client whose Sheet's tab ids aren't known yet: asks Google once."""
        template = report_template_id(self.settings.data()["options"].get("report_template"))
        if not template:
            raise Refused("report_off", "Data Studio reports are turned off. Choose a template in Settings.", 409)
        client = self.settings.client(body.get("client"))
        sid = client["sheet_id"]
        if not sid:
            raise Refused("no_sheet", "This client has no Google Sheet yet. Choose or make one first.", 422)
        tabs = _tabs(self.google.get_meta(sid, META_FIELDS))
        if "content" not in tabs:
            raise Refused("not_filled", "This client's Sheet has no numbers yet. Import their reports first, "
                                        "then try again.", 422)
        self.job.set_tabs(sid, {name: tabs[name]["id"] for name in TABS if name in tabs})
        return {"url": report_link(template, sid, tabs["content"]["id"], client["name"])}

    def set_report_template(self, body: dict) -> dict:
        """link: a report link or id (your own template), None (ours again) or "" (turned off)."""
        link = body.get("link")
        if link is not None and not isinstance(link, str):
            raise Refused("bad_request", "Paste the link to your Data Studio report.", field="link")
        value = None if link is None else (report_id_from(link) if link.strip() else "")
        if value == REPORT_TEMPLATE:
            value = None
        with self.db_lock:
            self.settings.set_option("report_template", value)
        template = report_template_id(value)
        return {"report": {"id": template, "is_default": template == REPORT_TEMPLATE,
                           "url": f"https://datastudio.google.com/reporting/{template}" if template else None}}

    # ---------------------------------------------------------------- the rest

    def new_link(self, body: dict) -> dict:
        if not self.network:
            raise Refused("not_network", "Other devices can only use Reddit Tracker when it's started with "
                                         "--network (the Raspberry Pi installer does that).", 409)
        with self.db_lock:
            self._key = self.settings.new_access_key()
        return {"links": self.links()}

    def restore(self, body: dict) -> dict:
        with self.db_lock:
            return {"ok": True, **self.settings.restore_last_saved()}

    def export(self, params: dict) -> tuple[str, Iterator[bytes]]:
        """(file name, CSV bytes in parts) of one client's tab, for "Download CSV": only that tab of that
        client is read, and sent as it's read (a long history is never in memory at once)."""
        client = self.settings.client((params.get("client") or [""])[0])
        tab = (params.get("tab") or ["content"])[0]
        if tab not in TABS:
            raise Refused("bad_request", "There's no such tab.")

        def parts():
            with self.read_tables({client["reddit_username"].lower()}, tab) as tables:
                yield "\ufeff".encode("utf-8")
                for text in store.export_csv(tables[tab]):
                    yield text.encode("utf-8")
        csv_parts = parts()
        first = next(csv_parts)  # the tables are read here: a problem is answered before the download starts
        return f"{client['name']} {tab}.csv", itertools.chain([first], csv_parts)


ROUTES = {"/api/files/preview": App.preview, "/api/files/import": App.import_files, "/api/google/begin": App.begin,
          "/api/google/finish": App.finish_pasted, "/api/google/signout": App.sign_out,
          "/api/clients/add": App.add_client, "/api/clients/update": App.update_client, "/api/clients/pause": App.pause,
          "/api/clients/remove": App.remove, "/api/sheets/new": App.new_sheet, "/api/sheets/stop": App.stop_sheet,
          "/api/sheets/update": App.update_now, "/api/sheets/check": App.check_sheet,
          "/api/sheets/replace-anyway": App.replace_anyway, "/api/sheets/use-sheet": App.use_sheet, "/api/network/new-link": App.new_link,
          "/api/settings/restore": App.restore, "/api/report": App.report,
          "/api/settings/report": App.set_report_template}


def run_command(*options: str) -> str:
    """How to run reddit_tracker.py with these options in a terminal on this computer."""
    return " ".join(("py" if os.name == "nt" else "python3", f'"{paths.app_dir() / "reddit_tracker.py"}"') + options)


def text_html(text: str) -> str:
    return html.escape(text, quote=False)


def small_page(title: str, body: str) -> bytes:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width='
            f'device-width,initial-scale=1"><link rel="icon" href="data:,"><title>{text_html(title)} · Reddit Tracker'
            '</title><style>body{font:16px/'
            '1.5 system-ui,sans-serif;max-width:34em;margin:12vh auto;padding:0 16px;color:#1c1c1c;background:#fff}'
            '@media(prefers-color-scheme:dark){body{color:#e6e6e6;background:#141414}}input{width:100%;box-sizing:'
            'border-box;font:inherit;padding:8px}button{font:inherit;min-height:44px;padding:8px 16px;margin-top:8px}'
            f'</style></head><body>{body}</body></html>').encode("utf-8")


CLOSE_SOON = "<script>setTimeout(function(){window.close()},1500)</script>"
COPY_SCRIPT = ("<script>document.getElementById('c').onclick=function(){var i=document.getElementById('u'),"
               "m=document.getElementById('m');i.focus();i.select();(navigator.clipboard?navigator.clipboard."
               "writeText(i.value):Promise.reject()).then(function(){m.textContent='Copied.'},function(){"
               "m.textContent='Press Ctrl+C (⌘C on a Mac) to copy it.'})}</script>")


class Handler(BaseHTTPRequestHandler):
    server_version = "RedditTracker"
    sys_version = ""
    timeout = 300
    is_local = False
    answered = False  # the answer has begun: a problem now can only cut it short

    @property
    def app(self) -> App:
        return self.server.app

    def local(self) -> bool:
        """This computer's own browser at its own address. Anything else needs the key (DNS rebinding too)."""
        port = self.server.server_address[1]
        names = ("127.0.0.1", "localhost", "[::1]")
        hosts = [f"{name}:{port}" for name in names] + (list(names) if port == 80 else [])  # browsers drop :80
        return peer_is_loopback(self) and self.headers.get("Host", "") in hosts

    def cookie(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def allowed(self) -> bool:
        if self.is_local:
            return True
        key = self.app.access_key() if self.app.network else None
        got = self.cookie()
        return bool(key and got) and hmac.compare_digest(got.encode("utf-8"), key.encode("utf-8"))

    # ---------------------------------------------------------------- answers

    def end_headers(self) -> None:
        for name, value in SECURITY:
            self.send_header(name, value)
        super().end_headers()

    def send(self, status: int, body, kind: str, headers: tuple = ()) -> None:
        """body: bytes, or bytes in parts (a download, sent as it's made: closing the connection ends it)."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            if isinstance(body, bytes):
                self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.answered = True
            for part in [body] if isinstance(body, bytes) else body:
                self.wfile.write(part)
        except (ConnectionError, socket.timeout):
            pass  # the tab was closed or the connection dropped; the work itself is done

    def json(self, data: dict, status: int = 200) -> None:
        self.send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def page(self, status: int, title: str, body: str) -> None:
        self.send(status, small_page(title, body), "text/html; charset=utf-8")

    def refuse(self, e) -> None:
        status = GOOGLE_STATUS.get(e.code, 502) if isinstance(e, GoogleError) else e.status  # not Google's own
        self.json({"error": e.message, "code": e.code, "field": getattr(e, "field", None)}, status)

    def need_link(self, as_page: bool = False) -> None:
        if self.app.network:
            options = ["--show-link"] + ([] if self.app.port == DEFAULT_PORT else ["--port", str(self.app.port)])
            if self.app.data != paths.default_data_dir():
                options += ["--data", f'"{self.app.data}"']
            text = ("This device needs the Reddit Tracker link (it ends in ?key=…). On the computer where Reddit "
                    f"Tracker runs, run: {run_command(*options)}. Or copy it from Settings → Use from other devices "
                    "on a device that already works.")
        else:
            text = f"Reddit Tracker only answers on this computer. Open http://127.0.0.1:{self.server.server_address[1]}"
        if as_page:
            return self.page(403, "Needs the link", f"<h1>This device needs the link</h1><p>{text_html(text)}</p>")
        self.json({"error": text, "code": "need_link"}, 403)

    def key_cookie(self) -> tuple:
        return (("Set-Cookie", f"{COOKIE}={self.app.access_key()}; HttpOnly; SameSite=Lax; Max-Age=31536000; Path=/"),)

    def log_request(self, code="-", size="-") -> None:
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        command = getattr(self, "command", None) or "-"
        code = code.value if isinstance(code, HTTPStatus) else code
        if not (command == "GET" and path in QUIET and str(code) == "200"):
            self.app.log(f"{command} {path} {code}", echo=False)

    def log_message(self, *args) -> None:  # never the raw request line: it can hold a sign-in code or a key
        pass

    def failed(self, e: Exception, path: str) -> None:
        self.app.log(f"{self.command} {path} failed: {type(e).__name__}: {str(e)[:300]}")
        if self.answered:  # a download that had begun: it ends short (the connection closes)
            self.close_connection = True
            return
        said = {"/api/files/import": "The import didn't finish: {}. Drop the files again to see which ones were saved.",
                "/api/files/preview": "The files couldn't be checked: {}.",
                "/api/status": "Reddit Tracker can't show your clients: {}."}.get(path, "That didn't work: {}.")
        self.json({"error": said.format(plain(e)), "code": "status_failed" if path == "/api/status" else "failed"},
                  500)

    # ---------------------------------------------------------------- GET

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        self.is_local = self.local()
        if path == "/oauth":
            return self.oauth(query) if self.is_local else self.need_link(as_page=True)
        if path == "/" and "key" in params:
            return self.key_link(params["key"][0])
        if not self.allowed():
            return self.need_link(as_page=path == "/")
        try:
            if path == "/":
                body = PAGE.read_bytes()
                if self.app.google.test_mode:
                    body = body.replace(b"<body>", b"<body>" + TEST_BANNER, 1)
                return self.send(200, body, "text/html; charset=utf-8", () if self.is_local else self.key_cookie())
            if path in FONTS:
                return self.send(200, FONTS[path].read_bytes(), "font/woff2")
            if path == "/api/ping":
                return self.json({"app": "reddit-tracker", "version": VERSION, "network": self.app.network})
            if path == "/api/status":
                return self.json(self.app.status(remote=not self.is_local))
            if path == "/api/export.csv":
                name, data = self.app.export(params)
                plain_name = re.sub(r"[^A-Za-z0-9 ._-]", "_", name)
                return self.send(200, data, "text/csv; charset=utf-8", (("Content-Disposition", (
                    f'attachment; filename="{plain_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}')),))
            self.json({"error": "There's no such page.", "code": "no_such_page"}, 404)
        except (SettingsError, Refused, GoogleError) as e:
            self.refuse(e)
        except Exception as e:
            self.failed(e, path)

    def key_link(self, key: str) -> None:
        """The link for other devices: the right key sets the cookie and goes to the page."""
        app_key = self.app.access_key() if self.app.network else None
        if app_key and hmac.compare_digest(key.encode("utf-8"), app_key.encode("utf-8")):
            return self.send(302, b"", "text/plain", (("Location", "/"),) + self.key_cookie())
        if self.allowed():  # this computer, or a device that still has a working key (an old bookmark)
            return self.send(302, b"", "text/plain", (("Location", "/"),))
        self.need_link(as_page=True)

    def oauth(self, query: str) -> None:
        """Where Google sends the browser back after Allow. Never shows what the address holds,
        except to hand it to the device where the sign-in started."""
        try:
            done = self.app.finish(urllib.parse.parse_qs(query, keep_blank_values=True))
            text = f"Got your Sheet “{done['picked_title']}”." if done["purpose"] == "pick" else "You're signed in."
            return self.page(200, "Done", f"<h1>{text_html(text)}</h1><p>You can close this tab and go back to "
                                          f"Reddit Tracker.</p>{CLOSE_SOON}")
        except GoogleError as e:
            if e.code == "bad_state":
                url = html.escape(f"http://{self.headers.get('Host')}{self.path}")
                return self.page(200, "Sign-in", (
                    "<h1>Finish signing in on the other device</h1><p>This sign-in was started on another device (like "
                    "a Raspberry Pi), or it's old. If you started it on another device, copy this address and paste it "
                    f'there:</p><input id="u" readonly value="{url}" aria-label="The whole address"><button id="c">'
                    'Copy</button><p id="m" role="status"></p><p>Otherwise go back to Reddit Tracker and try again.</p>'
                    + COPY_SCRIPT))
            message = e.message
        except (SettingsError, Refused) as e:
            message = e.message
        except Exception as e:
            self.app.log(f"GET /oauth failed: {type(e).__name__}")
            message = sentence(e)
        self.page(200, "That didn't work", f"<h1>That didn't work</h1><p>{text_html(message)}</p><p>You can close "
                                           "this tab and go back to Reddit Tracker.</p>")

    # ---------------------------------------------------------------- POST

    def from_page(self) -> bool:
        """Only Reddit Tracker's own page sends JSON with X-RT, from its own address."""
        origin = self.headers.get("Origin")
        return (self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"
                and self.headers.get("X-RT") == "1"
                and (origin is None or urllib.parse.urlsplit(origin).netloc == self.headers.get("Host")))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        self.is_local = self.local()
        if not self.allowed():
            return self.need_link()
        if not self.from_page():
            return self.json({"error": "Reddit Tracker only takes requests from its own page.", "code": "forbidden"}, 403)
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.json({"error": "The request had no length.", "code": "bad_request"}, 411)
        if size > MAX_BODY:
            self.close_connection = True
            left = size if size <= 2 * MAX_BODY else 0  # read it, so the browser gets to see the answer
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
            return self.json({"error": TOO_BIG, "code": "too_big"}, 413)
        try:
            body = json.loads(self.rfile.read(max(size, 0)).decode("utf-8") or "{}")
        except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested too deep
            body = None
        if not isinstance(body, dict):
            return self.json({"error": "The request wasn't readable. Reload the page.", "code": "bad_request"}, 400)
        route = ROUTES.get(path)
        if route is None:
            return self.json({"error": "There's no such page.", "code": "no_such_page"}, 404)
        try:
            answer = route(self.app, body)
            if "links" in answer:  # the page opens the first one: keep this device on the address it uses
                answer["links"].sort(key=lambda link: urllib.parse.urlsplit(link).netloc != self.headers.get("Host"))
            self.json(answer)
        except (SettingsError, Refused, GoogleError) as e:
            self.refuse(e)
        except Exception as e:
            self.failed(e, path)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    app: App

    def handle_error(self, request, client_address) -> None:
        """A request that broke off: a closed tab or a dropped connection is normal and stays quiet;
        anything else goes in log.txt (never a traceback in the console window)."""
        e = sys.exc_info()[1]
        if not isinstance(e, (ConnectionError, socket.timeout)):
            self.app.log(f"A request failed: {type(e).__name__}: {str(e)[:300]}", echo=False)


def create(app: App, port: int, host: str | None = None) -> Server:
    """Bind the server on 127.0.0.1 (with --network: every address of this computer)."""
    server = Server((host or ("0.0.0.0" if app.network else "127.0.0.1"), port), Handler)
    server.app = app
    app.port = server.server_address[1]
    return server


def ping(port: int, tries: int = 1) -> dict | None:
    """The /api/ping answer of a Reddit Tracker on this port, or None. tries > 1 also waits a moment for
    a copy that is only just starting (two starts at the same moment)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i in range(tries):
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
                answer = json.loads(r.read().decode("utf-8"))
            if isinstance(answer, dict) and answer.get("app") == "reddit-tracker":
                return answer
        except Exception:
            pass
        if i + 1 < tries:
            time.sleep(0.25)
    return None


def already_running(args, port: int, answer: dict) -> int:
    """Reddit Tracker already answers on this port: open it (0), or say why this start can't work (3)."""
    url = f"http://127.0.0.1:{port}"
    if args.network and not answer.get("network"):
        say("Reddit Tracker is already open on this computer, but only for this computer. Close that window, "
            "then start it again.")
        return 3  # won't work until the other copy is closed (the Raspberry Pi service doesn't retry on 3)
    if args.network or args.no_browser:
        say(f"Reddit Tracker is already running: {url}")
    else:
        say(f"Reddit Tracker is already running. Opening it: {url}")
        webbrowser.open(url)
    return 0


def show_link(data: Path, port: int) -> int:
    """--show-link: print the links for other devices, without starting anything."""
    settings = Settings(data / "settings.json", data / "backups")
    try:
        key = settings.option("access_key")
    except SettingsError as e:
        key = key_in_backup(e.backup)
    if not key:
        say("Reddit Tracker hasn't started in network mode yet.")
        return 0
    for host in lan_hosts():
        say(f"http://{host}:{port}/?key={key}")
    return 0


def banner(app: App) -> None:
    lines = ["", f"Reddit Tracker {VERSION} is running.", f"Open it in your browser: http://127.0.0.1:{app.port}"]
    if app.network:
        lines += ["On your phone or another computer on the same network, open (keep this link private):"]
        lines += [f"  {link}" for link in app.links()]
    if app.google.test_mode:
        lines.append("TEST MODE: Google is simulated on this computer. Nothing goes to the real Google.")
    lines += [f"Your data folder: {app.data}", "Keep this window open while you use Reddit Tracker. Close it to stop.",
              ""]
    say("\n".join(lines))


STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")


def _stop(*_) -> None:
    _hold_signals()  # closing a window sends SIGHUP twice: the second must not cut the clean-up short
    raise KeyboardInterrupt


def _hold_signals() -> dict:
    """Ignore Ctrl+C, SIGTERM and SIGHUP while Reddit Tracker closes up: {signal: its handler before}."""
    held = {}
    if threading.current_thread() is threading.main_thread():
        for name in STOP_SIGNALS:
            if hasattr(signal, name):
                held[getattr(signal, name)] = signal.signal(getattr(signal, name), signal.SIG_IGN)
    return held


def main(args, on_ready=None) -> int:
    """Start Reddit Tracker (args from reddit_tracker.py). on_ready(server) is for tests."""
    data = paths.data_dir(args.data)
    port = args.port or DEFAULT_PORT
    if args.show_link:
        return show_link(data, port)
    app = server = None
    served, before = False, {}
    try:
        for p in range(port, port + (1 if args.network or args.port else 11)):  # a fixed port, or the next free one
            answer = ping(p)
            if answer:
                return already_running(args, p, answer)
            try:
                app = app or App(data, network=args.network)
                server = create(app, p)
                break
            except store.ForeignDatabase as e:
                say(f"Reddit Tracker didn't start. {e}")
                return 1
            except sqlite3.Error as e:
                answer = "locked" in str(e) and ping(p, tries=12)  # another copy starting at the same moment
                if answer:
                    return already_running(args, p, answer)
                say(f"Reddit Tracker didn't start. {CANT_SAVE} Its data folder: {data}" if engine.unwritable(e) else
                    f"Reddit Tracker didn't start: its database in {data} can't be opened ({e}).")
                return 1
            except OSError:
                answer = ping(p, tries=2)  # taken a moment ago by another copy starting at the same moment?
                if answer:
                    return already_running(args, p, answer)
        if server is None:
            say(f"Reddit Tracker didn't start: another program is using port {port}. Close that program, or start "
                f"Reddit Tracker with another port, for example: {run_command('--port', str(port + 20))}")
            return 1
        banner(app)
        app.log(f"Reddit Tracker {VERSION} started on port {app.port}.", echo=False)

        def resume() -> None:
            try:
                if app.job.resume() == "started":
                    app.log("The Google Sheets were missing the newest numbers, so they're being updated now.")
            except Exception as e:
                app.log(f"The Google Sheets update couldn't start: {type(e).__name__}")
        threading.Thread(target=resume, daemon=True).start()
        if not (args.no_browser or args.network):
            threading.Timer(0.5, webbrowser.open, [f"http://127.0.0.1:{app.port}"]).start()
        if on_ready:
            on_ready(server)
        if threading.current_thread() is threading.main_thread():  # systemd's stop: close up like Ctrl+C
            before[signal.SIGINT] = signal.getsignal(signal.SIGINT)
            before[signal.SIGTERM] = signal.signal(signal.SIGTERM, _stop)
            # closing the Terminal window too (unless started with nohup, which ignores it)
            if hasattr(signal, "SIGHUP") and signal.getsignal(signal.SIGHUP) is signal.SIG_DFL:
                before[signal.SIGHUP] = signal.signal(signal.SIGHUP, _stop)
        served = True
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            say("Reddit Tracker stopped.")
        return 0
    finally:
        held = _hold_signals()
        try:
            if server:
                server.server_close()
            if app:
                app.close(stopping=served)
        finally:
            for sig, handler in {**held, **before}.items():  # as they were before (this may be a test)
                signal.signal(sig, handler)
