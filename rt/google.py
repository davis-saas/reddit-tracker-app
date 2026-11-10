"""Google sign-in (OAuth with PKCE on a loopback address), the saved sign-in, and the Sheets and
Drive calls Reddit Tracker makes.

Only the drive.file permission is asked for: Reddit Tracker can open the Sheets it made and the
ones the user picked in Google's file chooser, nothing else.
"""
from __future__ import annotations

import base64
import collections
import functools
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from rt import paths

SCOPE = "https://www.googleapis.com/auth/drive.file"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
EXCEL_MIMES = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel",
               "application/vnd.oasis.opendocument.spreadsheet")
ABOUT_FIELDS = "user(displayName,emailAddress,permissionId)"
FILE_FIELDS = "name,mimeType,trashed,capabilities/canEdit"
META_FIELDS = "properties.title,sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"

TIMEOUT = 60
PENDING_SECONDS = 15 * 60       # a sign-in link works this long; finished ones are remembered as long
LONE_PICK_SECONDS = 10 * 60     # a Sheet-picker answer without state is accepted only this soon
MAX_PENDING = 5
WRITES_PER_MINUTE = 50          # Google allows 60 Sheets writes a minute per user; stay under it
QUOTA_WAITS = (20, 40, 65)      # "slow down" answers
RETRY_WAITS = (0, 10, 20)       # Google busy, or the connection dropped
RETRY_STATUSES = (500, 502, 503, 504)
MAX_RETRY_AFTER = 120
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
# The sign-in endpoint turned down google_app.json itself (secret reset, client deleted or disabled)
CLIENT_REFUSED = {"invalid_client", "unauthorized_client", "deleted_client", "disabled_client"}
CELL_LIMIT = re.compile(r"above the limit of [\d,]+ cells", re.I)

MESSAGES = {
    "api_off": "A Google service Reddit Tracker needs (Sheets, Drive or Picker) is switched off for this copy. "
               "Tell whoever set it up.",
    "protected": "Part of this Sheet is protected, so Reddit Tracker can't change it. In the Sheet, remove the "
                 "protection (Data → Protected sheets and ranges), then try again.",
    "not_found": "Reddit Tracker can't open this Sheet any more. Choose it again.",
    "read_only": "You can only view this Sheet, not change it. Choose a Sheet you can edit.",
    "trashed": "This Sheet is in the Google Drive trash. Restore it (Drive → Trash → Restore), or choose "
               "another Sheet.",
    "excel_file": "This is an Excel file stored in Google Drive, not a Google Sheet. Open it, choose File → Save "
                  "as Google Sheets, then choose the new Sheet.",
    "not_a_sheet": "That isn't a Google Sheet. Choose a Google Sheet.",
    "quota": "Google asked Reddit Tracker to slow down. Wait a few minutes, then try again.",
    "drive_full": "Your Google Drive is full, so Reddit Tracker can't make a new Sheet. Free up some space, or "
                  "choose an existing Sheet.",
    "storage_full": "The Google account that owns this Sheet is out of storage, so Google won't let anyone change "
                    "it. Free up space in that Google Drive, then press Update Google Sheets now.",
    "too_big": "This Sheet is full (Google's cell limit). Make a new Sheet for it.",
    "too_much_one": "This client has more numbers than one Google Sheet can hold (Google's limit is 10 million "
                    "cells), so a new Sheet won't help. Everything is still saved in Reddit Tracker. To clear this, "
                    "open Edit and press Stop using this Sheet.",
    "too_much": "There are more numbers than one Google Sheet can hold (Google's limit is 10 million cells), so a "
                "new Sheet won't help. Everything is still saved in Reddit Tracker, and the other Sheets still update. "
                "To clear this, pause the clients you no longer need, or press Stop using it.",
    "google_down": "Google had a temporary problem (error {status}). Try again in a few minutes.",
    "rejected": "Google ended Reddit Tracker's sign-in. Sign in with Google again.",
    "signed_out": "You're not signed in to Google. Sign in with Google again.",
    "offline": "Reddit Tracker can't reach Google. Check the internet connection, then try again.",
    "cert": "This computer couldn't check Google's security certificate. Make sure the date and time are right. "
            "On a Mac with Python from python.org, open Applications → Python 3.x → Install Certificates.command.",
    "other": "Google gave an unexpected answer (error {status}{said}). Try again in a few minutes.",
    "bad_state": "This sign-in link is old or was already used. Try again.",
    "denied": "Google sign-in was cancelled, or access wasn't allowed. Try again and click Allow.",
    "exchange_failed": "Google didn't finish the sign-in. Try again.",
    "bad_paste": "That isn't the whole address. It starts with http://127.0.0.1 and contains code=. Copy it again "
                 "from the address bar.",
    "code_expired": "Google's sign-in code ran out of time. Click Sign in with Google again and paste the address "
                    "within a few minutes.",
    "wrong_account": "You chose the Sheet while signed in to Google as {new}. Reddit Tracker uses {old}. Choose it "
                     "again and pick {old} in Google's account list.",
    "other_account": "You're now signed in as {new}. Your Sheets were set up with {old}: sign in as {old} to keep "
                     "updating them, or choose each client's Sheet again.",
    "pick_failed": "Couldn't finish choosing the Sheet. Check the internet and try again.",
    "signin_failed": "Couldn't finish signing in. Check the internet and try again.",
    "no_app_file": "This copy of Reddit Tracker has no Google connection file (google_app.json), so it can't use "
                   "Google Sheets yet.",
    "bad_app_file": "This copy of Reddit Tracker's Google connection file (google_app.json) can't be read. Get a "
                    "fresh copy from whoever set it up.",
    "app_refused": "Google no longer accepts this copy of Reddit Tracker. Download the newest Reddit Tracker and "
                   "start it; your clients and numbers are kept.",
    "bad_number": "One of the numbers isn't a real number, so Google won't take it. Tell whoever set up Reddit "
                  "Tracker.",
}


