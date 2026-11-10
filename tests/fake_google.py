"""A strict, in-memory fake of the Google endpoints Reddit Tracker uses: sign-in (OAuth with PKCE),
Drive (about, files.get with drive.file rules) and Sheets (create, get, values, batchUpdate).

Point the app at it with RT_GOOGLE_BASE=fake.base. It refuses what real Google refuses (a wrong
scope, verifier, redirect address or secret, a reused or old code, a write outside the grid, …) so a
client bug fails here instead of on a customer's computer. Synthetic accounts only.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCOPE = "https://www.googleapis.com/auth/drive.file"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUS_NAMES = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
                429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "BAD_GATEWAY", 503: "UNAVAILABLE"}
DROP = object()  # answer by closing the connection (no internet)


class FakeClock:
    """A clock whose sleep() only moves time on, so retries and rate limits run instantly."""

    def __init__(self, start: float = 1_000_000.0):
        self.t, self.slept = start, []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += max(0.0, seconds)


class Tab:
    def __init__(self, sheet_id: int, title: str, rows: int, cols: int):
        self.id, self.title, self.rows, self.cols = sheet_id, title, rows, cols
        self.cells: dict[tuple[int, int], object] = {}  # (row, col) from 0 -> value
        self.frozen, self.bold_rows, self.charts = 0, set(), []

    def props(self, index: int) -> dict:
        grid = {"rowCount": self.rows, "columnCount": self.cols}
        if self.frozen:
            grid["frozenRowCount"] = self.frozen
        return {"sheetId": self.id, "title": self.title, "index": index, "sheetType": "GRID", "gridProperties": grid}

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.cells = {(r, c): v for (r, c), v in self.cells.items() if r < rows and c < cols}

    def grid(self) -> list[list]:
        return [[self.cells.get((r, c), "") for c in range(self.cols)] for r in range(self.rows)]


class File:
    def __init__(self, file_id: str, name: str, mime: str = SHEET_MIME):
        self.id, self.name, self.mime = file_id, name, mime
        self.trashed, self.can_edit = False, True
        self.granted: set[str] = set()   # permission ids that opened it through the app (made or picked)
        self.tabs: list[Tab] = []
        self.next_tab = 0

    def add_tab(self, title: str, rows: int = 1000, cols: int = 26) -> Tab:
        tab = Tab(self.next_tab, title, rows, cols)
        self.next_tab += 1
        self.tabs.append(tab)
        return tab

    def tab(self, title: str) -> Tab:
        """Like Google: tab names are unique, and found, whatever their case."""
        return next(t for t in self.tabs if t.title.lower() == title.lower())


class ApiError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.status, self.message, self.reason = status, message, reason


class Rule:
    def __init__(self, pattern: str, method: str | None, times: int, status: int | None, reason: str | None,
                 message: str | None, retry_after: str | None, html: bool, reason_in: str, drop: bool,
                 after_apply: bool):
        self.rx, self.method, self.times = re.compile(pattern), method, times
        self.status, self.reason, self.message, self.retry_after = status, reason, message, retry_after
        self.html, self.reason_in, self.drop, self.after_apply = html, reason_in, drop, after_apply


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _a1(text: str) -> tuple[str, str | None]:
    """"'my tab'!A1:B" -> ("my tab", "A1:B")."""
    m = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^'!]+))(?:!(.+))?", text)
    if not m:
        raise ApiError(400, f"Unable to parse range: {text}")
    return (m.group(1).replace("''", "'") if m.group(1) else m.group(2)), m.group(3)


def _end(text: str) -> tuple[int | None, int | None]:
    m = re.fullmatch(r"([A-Z]*)(\d*)", text)
    if not m or not (m.group(1) or m.group(2)):
        raise ApiError(400, f"Unable to parse range: {text}")
    col = None
    if m.group(1):
        col = 0
        for ch in m.group(1):
            col = col * 26 + ord(ch) - 64
        col -= 1
    return (int(m.group(2)) - 1 if m.group(2) else None), col


def _box(tab: Tab, cells: str | None) -> tuple[int, int, int, int]:
    """(first row, first col, last row, last col), inclusive, for A1 cells inside a tab."""
    if cells is None:
        return 0, 0, tab.rows - 1, tab.cols - 1
    a, _, b = cells.partition(":")
    r0, c0 = _end(a)
    r1, c1 = _end(b) if b else (r0, c0)
    return r0 or 0, c0 or 0, tab.rows - 1 if r1 is None else r1, tab.cols - 1 if c1 is None else c1


def _formatted(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class FakeGoogle:
    """Start with `with FakeGoogle() as fake:`; fake.base is the address to give RT_GOOGLE_BASE."""

    def __init__(self, clock=time.time, code_max_age: float = 300, write_limit: int | None = None,
                 cell_limit: int = 10_000_000):
        self.clock, self.code_max_age, self.write_limit, self.cell_limit = clock, code_max_age, write_limit, cell_limit
        self.client_id, self.client_secret = "test-client.apps.googleusercontent.com", "test-secret"
        self.accounts = {"jane@example.com": {"permissionId": "perm-jane", "name": "Acme Jane"},
                         "bolt@example.com": {"permissionId": "perm-bolt", "name": "Bolt Bikes"}}
        self.account = "jane@example.com"           # who clicks Allow on the sign-in page
        self.deny = self.omit_state = self.refresh_invalid = self.no_refresh_token = False
        self.offline = False
        self.grant_scope = SCOPE                    # what the user leaves ticked on the consent screen
        self.pick_ids: list[str] = []               # what the user picks in the file chooser
        self.last_auth: dict = {}
        self.files: dict[str, File] = {}
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, tuple[str, str]] = {}  # token -> (account, client that got it)
        self.access_tokens: dict[str, dict] = {}
        self.calls: list[tuple] = []                # (method, path, query, body)
        self.refreshes = self.revokes = self.rate_limited = 0
        self.held = threading.Event()
        self._writes: list[float] = []
        self._rules: list[Rule] = []
        self._holds: list[tuple[re.Pattern, threading.Event]] = []
        self.lock = threading.RLock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake._handle(self, "GET")

            def do_POST(self):
                fake._handle(self, "POST")

            def do_PUT(self):
                fake._handle(self, "PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                        daemon=True)

    def __enter__(self) -> FakeGoogle:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        for _, gate in self._holds:
            gate.set()
        self.server.shutdown()
        self.server.server_close()

    # ---------------------------------------------------------------- helpers for tests

    def write_app_file(self, folder: Path) -> Path:
        path = Path(folder) / "google_app.json"
        path.write_text(json.dumps({"installed": {"client_id": self.client_id, "client_secret": self.client_secret,
                                                  "redirect_uris": ["http://localhost"]}}), encoding="utf-8")
        return path

    def approve(self, url: str) -> dict:
        """Open a sign-in address like a browser and return the parameters Google sends back."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
        try:
            opener.open(url, timeout=10)
        except urllib.error.HTTPError as e:
            if e.code != 302:
                raise AssertionError(f"Google refused the sign-in address: {e.code} {e.read()!r}") from None
            return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(e.headers["Location"]).query))
        raise AssertionError("Google didn't redirect")

    def callback_url(self, url: str) -> str:
        """The full address the browser lands on after Allow (what a user would paste)."""
        params = self.approve(url)
        redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["redirect_uri"]
        return redirect + "?" + urllib.parse.urlencode(params)

    def sign_in(self, google, purpose: str = "signin", **kw) -> dict:
        return google.finish(self.approve(google.begin(purpose, **kw)["url"]))

    def client(self, folder: Path, clock: FakeClock | None = None, sign_in: bool = True, log=None):
        """An rt.google.Google pointed at this fake (made while RT_GOOGLE_BASE is set), signed in unless told not to."""
        from rt.google import Google
        clock = clock or FakeClock()
        with mock.patch.dict(os.environ, {"RT_GOOGLE_BASE": self.base}):
            google = Google(Path(folder), self.write_app_file(folder), clock=clock.now, sleep=clock.sleep, log=log)
        if sign_in:
            self.sign_in(google)
        return google

    def make_sheet(self, title: str = "Fake Sheet", tabs: dict | None = None, granted: bool = True,
                   file_id: str | None = None) -> str:
        """A spreadsheet with {tab: (rows, cols)}; granted means this app may open it (made or picked)."""
        f = File(file_id or "S" + secrets.token_hex(15), title)
        for name, (rows, cols) in (tabs if tabs is not None else {"Sheet1": (1000, 26)}).items():
            f.add_tab(name, rows, cols)
        if granted:
            f.granted.add(self.accounts[self.account]["permissionId"])
        self.files[f.id] = f
        return f.id

    def add_file(self, name: str, mime: str, granted: bool = True) -> str:
        f = File("F" + secrets.token_hex(15), name, mime)
        if granted:
            f.granted.add(self.accounts[self.account]["permissionId"])
        self.files[f.id] = f
        return f.id

    def set_rows(self, sheet_id: str, tab: str, rows: list[list]) -> None:
        t = self.files[sheet_id].tab(tab)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                t.cells[(r, c)] = v

    def values(self, sheet_id: str, tab: str) -> list[list]:
        """The whole grid, empty cells as ""."""
        return self.files[sheet_id].tab(tab).grid()

    def data(self, sheet_id: str, tab: str) -> list[list]:
        """Only the rows that hold something."""
        return [r for r in self.values(sheet_id, tab) if any(v != "" for v in r)]

    def grid(self, sheet_id: str, tab: str) -> tuple[int, int]:
        t = self.files[sheet_id].tab(tab)
        return t.rows, t.cols

    def tab_names(self, sheet_id: str) -> list[str]:
        return [t.title for t in self.files[sheet_id].tabs]

    def writes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("POST", "PUT") and c[1].startswith("/v4/spreadsheets")]

    def batch_requests(self) -> list[list[str]]:
        """For every batchUpdate call: the kinds of its requests, e.g. ["updateSheetProperties"]."""
        return [[next(iter(r)) for r in c[3]["requests"]] for c in self.calls if c[1].endswith(":batchUpdate")]

    def expire_access_tokens(self) -> None:
        with self.lock:
            for info in self.access_tokens.values():
                info["expires"] = 0

    def fail(self, pattern: str, status: int | None = None, times: int = 1, method: str | None = None,
             reason: str | None = None, message: str | None = None, retry_after: str | None = None,
             html: bool = False, reason_in: str = "both", drop: bool = False, after_apply: bool = False) -> None:
        """The next `times` requests whose path matches `pattern` get this answer. drop: close the
        connection instead; after_apply: do the request, then drop the answer (a lost reply)."""
        with self.lock:
            self._rules.append(Rule(pattern, method, times, status, reason, message, retry_after, html, reason_in,
                                    drop, after_apply))

    def hold(self, pattern: str) -> threading.Event:
        """Requests whose path matches wait until the returned gate is set (fake.held tells one waits)."""
        gate = threading.Event()
        self.held.clear()
        self._holds.append((re.compile(pattern), gate))
        return gate

    # ---------------------------------------------------------------- HTTP plumbing

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        raw_path, _, query = h.path.partition("?")
        path = urllib.parse.unquote(raw_path)
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        size = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(size) if size else b""
        try:
            body = json.loads(raw) if raw and "json" in (h.headers.get("Content-Type") or "") else None
        except ValueError:
            body = None
        with self.lock:
            self.calls.append((method, path, params, body))
            rule = next((r for r in self._rules if r.times > 0 and (r.method in (None, method))
                         and r.rx.search(path)), None)
            if rule:
                rule.times -= 1
        for rx, gate in list(self._holds):
            if rx.search(path) and not gate.is_set():
                self.held.set()
                gate.wait(10)
        if self.offline or (rule and rule.drop):
            return
        if rule and not rule.after_apply:
            return self._send(h, *self._injected(rule))
        try:
            with self.lock:
                answer = self._route(method, path, params, raw, body, h.headers)
        except ApiError as e:
            answer = self._error(e.status, e.message, e.reason)
        except Exception as e:  # a bug in this fake: say so instead of looking like a dropped connection
            answer = self._error(500, f"fake_google failed: {e!r}")
        if rule and rule.after_apply:
            return
        self._send(h, *answer)

    def _send(self, h: BaseHTTPRequestHandler, status: int, payload, headers: dict | None = None) -> None:
        if isinstance(payload, bytes):
            data, ctype = payload, "text/html; charset=utf-8"
        else:
            data, ctype = json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8"
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    @staticmethod
    def _error(status: int, message: str, reason: str | None = None, reason_in: str = "both",
               headers: dict | None = None) -> tuple:
        err = {"code": status, "message": message, "status": STATUS_NAMES.get(status, "UNKNOWN")}
        if reason and reason_in in ("both", "errors"):
            err["errors"] = [{"message": message, "domain": "global", "reason": reason}]
        if reason and reason_in in ("both", "details"):
            err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
        return status, {"error": err}, headers or {}

    def _injected(self, rule: Rule) -> tuple:
        headers = {"Retry-After": rule.retry_after} if rule.retry_after else {}
        if rule.html:
            page = f"<!DOCTYPE html><html><title>Error {rule.status} (Server Error)!!1</title></html>"
            return rule.status, page.encode("utf-8"), headers
        return self._error(rule.status, rule.message or "Injected failure", rule.reason, rule.reason_in, headers)

    def _account(self, headers) -> str:
        token = (headers.get("Authorization") or "").partition("Bearer ")[2]
        info = self.access_tokens.get(token)
        if not info or info["expires"] <= self.clock():
            raise ApiError(401, "Request had invalid authentication credentials.", "authError")
        return info["account"]

    def _issue(self, account: str) -> str:
        token = "ya29." + secrets.token_urlsafe(24)
        self.access_tokens[token] = {"account": account, "expires": self.clock() + 3599}
        return token

    def _route(self, method: str, path: str, params: dict, raw: bytes, body, headers) -> tuple:
        if path == "/o/oauth2/v2/auth" and method == "GET":
            return self._auth(params)
        if path == "/token" and method == "POST":
            return self._token(dict(urllib.parse.parse_qsl(raw.decode("ascii"))), headers)
        if path == "/revoke" and method == "POST":
            self.revokes += 1
            return 200, {}, {}
        if path.startswith("/drive/v3/"):
            return self._drive(method, path, params, self._account(headers))
        if path.startswith("/v4/spreadsheets"):
            account = self._account(headers)
            if method in ("POST", "PUT"):
                self._count_write()
            return self._sheets(method, path, params, body, account)
        raise ApiError(404, "Not found")

    def _count_write(self) -> None:
        if self.write_limit is None:
            return
        now = self.clock()
        self._writes = [t for t in self._writes if now - t < 60]
        if len(self._writes) >= self.write_limit:
            self.rate_limited += 1
            raise ApiError(429, "Quota exceeded for quota metric 'Write requests' and limit 'Write requests per "
                                "minute per user'.", "RATE_LIMIT_EXCEEDED")
        self._writes.append(now)

    # ---------------------------------------------------------------- sign-in

    def _auth(self, p: dict) -> tuple:
        redirect = p.get("redirect_uri", "")
        if p.get("client_id") != self.client_id:
            return 400, {"error": "invalid_client"}, {}
        if not re.fullmatch(r"http://(127\.0\.0\.1|\[::1\]|localhost):\d+(/[^?#]*)?", redirect):
            return 400, {"error": "redirect_uri_mismatch"}, {}
        if p.get("response_type") != "code":
            return 400, {"error": "unsupported_response_type"}, {}
        if p.get("scope") != SCOPE:
            return 400, {"error": "invalid_scope"}, {}
        if p.get("code_challenge_method") != "S256" or not p.get("code_challenge"):
            return 400, {"error": "invalid_request", "error_description": "PKCE (S256) is required."}, {}
        self.last_auth = dict(p)
        answer = {}
        if self.deny:
            answer["error"] = "access_denied"
        else:
            code = "4/" + secrets.token_urlsafe(20)
            picked = list(self.pick_ids) if p.get("trigger_onepick") == "true" else []
            perm = self.accounts[self.account]["permissionId"]
            for file_id in picked:
                if file_id in self.files:
                    self.files[file_id].granted.add(perm)
            self.codes[code] = {"challenge": p["code_challenge"], "redirect_uri": redirect, "account": self.account,
                                "created": self.clock(), "used": False, "scope": self.grant_scope,
                                "offline": p.get("access_type") == "offline"}
            answer.update(code=code, scope=self.grant_scope)
            if picked:
                answer["picked_file_ids"] = ",".join(picked)
        if p.get("state") and not self.omit_state:
            answer["state"] = p["state"]
        return 302, b"", {"Location": redirect + "?" + urllib.parse.urlencode(answer)}

    def _token(self, form: dict, headers) -> tuple:
        def bad(error: str, status: int = 400, said: str = "") -> tuple:
            return status, {"error": error, "error_description": said or error}, {}
        if "x-www-form-urlencoded" not in (headers.get("Content-Type") or ""):
            return bad("invalid_request", said="Form body expected.")
        if form.get("client_id") != self.client_id:
            return bad("invalid_client", 401, "The OAuth client was not found.")
        if "client_secret" not in form:
            return bad("invalid_request", said="client_secret is missing.")
        if form["client_secret"] != self.client_secret:
            return bad("invalid_client", 401, "Unauthorized")
        kind = form.get("grant_type")
        if kind == "authorization_code":
            code = self.codes.get(form.get("code", ""))
            if not code or code["used"] or self.clock() - code["created"] > self.code_max_age:
                return bad("invalid_grant", said="Bad Request")
            code["used"] = True
            if form.get("redirect_uri") != code["redirect_uri"]:
                return bad("redirect_uri_mismatch")
            if _s256(form.get("code_verifier", "")) != code["challenge"]:
                return bad("invalid_grant", said="Invalid code verifier.")
            answer = {"access_token": self._issue(code["account"]), "expires_in": 3599, "token_type": "Bearer",
                      "scope": code["scope"]}
            if code["offline"] and not self.no_refresh_token:  # access_type=online never gets one
                refresh = "1//" + secrets.token_urlsafe(24)
                self.refresh_tokens[refresh] = (code["account"], self.client_id)
                answer["refresh_token"] = refresh
            return 200, answer, {}
        if kind == "refresh_token":
            account, client = self.refresh_tokens.get(form.get("refresh_token", ""), (None, None))
            if self.refresh_invalid or not account:
                return bad("invalid_grant", said="Token has been expired or revoked.")
            if client != self.client_id:  # made for another OAuth client (an older google_app.json)
                return bad("unauthorized_client", 401, "Unauthorized")
            self.refreshes += 1
            return 200, {"access_token": self._issue(account), "expires_in": 3599, "token_type": "Bearer",
                         "scope": SCOPE}, {}
        return bad("unsupported_grant_type")

    # ---------------------------------------------------------------- Drive

    def _visible(self, file_id: str, account: str) -> File:
        f = self.files.get(file_id)
        if not f or self.accounts[account]["permissionId"] not in f.granted:
            raise ApiError(404, f"File not found: {file_id}.", "notFound")
        return f

    def _drive(self, method: str, path: str, params: dict, account: str) -> tuple:
        if path == "/drive/v3/about" and method == "GET":
            if not params.get("fields"):
                raise ApiError(400, "The 'fields' parameter is required for this operation.", "required")
            info = self.accounts[account]
            return 200, {"user": {"kind": "drive#user", "displayName": info["name"], "emailAddress": account,
                                  "permissionId": info["permissionId"], "me": True}}, {}
        m = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
        if m and method == "GET":
            f = self._visible(m.group(1), account)
            return 200, {"name": f.name, "mimeType": f.mime, "trashed": f.trashed,
                         "capabilities": {"canEdit": f.can_edit}}, {}
        raise ApiError(404, "Not found")

    # ---------------------------------------------------------------- Sheets

    def _sheet(self, file_id: str, account: str, write: bool) -> File:
        f = self.files.get(file_id)
        if not f:
            raise ApiError(404, "Requested entity was not found.")
        if self.accounts[account]["permissionId"] not in f.granted or (write and not f.can_edit):
            raise ApiError(403, "The caller does not have permission")
        if f.mime != SHEET_MIME:
            raise ApiError(400, "This operation is not supported for this document")
        return f

    def _resource(self, f: File, fields: str = "") -> dict:
        sheets = []
        for i, t in enumerate(f.tabs):
            s = {"properties": t.props(i)}
            if t.charts and (not fields or "charts" in fields):
                s["charts"] = [{"chartId": c} for c in t.charts]
            sheets.append(s)
        return {"spreadsheetId": f.id, "properties": {"title": f.name, "locale": "en_US"}, "sheets": sheets,
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{f.id}/edit"}

    def _check_cells(self, tabs: list[Tab]) -> None:
        if sum(t.rows * t.cols for t in tabs) > self.cell_limit:
            raise ApiError(400, "This action would increase the number of cells in the workbook above the limit "
                                f"of {self.cell_limit} cells.")

    def _sheets(self, method: str, path: str, params: dict, body, account: str) -> tuple:
        if path == "/v4/spreadsheets" and method == "POST":
            return self._create(body or {}, account)
        m = re.fullmatch(r"/v4/spreadsheets/([^/:]+)(:batchUpdate|/values:batchClear|/values/(.+))?", path)
        if not m:
            raise ApiError(404, "Not found")
        f = self._sheet(m.group(1), account, write=method != "GET")
        if m.group(2) is None and method == "GET":
            return 200, self._resource(f, params.get("fields", "")), {}
        if m.group(2) == ":batchUpdate" and method == "POST":
            return self._batch_update(f, (body or {}).get("requests") or [])
        if m.group(2) == "/values:batchClear" and method == "POST":
            for a1 in (body or {}).get("ranges") or []:
                tab, r0, c0, r1, c1 = self._range(f, a1)
                if r1 >= tab.rows or c1 >= tab.cols:
                    raise ApiError(400, f"Range ({a1}) exceeds grid limits. Max rows: {tab.rows}, max columns: "
                                        f"{tab.cols}")
                for key in [k for k in tab.cells if r0 <= k[0] <= r1 and c0 <= k[1] <= c1]:
                    del tab.cells[key]
            return 200, {"spreadsheetId": f.id, "clearedRanges": (body or {}).get("ranges")}, {}
        if m.group(3) and method == "GET":
            return 200, self._values_get(f, m.group(3), params), {}
        if m.group(3) and method == "PUT":
            return 200, self._values_put(f, m.group(3), params, body or {}), {}
        raise ApiError(404, "Not found")

    def _range(self, f: File, a1: str) -> tuple:
        title, cells = _a1(a1)
        tab = next((t for t in f.tabs if t.title.lower() == title.lower()), None)  # any case, like Google
        if tab is None:
            raise ApiError(400, f"Unable to parse range: {a1}")
        return (tab, *_box(tab, cells))

    def _create(self, body: dict, account: str) -> tuple:
        f = File("1" + secrets.token_urlsafe(32).replace("-", "a").replace("_", "b"),
                 (body.get("properties") or {}).get("title") or "Untitled spreadsheet")
        for s in body.get("sheets") or [{"properties": {"title": "Sheet1"}}]:
            p = s.get("properties") or {}
            grid = p.get("gridProperties") or {}
            f.add_tab(p.get("title") or f"Sheet{len(f.tabs) + 1}", grid.get("rowCount", 1000),
                      grid.get("columnCount", 26))
        self._check_cells(f.tabs)
        f.granted.add(self.accounts[account]["permissionId"])
        self.files[f.id] = f
        return 200, self._resource(f), {}

    def _values_get(self, f: File, a1: str, params: dict) -> dict:
        tab, r0, c0, r1, c1 = self._range(f, a1)
        raw = params.get("valueRenderOption") == "UNFORMATTED_VALUE"
        inside = [k for k, v in tab.cells.items() if r0 <= k[0] <= r1 and c0 <= k[1] <= c1 and v != ""]
        last_r, last_c = max((k[0] for k in inside), default=r0 - 1), max((k[1] for k in inside), default=c0 - 1)
        rows = [[tab.cells.get((r, c), "") for c in range(c0, last_c + 1)] for r in range(r0, last_r + 1)]
        if params.get("majorDimension") == "COLUMNS":
            rows = [list(col) for col in zip(*rows)] if rows else []
        values = []
        for row in rows:
            row = [v if raw else _formatted(v) for v in row]
            while row and row[-1] == "":
                row.pop()
            values.append(row)
        while values and not values[-1]:
            values.pop()
        answer = {"range": a1, "majorDimension": params.get("majorDimension", "ROWS")}
        if values:
            answer["values"] = values
        return answer

    def _values_put(self, f: File, a1: str, params: dict, body: dict) -> dict:
        option = params.get("valueInputOption")
        if option not in ("RAW", "USER_ENTERED"):
            raise ApiError(400, "'valueInputOption' is required but not specified")
        tab, r0, c0, _, _ = self._range(f, a1)
        values = body.get("values") or []
        width = max((len(r) for r in values), default=0)
        if r0 >= tab.rows or c0 >= tab.cols or r0 + len(values) > tab.rows or c0 + width > tab.cols:
            raise ApiError(400, f"Range ({a1}) exceeds grid limits. Max rows: {tab.rows}, max columns: {tab.cols}")
        for r, row in enumerate(values):
            for c, v in enumerate(row):
                if isinstance(v, str) and len(v) > 50_000:
                    raise ApiError(400, "Your input contains more than the maximum of 50000 characters in a "
                                        "single cell.")
                if v is not None:  # like Google: null leaves the cell as it is
                    tab.cells[(r0 + r, c0 + c)] = v
        return {"spreadsheetId": f.id, "updatedRange": a1, "updatedRows": len(values), "updatedColumns": width,
                "updatedCells": sum(len(r) for r in values)}

    def _batch_update(self, f: File, requests: list) -> tuple:
        """All or nothing, like Google: work on a copy and keep it only if every request is valid."""
        tabs, next_tab = copy.deepcopy(f.tabs), f.next_tab
        replies = []
        for i, req in enumerate(requests):
            kind, args = next(iter(req.items())) if len(req) == 1 else (None, None)
            by_id = {t.id: t for t in tabs}
            if kind == "addSheet":
                p = args.get("properties") or {}
                title = p.get("title") or f"Sheet{len(tabs) + 1}"
                if any(t.title.lower() == title.lower() for t in tabs):
                    raise ApiError(400, f'Invalid requests[{i}].addSheet: A sheet with the name "{title}" already '
                                        "exists. Please enter another name.")
                grid = p.get("gridProperties") or {}
                tab = Tab(next_tab, title, grid.get("rowCount", 1000), grid.get("columnCount", 26))
                next_tab += 1
                tabs.append(tab)
                replies.append({"addSheet": {"properties": tab.props(len(tabs) - 1)}})
            elif kind == "deleteSheet":
                if args.get("sheetId") not in by_id:
                    raise ApiError(400, f"Invalid requests[{i}].deleteSheet: No grid with id: {args.get('sheetId')}")
                if len(tabs) == 1:
                    raise ApiError(400, f"Invalid requests[{i}].deleteSheet: You can't remove all the sheets in a "
                                        "document.")
                tabs.remove(by_id[args["sheetId"]])
                replies.append({})
            elif kind == "updateSheetProperties":
                self._update_props(i, by_id, args)
                replies.append({})
            elif kind == "repeatCell":
                rng = args.get("range") or {}
                if rng.get("sheetId") not in by_id or args.get("fields") != "userEnteredFormat.textFormat.bold":
                    raise ApiError(400, f"Invalid requests[{i}].repeatCell")
                tab = by_id[rng["sheetId"]]
                bold = ((args.get("cell") or {}).get("userEnteredFormat") or {}).get("textFormat", {}).get("bold")
                for r in range(rng.get("startRowIndex", 0), rng.get("endRowIndex", tab.rows)):
                    (tab.bold_rows.add if bold else tab.bold_rows.discard)(r)
                replies.append({})
            else:
                raise ApiError(400, f"Invalid requests[{i}]: Unknown request.")
        self._check_cells(tabs)
        f.tabs, f.next_tab = tabs, next_tab
        return 200, {"spreadsheetId": f.id, "replies": replies}, {}

    @staticmethod
    def _update_props(i: int, by_id: dict, args: dict) -> None:
        p = args.get("properties") or {}
        tab = by_id.get(p.get("sheetId"))
        if tab is None:
            raise ApiError(400, f"Invalid requests[{i}].updateSheetProperties: No grid with id: {p.get('sheetId')}")
        grid = p.get("gridProperties") or {}
        for field in (args.get("fields") or "").replace("/", ".").split(","):
            if field == "gridProperties.rowCount":
                tab.resize(grid["rowCount"], tab.cols)
            elif field == "gridProperties.columnCount":
                tab.resize(tab.rows, grid["columnCount"])
            elif field == "gridProperties.frozenRowCount":
                tab.frozen = grid.get("frozenRowCount", 0)
            elif field == "title":
                tab.title = p["title"]
            else:
                raise ApiError(400, f"Invalid requests[{i}].updateSheetProperties: bad field mask {field!r}")
        if tab.rows < 1 or tab.cols < 1:
            raise ApiError(400, f"Invalid requests[{i}].updateSheetProperties: the grid must keep a cell")
        if tab.frozen and tab.rows <= tab.frozen:
            raise ApiError(400, f"Invalid requests[{i}].updateSheetProperties: You can't freeze all visible rows "
                                "on the sheet.")