class GoogleError(Exception):
    """A Google problem with a short code and a plain message ready to show."""

    def __init__(self, code: str, message: str | None = None, status: int | None = None,
                 reason: str | None = None, offline: bool = False):
        self.code, self.status, self.reason, self.offline = code, status, reason, offline
        self.message = message or MESSAGES.get(code) or "Something went wrong with Google. Try again."
        super().__init__(self.message)


def utc_stamp(t: float | None = None) -> str:
    """Epoch seconds (default: now) as UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def write_private(path: Path, data: bytes) -> None:
    """Replace a file in one step (temp file in the same folder, private from the start, fsync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        paths.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@functools.lru_cache(maxsize=None)
def ssl_context() -> ssl.SSLContext:
    """The system's certificates; on a Mac whose Python has none of its own, macOS's root list."""
    ctx = ssl.create_default_context()
    if sys.platform == "darwin":
        cafile = ssl.get_default_verify_paths().openssl_cafile
        if not (cafile and os.path.exists(cafile)) and os.path.exists("/etc/ssl/cert.pem"):
            ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


def _http_error(status: int, raw: bytes) -> GoogleError:
    """Turn a Google error answer into a GoogleError. Never shows the raw body (it can be a web page)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = None
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, str):  # the sign-in endpoint: {"error": "invalid_grant", "error_description": ...}
        reasons, said = [err], str(data.get("error_description") or err)
    elif isinstance(err, dict):
        reasons = [str(e.get("reason")) for e in (err.get("errors") or []) + (err.get("details") or [])
                   if isinstance(e, dict) and e.get("reason")]
        said = str(err.get("message") or "")
    else:
        reasons, said = [], ""
    blob = json.dumps(err) if err is not None else ""
    reason = reasons[0] if reasons else None
    if "SERVICE_DISABLED" in blob or "accessNotConfigured" in blob:
        code = "api_off"
    elif status == 429 or RATE_REASONS & set(reasons):
        code = "quota"
    elif "storageQuotaExceeded" in reasons or "storage quota" in said.lower():
        code = "storage_full"  # create_spreadsheet calls it drive_full
    elif status == 403 and "protected" in blob.lower():
        code = "protected"
    elif status == 400 and CELL_LIMIT.search(said):
        code = "too_big"
    elif status == 401:
        code = "signed_out"
    elif status in (403, 404):
        code = "not_found"
    elif status >= 500:
        return GoogleError("google_down", MESSAGES["google_down"].format(status=status), status, reason)
    else:
        said = f": {said[:150]}" if said and data is not None else ""
        return GoogleError("other", MESSAGES["other"].format(status=status, said=said), status, reason)
    return GoogleError(code, status=status, reason=reason)


def _is_cert(e: BaseException) -> bool:
    return isinstance(e, ssl.SSLCertVerificationError) or isinstance(
        getattr(e, "reason", None), ssl.SSLCertVerificationError)


def problem(exc: BaseException) -> tuple[str, str, bool]:
    """(code, plain message, whether Google can't be reached at all) for any error."""
    if isinstance(exc, GoogleError):
        return exc.code, exc.message, exc.offline
    if isinstance(exc, urllib.error.HTTPError):
        e = _http_error(exc.code, b"")
        return e.code, e.message, False
    if _is_cert(exc):
        return "cert", MESSAGES["cert"], False
    if isinstance(exc, (urllib.error.URLError, ConnectionError, socket.timeout, http.client.HTTPException)):
        return "offline", MESSAGES["offline"], True
    return "other", f"Something unexpected went wrong: {str(exc)[:300] or type(exc).__name__}", False


def _retry_after(headers) -> float | None:
    try:
        return min(float(headers.get("Retry-After")), MAX_RETRY_AFTER)
    except (AttributeError, TypeError, ValueError):
        return None


def _about_of(answer: dict) -> dict:
    user = answer.get("user") or {}
    return {"email": user.get("emailAddress"), "name": user.get("displayName"),
            "permission_id": user.get("permissionId")}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _Limiter:
    """At most `limit` calls per rolling `window` seconds; waits before the one that would go over."""

    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None], limit: int, window: float = 60.0):
        self.clock, self.sleep, self.limit, self.window = clock, sleep, limit, window
        self.times: collections.deque = collections.deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            while True:
                now = self.clock()
                while self.times and now - self.times[0] >= self.window:
                    self.times.popleft()
                if len(self.times) < self.limit:
                    self.times.append(now)
                    return
                self.sleep(self.times[0] + self.window - now)


class Google:
    """One per app: sign-in flows, the saved sign-in (google_token.json) and every Google call."""

    def __init__(self, data_dir: Path, app_file: Path | None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] | None = None):
        self.token_path = Path(data_dir) / "google_token.json"
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda text: print(text, file=sys.stderr))
        self.last: dict | None = None
        self.app, self.app_problem = self._read_app(app_file)
        self.configured = self.app is not None
        base = os.environ.get("RT_GOOGLE_BASE", "").strip().rstrip("/")
        if base and not re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d{1,5}", base):
            self.log("RT_GOOGLE_BASE is ignored: it may only be http://127.0.0.1:<port> or http://localhost:<port>.")
            base = ""
        self.test_mode = bool(base)
        self.auth_url = base + "/o/oauth2/v2/auth" if base else "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_url = base + "/token" if base else "https://oauth2.googleapis.com/token"
        self.sheets_url = base + "/v4/spreadsheets" if base else "https://sheets.googleapis.com/v4/spreadsheets"
        self.drive_url = base + "/drive/v3" if base else "https://www.googleapis.com/drive/v3"
        handlers = [urllib.request.HTTPSHandler(context=ssl_context())]
        if self.test_mode:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)
        self._limiter = _Limiter(clock, sleep, WRITES_PER_MINUTE)
        self._lock = threading.Lock()           # pending and finished sign-ins
        self._token_lock = threading.RLock()    # google_token.json, refreshes
        self._pending: dict[str, dict] = {}
        self._finished: dict[str, dict] = {}

    # ---------------------------------------------------------------- app file and saved sign-in

    @staticmethod
    def _read_app(app_file: Path | None) -> tuple[tuple[str, str] | None, str | None]:
        if app_file is None or not Path(app_file).is_file():
            return None, "no_app_file"
        try:
            data = json.loads(Path(app_file).read_text(encoding="utf-8"))
            info = data.get("installed") or data
            client_id, secret = info["client_id"], info["client_secret"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None, "bad_app_file"
        if not (isinstance(client_id, str) and client_id and isinstance(secret, str) and secret):
            return None, "bad_app_file"
        return (client_id, secret), None

    def _need_app(self) -> tuple[str, str]:
        if self.app is None:
            raise GoogleError(self.app_problem or "no_app_file")
        return self.app

    def _read_token(self) -> dict | None:
        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return token if isinstance(token, dict) else None

    def _write_token(self, token: dict) -> None:
        with self._token_lock:
            write_private(self.token_path, json.dumps(token, indent=1).encode("utf-8"))

    def status(self) -> dict:
        token = self._read_token() or {}
        rejected = bool(token.get("rejected"))
        out = {"configured": self.configured,
               "signed_in": self.configured and not rejected and bool(token.get("refresh_token")
                                                                      or token.get("access_token")),
               "email": token.get("email"), "name": token.get("name"),
               "problem": self.app_problem or ("rejected" if rejected else None)}
        if self.test_mode:
            out["test_mode"] = True
        return out

    def sign_out(self) -> None:
        """Forget the sign-in on this computer, keeping only who it was: signing in later as someone else
        then says why the Sheets stopped. Never revokes: that could drop every Sheet's access."""
        with self._token_lock:
            token = self._read_token() or {}
            if token.get("signed_out"):
                return
            who = {k: token[k] for k in ("email", "name", "permission_id", "before") if token.get(k)}
            if who.get("permission_id"):
                try:
                    self._write_token({"signed_out": True, **who})
                    return
                except OSError:  # a full disk: forgetting who it was is fine too
                    pass
            try:
                self.token_path.unlink()
            except FileNotFoundError:
                pass

    def access_token(self, force: bool = False) -> str:
        """A working access token, refreshed when it has under a minute left (or when force is set)."""
        with self._token_lock:
            client_id, secret = self._need_app()
            token = self._read_token()
            if not token or token.get("signed_out"):
                raise GoogleError("signed_out")
            if token.get("rejected"):
                raise GoogleError("rejected")
            if not force and token.get("access_token") and token.get("expires_at", 0) - time.time() > 60:
                return token["access_token"]
            if not token.get("refresh_token"):
                raise self._reject(token)
            try:
                got = self._send("POST", self.token_url, form={
                    "client_id": client_id, "client_secret": secret, "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"]})
            except GoogleError as e:
                # unauthorized_client here: the sign-in was made with another google_app.json (an older
                # Reddit Tracker), so signing in again with this one fixes it
                if e.reason in ("invalid_grant", "unauthorized_client"):
                    raise self._reject(token) from None
                if e.reason in CLIENT_REFUSED:
                    raise self._app_refused() from None
                raise
            token.update(access_token=got.get("access_token"),
                         expires_at=time.time() + float(got.get("expires_in") or 3600))
            if got.get("refresh_token"):
                token["refresh_token"] = got["refresh_token"]
            try:
                self._write_token(token)
            except OSError:  # a full or read-only disk: this token works now; the next call refreshes again
                if got.get("refresh_token"):
                    raise
            return token["access_token"]

    def _reject(self, token: dict) -> GoogleError:
        """Mark the saved sign-in as turned down (keeping who it was, for "sign in again as …")."""
        token["rejected"] = True
        token.pop("access_token", None)
        self._write_token(token)
        return GoogleError("rejected")

    def _app_refused(self) -> GoogleError:
        """Google turned down google_app.json itself: stop using it until a newer Reddit Tracker starts."""
        self.app, self.app_problem, self.configured = None, "app_refused", False
        return GoogleError("app_refused")

    # ---------------------------------------------------------------- sign-in flows

    @property
    def busy(self) -> bool:
        """Whether a sign-in (or Sheet choice) was started and hasn't finished or run out of time."""
        with self._lock:
            self._prune()
            return bool(self._pending)

    def _prune(self) -> None:
        now = self.clock()
        for store in (self._pending, self._finished):
            for key in [k for k, v in store.items() if now - v["created"] > PENDING_SECONDS]:
                del store[key]

    def begin(self, purpose: str, target: str | None = None, port: int = 8765, file_id: str | None = None) -> dict:
        """Start a sign-in ("signin") or a Sheet choice ("pick"): returns the Google address to open."""
        if purpose not in ("signin", "pick"):
            raise ValueError(f"unknown purpose {purpose!r}")
        client_id, _ = self._need_app()
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
        redirect_uri = f"http://127.0.0.1:{int(port)}/oauth"
        token = self._read_token() or {}
        # A Sheet choice keeps the saved sign-in, so it asks for no lasting one of its own (Google keeps
        # only so many per account); without a working saved sign-in it signs in as well
        online = purpose == "pick" and bool(token.get("refresh_token")) and not token.get("rejected")
        query = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
                 "access_type": "online" if online else "offline", "prompt": "consent",
                 "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
                 "code_challenge_method": "S256", "state": state}
        if token.get("email") and not token.get("signed_out"):  # after Sign out, Google asks which account
            query["login_hint"] = token["email"]
        if purpose == "pick":
            query.update(trigger_onepick="true", mimetypes=SHEET_MIME)
            if file_id:
                query["file_ids"] = file_id
        with self._lock:
            self._prune()
            self._pending[state] = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                                    "purpose": purpose, "target": target, "created": self.clock()}
            while len(self._pending) > MAX_PENDING:
                del self._pending[min(self._pending.values(), key=lambda e: e["created"])["state"]]
        return {"url": self.auth_url + "?" + urllib.parse.urlencode(query), "state": state}

    def parse_pasted(self, text: str) -> dict:
        """The answer's parameters from a pasted address (full, without http://, or just the part after ?)."""
        text = (text or "").strip().strip("\"'<>")
        head, _, query = text.partition("?") if "?" in text else ("", "", text)
        host = re.sub(r"^[a-z]+://", "", head, flags=re.I).split("/")[0].lower()
        if host and not re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", host):
            raise GoogleError("bad_paste")
        params = dict(urllib.parse.parse_qsl(query.split("#")[0], keep_blank_values=True))
        if not (params.get("code") or params.get("error")):
            raise GoogleError("bad_paste")
        return params

    def finish(self, params: dict) -> dict:
        """Complete a sign-in from Google's answer: {"purpose", "target", "picked": [file ids]}.
        A repeated answer gets the same outcome as the first time (waiting for it if it's still coming)."""
        p = {k: (v[0] if isinstance(v, (list, tuple)) and v else v) for k, v in dict(params).items()}
        state = str(p.get("state") or "")
        key = state or f"code:{p.get('code') or ''}"
        with self._lock:
            self._prune()
            done = self._finished.get(key)
            if done is None:
                entry = self._pending.pop(state, None) if state else self._lone_pick()
                if entry is None:
                    raise GoogleError("bad_state")
                mine = self._finished[key] = {"outcome": None, "created": self.clock(), "ready": threading.Event()}
        if done is not None:  # the same answer again (a reloaded tab, or pasted as well)
            done["ready"].wait(2 * TIMEOUT)
            if isinstance(done["outcome"], GoogleError):
                raise done["outcome"]
            if done["outcome"] is None:
                raise GoogleError("exchange_failed")
            return dict(done["outcome"])
        outcome = None
        try:
            outcome = self._complete(entry, p)
        except GoogleError as e:
            outcome = e
            raise
        finally:
            self._remember(mine, outcome, entry)
        return dict(outcome)

    def _lone_pick(self) -> dict | None:
        """Google's file chooser may answer without state: accept it only while the fresh choices waiting
        are all for one target (the newest is used; the others go)."""
        now = self.clock()
        picks = [e for e in self._pending.values() if e["purpose"] == "pick" and now - e["created"] < LONE_PICK_SECONDS]
        if not picks or len({e["target"] for e in picks}) != 1:
            return None
        for e in picks:
            del self._pending[e["state"]]
        return picks[-1]

    def _remember(self, done: dict, outcome, entry: dict) -> None:
        """Keep how an answer went (None: it failed unexpectedly) and tell anyone waiting on a repeat."""
        with self._lock:
            done["outcome"] = outcome
        done["ready"].set()
        if outcome is None or (entry["purpose"] == "pick" and not isinstance(outcome, GoogleError)):
            return  # a choice that came back: the server says how it went once it has checked the file
        was = None
        if isinstance(outcome, GoogleError):
            message = outcome.message
        else:
            who = self.status()
            new = who["email"] or who["name"] or "your account"
            was = entry.get("was")
            message = (MESSAGES["other_account"].format(new=new, old=was) if was
                       else f"You're signed in to Google as {new}.")
        self.last = {"ok": not isinstance(outcome, GoogleError), "message": message, "at": utc_stamp(),
                     "purpose": entry["purpose"], "target": entry["target"],
                     "keep": bool(was)}  # the account changed: the page keeps this on screen until it's closed

    def _complete(self, entry: dict, p: dict) -> dict:
        if p.get("error"):
            raise GoogleError("denied")
        if not p.get("code"):
            raise GoogleError("exchange_failed")
        client_id, secret = self._need_app()
        try:
            got = self._send("POST", self.token_url, form={
                "client_id": client_id, "client_secret": secret, "code": p["code"],
                "code_verifier": entry["verifier"], "grant_type": "authorization_code",
                "redirect_uri": entry["redirect_uri"]})
        except GoogleError as e:
            if e.reason == "invalid_grant":
                raise GoogleError("code_expired") from None
            if e.code in ("offline", "cert"):
                raise
            if e.reason in CLIENT_REFUSED:
                raise self._app_refused() from None
            raise GoogleError("exchange_failed", status=e.status, reason=e.reason) from None
        if SCOPE not in str(got.get("scope") or "").split():
            raise GoogleError("denied")
        if not got.get("access_token"):
            raise GoogleError("exchange_failed")
        pick = entry["purpose"] == "pick"
        try:
            me = _about_of(self._send("GET", self.drive_url + "/about", {"fields": ABOUT_FIELDS},
                                      token=got["access_token"]))
        except GoogleError as e:
            if e.code == "api_off":
                raise
            raise GoogleError(e.code, MESSAGES["pick_failed" if pick else "signin_failed"],
                              offline=e.offline) from None
        with self._token_lock:
            old = self._read_token() or {}
            same = bool(old.get("permission_id")) and old.get("permission_id") == me["permission_id"]
            other = bool(old.get("permission_id")) and not same
            if pick and other and not old.get("signed_out"):  # signed out: choosing a Sheet signs in as well
                raise GoogleError("wrong_account", MESSAGES["wrong_account"].format(
                    new=me["email"] or me["name"] or "another account",
                    old=old.get("email") or old.get("name") or "another account"))
            # whose the Sheets are: the account used before a change of account, until signing in as it again
            home = old.get("before") if isinstance(old.get("before"), dict) else (old if other else None)
            home = home if home and home.get("permission_id") != me["permission_id"] else None
            if home and other and not pick:
                entry["was"] = home.get("email") or home.get("name")
            token = {"refresh_token": got.get("refresh_token") or (old.get("refresh_token") if same else None),
                     "access_token": got["access_token"],
                     "expires_at": time.time() + float(got.get("expires_in") or 3600),
                     "email": me["email"], "name": me["name"], "permission_id": me["permission_id"],
                     "scope": got.get("scope"), "saved_at": utc_stamp()}
            if home:
                token["before"] = {k: home.get(k) for k in ("email", "name", "permission_id")}
            self._write_token(token)
        picked = [i for i in str(p.get("picked_file_ids") or "").split(",") if i] if pick else []
        return {"purpose": entry["purpose"], "target": entry["target"], "picked": picked}

    # ---------------------------------------------------------------- HTTP

    def request(self, method: str, url: str, params: dict | None = None, body: dict | None = None) -> dict:
        """A signed-in call to Google; JSON in and out. Raises only GoogleError."""
        try:
            return self._send(method, url, params, body, token=self.access_token())
        except GoogleError as e:
            if e.status != 401:
                raise
        return self._send(method, url, params, body, token=self.access_token(force=True))

    def _send(self, method: str, url: str, params: dict | None = None, body: dict | None = None,
              form: dict | None = None, token: str | None = None) -> dict:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers, data = {"Accept": "application/json"}, None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            try:
                data = json.dumps(body, allow_nan=False).encode("utf-8")
            except ValueError:  # "inf" or "nan": Google refuses JSON that has them
                raise GoogleError("other", MESSAGES["bad_number"]) from None
            headers["Content-Type"] = "application/json; charset=utf-8"
        if token:
            headers["Authorization"] = "Bearer " + token
        write = method in ("POST", "PUT") and url.startswith(self.sheets_url)
        quota_tries = other_tries = 0
        while True:
            if write:
                self._limiter.wait()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(req, timeout=TIMEOUT) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:  # before URLError: it is one
                try:
                    answer = e.read()
                except (OSError, http.client.HTTPException):
                    answer = b""
                err, wait = _http_error(e.code, answer), _retry_after(e.headers)
                if err.code == "quota" and quota_tries < len(QUOTA_WAITS):
                    self.sleep(QUOTA_WAITS[quota_tries] if wait is None else wait)
                    quota_tries += 1
                    continue
                if e.code in RETRY_STATUSES and other_tries < len(RETRY_WAITS):
                    self.sleep(RETRY_WAITS[other_tries] if wait is None else wait)
                    other_tries += 1
                    continue
                raise err from None
            except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, dropped connections
                if _is_cert(e):
                    raise GoogleError("cert") from None
                if other_tries < len(RETRY_WAITS):
                    self.sleep(RETRY_WAITS[other_tries])
                    other_tries += 1
                    continue
                raise GoogleError("offline", offline=True) from None
            try:
                return json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (ValueError, UnicodeDecodeError):
                raise GoogleError("other", "Google gave an unexpected answer. Try again in a few minutes.") from None

    # ---------------------------------------------------------------- Drive and Sheets

    def about(self) -> dict:
        """{"email", "name", "permission_id"} of the signed-in Google account."""
        return _about_of(self.request("GET", self.drive_url + "/about", {"fields": ABOUT_FIELDS}))

    def check_file(self, file_id: str) -> dict:
        """Read-only check that Reddit Tracker can write to a Sheet: {"code", "title", "message"}.
        Codes ok, not_found, trashed, not_a_sheet, excel_file, read_only; other problems raise."""
        try:
            f = self.request("GET", f"{self.drive_url}/files/{urllib.parse.quote(file_id, safe='')}",
                             {"fields": FILE_FIELDS, "supportsAllDrives": "true"})
        except GoogleError as e:
            if e.code != "not_found":
                raise
            return {"code": "not_found", "title": None, "message": MESSAGES["not_found"]}
        mime = f.get("mimeType") or ""
        if f.get("trashed"):
            code = "trashed"
        elif mime in EXCEL_MIMES:
            code = "excel_file"
        elif mime != SHEET_MIME:
            code = "not_a_sheet"
        elif not (f.get("capabilities") or {}).get("canEdit"):
            code = "read_only"
        else:
            code = "ok"
        return {"code": code, "title": f.get("name"), "message": MESSAGES.get(code)}

    def create_spreadsheet(self, title: str, tabs: list[tuple[str, list[str]]]) -> dict:
        """A new Sheet with our tabs (no locale-named "Sheet1"), each with a bold, frozen header row."""
        try:
            made = self.request("POST", self.sheets_url, body={
                "properties": {"title": title},
                "sheets": [{"properties": {"title": name, "gridProperties": {"rowCount": 2,
                                                                             "columnCount": len(header)}}}
                           for name, header in tabs]})
        except GoogleError as e:
            if e.code != "storage_full":
                raise
            raise GoogleError("drive_full", status=e.status, reason=e.reason) from None
        sheet_id = made["spreadsheetId"]
        requests = []
        for s in made.get("sheets") or []:
            tab = s["properties"]["sheetId"]
            requests += [
                {"repeatCell": {"range": {"sheetId": tab, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                "fields": "userEnteredFormat.textFormat.bold"}},
                {"updateSheetProperties": {"properties": {"sheetId": tab, "gridProperties": {"frozenRowCount": 1}},
                                           "fields": "gridProperties.frozenRowCount"}}]
        try:  # only looks: the Sheet exists now (and gets its headers on its first update anyway)
            if requests:
                self.batch_update(sheet_id, requests)
            for name, header in tabs:
                self.values_update(sheet_id, f"'{name}'!A1", [list(header)])
        except GoogleError as e:
            self.log(f"The new Sheet was made, but its header rows weren't set up: {e.message}")
        return {"id": sheet_id, "title": (made.get("properties") or {}).get("title") or title,
                "url": made.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"}

    def _sheet(self, sheet_id: str, tail: str = "") -> str:
        return f"{self.sheets_url}/{urllib.parse.quote(sheet_id, safe='')}{tail}"

    def get_meta(self, sheet_id: str, fields: str | None = None) -> dict:
        return self.request("GET", self._sheet(sheet_id), {"fields": fields or META_FIELDS})

    def values_get(self, sheet_id: str, a1: str, params: dict | None = None) -> dict:
        return self.request("GET", self._sheet(sheet_id, "/values/" + urllib.parse.quote(a1, safe="")), params)

    def values_update(self, sheet_id: str, a1: str, values: list[list]) -> dict:
        return self.request("PUT", self._sheet(sheet_id, "/values/" + urllib.parse.quote(a1, safe="")),
                            {"valueInputOption": "RAW"}, {"values": values})

    def values_batch_clear(self, sheet_id: str, ranges: list[str]) -> dict:
        return self.request("POST", self._sheet(sheet_id, "/values:batchClear"), body={"ranges": list(ranges)})

    def batch_update(self, sheet_id: str, requests: list[dict]) -> dict:
        return self.request("POST", self._sheet(sheet_id, ":batchUpdate"), body={"requests": list(requests)})
