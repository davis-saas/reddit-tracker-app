"""The server: who may use it, every route, the /api/status contract and the Sheet cards, against the
strict fake Google. Synthetic clients only (Acme Jane / acme_jane, Bolt Bikes / bolt_bikes)."""
from __future__ import annotations

import ast
import contextlib
import errno
import http.client
import io
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _folder in (HERE.parent, HERE):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))

import builders as b  # noqa: E402
from fake_google import FakeClock, FakeGoogle  # noqa: E402
from rt import VERSION, engine, paths, server, store  # noqa: E402
from rt.google import GoogleError  # noqa: E402
from rt.server import CANT_SAVE, REPORT_TEMPLATE, App, create, sheet_card  # noqa: E402

POSTS = ("acme_jane Post Insights Report.csv", b.POSTS_CSV)
COMMENTS = ("acme_jane Comment Insights Report.csv", b.COMMENTS_CSV)
ACCOUNT = ("acme_jane Account Performance Report.csv", b.ACCOUNT_CSV)
BOLT_POSTS = ("bolt_bikes Post Insights Report.csv", b.report("post", b.post_rows(4, prefix="B", sub="r/bikes")))
SECURITY = ("X-Frame-Options", "Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "Cache-Control")


class NoTimer:
    """The Sheets job's 5-minute retry timer, recorded but never fired."""
    made: list = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.daemon = seconds, fn, True
        NoTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


class Answer:
    def __init__(self, status: int, body, headers):
        self.status, self.body, self.headers = status, body, headers

    def __repr__(self):
        return f"<{self.status} {self.body!r:.300}>"


class Base(unittest.TestCase):
    network = False

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.clock = FakeClock()
        self.fake = FakeGoogle(clock=self.clock.now).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.google = self.fake.client(self.data, self.clock, sign_in=False)
        self.app = App(self.data, network=self.network, google=self.google, timer=NoTimer, echo=False)
        self.addCleanup(self.app.close)
        self.srv = create(self.app, 0, host="127.0.0.1")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.wait_sheets)
        self.host = f"127.0.0.1:{self.port}"

    # ---------------------------------------------------------------- requests

    def call(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None) -> Answer:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": self.host}
        if method == "POST":
            h.update({"Content-Type": "application/json", "X-RT": "1", "Origin": f"http://{self.host}"})
        h.update(headers or {})
        if raw is None and (body is not None or method == "POST"):
            raw = json.dumps({} if body is None else body).encode("utf-8")
        c.request(method, path, body=raw, headers={k: v for k, v in h.items() if v is not None})
        r = c.getresponse()
        payload = r.read()
        c.close()
        kind = r.getheader("Content-Type") or ""
        return Answer(r.status, json.loads(payload) if "json" in kind else payload.decode("utf-8", "replace"),
                      r.headers)

    def get(self, path: str, **kw) -> Answer:
        return self.call("GET", path, **kw)

    def post(self, path: str, body=None, **kw) -> Answer:
        return self.call("POST", path, body, **kw)

    def ok(self, path: str, body=None, **kw) -> dict:
        r = self.post(path, body, **kw)
        self.assertEqual(r.status, 200, r)
        return r.body

    def status(self) -> dict:
        r = self.get("/api/status")
        self.assertEqual(r.status, 200, r)
        return r.body

    # ---------------------------------------------------------------- steps

    def sign_in(self, purpose="signin", **body) -> Answer:
        url = self.ok("/api/google/begin", {"purpose": purpose, **body})["url"]
        return self.get("/oauth?" + urllib.parse.urlencode(self.fake.approve(url)))

    def add(self, username: str, name: str | None = None) -> dict:
        return self.ok("/api/clients/add", {"rev": self.status()["rev"], "username": username, "name": name})["client"]

    def files(self, *named) -> list[dict]:
        return [b.dropped(f"f{i}", name, text) for i, (name, text) in enumerate(named)]

    def drop(self, *named) -> dict:
        """Preview, then import what the preview showed as ready (like the page)."""
        files = self.files(*named)
        plan = self.ok("/api/files/preview", {"files": files})
        ready = [{**f, "expect": p["client"]} for f, p in zip(files, plan["files"]) if p["import"]]
        return self.ok("/api/files/import", {"files": ready})

    def wait_sheets(self) -> dict:
        for _ in range(1000):
            state = self.app.job.state()
            if not state["running"]:
                return state
            time.sleep(0.01)
        self.fail("The Sheets update didn't finish")

    def tables(self, users) -> dict:
        with self.app.read_tables(users) as tables:
            return {tab: list(rows) for tab, rows in tables.items()}

    def assert_sheet(self, sheet_id: str, users) -> None:
        for tab, rows in self.tables(users).items():
            self.assertEqual(self.fake.data(sheet_id, tab), [["" if v is None else v for v in r] for r in rows], tab)

    def client(self, cid: str) -> dict:
        return next(c for c in self.status()["clients"] if c["id"] == cid)


class GuardTests(Base):
    def test_lan_peer_with_a_loopback_host_is_refused(self):
        with mock.patch.object(server, "peer_is_loopback", return_value=False):
            r = self.get("/api/status")
            self.assertEqual((r.status, r.body["code"]), (403, "need_link"))
            page = self.get("/")
            self.assertEqual(page.status, 403)
            self.assertIn("only answers on this computer", page.body)
            self.assertEqual(self.post("/api/clients/add", {"username": "acme_jane"}).status, 403)
        self.assertEqual(self.get("/api/status").status, 200)

    def test_loopback_peer_with_a_foreign_host_is_refused(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}",
                     f"pi.local:{self.port}"):
            r = self.get("/api/status", headers={"Host": host})
            self.assertEqual((r.status, r.body["code"]), (403, "need_link"), host)
            self.assertEqual(self.post("/api/sheets/update", headers={"Host": host}).status, 403, host)
        for host in (f"localhost:{self.port}", f"[::1]:{self.port}"):
            self.assertEqual(self.get("/api/status", headers={"Host": host}).status, 200, host)

    def test_posts_only_from_the_page_itself(self):
        good = {"rev": "", "username": "acme_jane"}
        self.assertEqual(self.post("/api/clients/add", good, headers={"X-RT": None}).status, 403)
        self.assertEqual(self.post("/api/clients/add", good, headers={"X-RT": "yes"}).status, 403)
        self.assertEqual(self.post("/api/clients/add", good, headers={"Content-Type": "text/plain"}).status, 403)
        for origin in ("https://attacker.example", "null", f"http://localhost:{self.port}"):
            self.assertEqual(self.post("/api/clients/add", good, headers={"Origin": origin}).status, 403, origin)
        self.assertEqual(self.post("/api/clients/add", raw=b"not json").status, 400)
        self.assertEqual(self.post("/api/clients/add", raw=b"[1, 2]").status, 400)
        self.assertEqual(self.post("/api/clients/add", raw="{\"a\": \"\xff\"}".encode("latin-1")).status, 400)
        self.assertEqual(self.post("/api/clients/add", raw=b"[" * 100_000 + b"]" * 100_000).status, 400)  # too deep
        # no Origin (some browsers leave it out on same-origin requests) is fine
        self.assertEqual(self.post("/api/clients/add", good, headers={"Origin": None}).status, 200)
        self.assertEqual(self.post("/api/nothing-here").status, 404)

    def test_too_big_and_no_length(self):
        r = self.post("/api/files/preview", raw=b" " * (server.MAX_BODY + 1))
        self.assertEqual((r.status, r.body["error"]), (413, "These files are too big to send at once. Drop fewer "
                                                              "files at a time."))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/files/preview", skip_host=True)
        for k, v in (("Host", self.host), ("Content-Type", "application/json"), ("X-RT", "1")):
            c.putheader(k, v)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 411)
        c.close()

    def test_security_headers_on_every_answer_and_no_cors(self):
        answers = [self.get("/"), self.get("/api/status"), self.get("/nope"), self.post("/api/files/preview"),
                   self.get("/api/status", headers={"Host": "evil.example"}), self.call("OPTIONS", "/api/status")]
        for r in answers:
            for name in SECURITY:
                self.assertTrue(r.headers.get(name), (name, r))
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))
        self.assertNotEqual(answers[-1].status, 200)
        self.assertIn("frame-ancestors 'none'", answers[0].headers["Content-Security-Policy"])
        self.assertEqual(answers[0].headers["Cache-Control"], "no-store")

    def test_a_dropped_connection_stays_quiet(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(  # what Windows says when a tab drops a poll
                server.Handler, "end_headers", side_effect=ConnectionAbortedError(10053, "aborted")):
            with self.assertRaises((http.client.HTTPException, ConnectionError)):
                self.get("/api/status")
        with contextlib.redirect_stderr(err):  # e.g. a phone that lost Wi-Fi while sending files
            for e in (ConnectionResetError(54, "reset"), socket.timeout("timed out"), KeyError("boom")):
                try:
                    raise e
                except Exception:
                    self.srv.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")  # no traceback in the console window
        log = (self.data / "log.txt").read_text(encoding="utf-8")
        self.assertNotIn("failed", log.replace("A request failed: KeyError", ""))
        self.assertIn("A request failed: KeyError: 'boom'", log)

    def test_the_log_never_fails_a_request(self):
        self.app.log("a file name Python can't write \udcff")
        self.assertIn("can't write ?", (self.data / "log.txt").read_text(encoding="utf-8"))
        (self.data / "log.txt").write_text("x" * (server.LOG_BYTES + 1), encoding="utf-8")
        with mock.patch.object(server.paths, "replace", wraps=paths.replace) as replace:  # Windows-safe replace
            self.app.log("rotated")
        replace.assert_called_once()
        self.assertTrue((self.data / "log.old.txt").exists())
        (self.data / "log.txt").unlink()
        (self.data / "log.txt").mkdir()  # can't be written, like a read-only disk
        self.app.log("lost")
        self.assertEqual(self.post("/api/nothing-here").status, 404)
        self.assertEqual(self.get("/api/status").status, 200)

    def test_log_never_holds_a_code_state_or_key(self):
        self.app.network = True
        key = self.app.access_key()
        out = io.StringIO()
        self.app.echo = True
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            url = self.ok("/api/google/begin", {"purpose": "signin"})["url"]
            params = self.fake.approve(url)
            self.assertEqual(self.get("/oauth?" + urllib.parse.urlencode(params)).status, 200)
            self.get("/oauth?" + urllib.parse.urlencode(params))  # a repeat: same outcome
            url = self.ok("/api/google/begin", {"purpose": "signin"})["url"]
            pasted = self.fake.callback_url(url)
            self.ok("/api/google/finish", {"url": pasted})
            self.post("/api/google/finish", {"url": pasted + "x"})
            with mock.patch.object(server, "peer_is_loopback", return_value=False):
                self.get(f"/?key={key}", headers={"Host": f"pi.local:{self.port}"})
                self.get("/?key=wrong-key", headers={"Host": f"pi.local:{self.port}"})
            self.app.log(f"a message that mentions code=abc123 and ya29.secret-token and key={key}")
        logged = (self.data / "log.txt").read_text(encoding="utf-8") + out.getvalue()
        self.assertIn("GET /oauth 200", logged)
        token = json.loads((self.data / "google_token.json").read_text(encoding="utf-8"))
        for secret in (params["code"], params["state"], key, "abc123", "ya29.secret", token["refresh_token"],
                       token["access_token"], urllib.parse.parse_qs(pasted.split("?")[1])["code"][0]):
            self.assertNotIn(secret, logged)


class NetworkTests(Base):
    network = True

    def lan(self, path: str, method: str = "GET", body=None, cookie: str | None = None, **headers) -> Answer:
        h = {"Host": f"pi.local:{self.port}", "Origin": f"http://pi.local:{self.port}" if method == "POST" else None,
             "Cookie": f"rt_key={cookie}" if cookie else None, **headers}
        with mock.patch.object(server, "peer_is_loopback", return_value=False):
            return self.call(method, path, body, headers=h)

    def test_other_devices_need_the_key_link(self):
        key = self.app.access_key()
        self.assertEqual(len(key), 12)
        r = self.lan("/api/status")
        self.assertEqual((r.status, r.body["code"]), (403, "need_link"))
        script = paths.app_dir() / "reddit_tracker.py"
        self.assertIn(f'On the computer where Reddit Tracker runs, run: python3 "{script}" --show-link --port '
                      f'{self.port} --data "{self.data}". Or copy it', r.body["error"])  # a command that works
        self.assertIn("ends in ?key=", self.lan("/").body)
        self.assertEqual(self.lan("/?key=wrong").status, 403)
        self.assertEqual(self.lan("/api/status", cookie="wrong").status, 403)
        r = self.lan(f"/?key={key}")
        self.assertEqual((r.status, r.headers["Location"]), (302, "/"))
        self.assertEqual(r.headers["Set-Cookie"], f"rt_key={key}; HttpOnly; SameSite=Lax; Max-Age=31536000; Path=/")
        s = self.lan("/api/status", cookie=key)
        self.assertEqual(s.status, 200)
        self.assertTrue(s.body["app"]["remote"] and s.body["app"]["network"])
        self.assertTrue(s.body["app"]["links"] and all(link.endswith(f":{self.port}/?key={key}")
                                                       for link in s.body["app"]["links"]))
        page = self.lan("/", cookie=key)
        self.assertEqual(page.status, 200)
        self.assertIn(f"rt_key={key};", page.headers["Set-Cookie"])  # renewed on every visit
        old_bookmark = self.lan("/?key=an-older-key", cookie=key)  # this device still has a working key
        self.assertEqual((old_bookmark.status, old_bookmark.headers["Location"]), (302, "/"))
        self.assertEqual(self.get("/api/ping").body["network"], True)
        self.assertFalse(self.status()["app"]["remote"])

    def test_posts_from_another_device(self):
        key = self.app.access_key()
        rev = self.lan("/api/status", cookie=key).body["rev"]
        self.assertEqual(self.lan("/api/clients/add", "POST", {"rev": rev, "username": "acme_jane"}, key).status, 200)
        r = self.lan("/api/clients/add", "POST", {"rev": self.status()["rev"], "username": "bolt_bikes"}, key,
                     Origin=f"http://evil.example:{self.port}")
        self.assertEqual(r.status, 403)
        self.assertEqual(self.lan("/oauth?code=x&state=y", cookie=key).status, 403)  # sign-ins land on this computer

    def test_a_new_link_turns_the_old_one_off(self):
        old = self.app.access_key()
        links = self.ok("/api/network/new-link")["links"]
        new = self.app.access_key()
        self.assertNotEqual(old, new)
        self.assertTrue(all(link.endswith(f"?key={new}") for link in links))
        self.assertEqual(self.lan("/api/status", cookie=old).status, 403)
        self.assertEqual(self.lan("/api/status", cookie=new).status, 200)
        with mock.patch.object(server, "lan_hosts", return_value=["pi.local", "192.168.1.20"]):
            ip = self.lan("/api/network/new-link", "POST", {}, self.app.access_key(),
                          Host=f"192.168.1.20:{self.port}", Origin=f"http://192.168.1.20:{self.port}")
        self.assertEqual(ip.status, 200)
        self.assertEqual([urllib.parse.urlsplit(link).hostname for link in ip.body["links"]], ["192.168.1.20", "pi.local"])

    def test_show_link(self):
        key = self.app.access_key()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(server.show_link(self.data, 8765), 0)
        self.assertIn(f":8765/?key={key}", out.getvalue())
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stdout(out):
            self.assertEqual(server.show_link(Path(empty), 8765), 0)
        self.assertIn("Reddit Tracker hasn't started in network mode yet.", out.getvalue())

    def test_a_damaged_settings_file_keeps_other_devices_in(self):
        key = self.app.access_key()
        self.add("acme_jane")
        (self.data / "settings.json").write_text("{broken", encoding="utf-8")
        s = self.lan("/api/status", cookie=key)
        self.assertEqual(s.status, 200)
        self.assertIn("settings_problem", s.body)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            server.show_link(self.data, 8765)
        self.assertIn(f"?key={key}", out.getvalue())  # from the last good copy


class RouteTests(Base):
    def test_page_and_ping(self):
        page = self.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn("<title>Reddit Tracker</title>", page.body)
        self.assertIn("Test mode: Google is simulated", page.body)  # RT_GOOGLE_BASE is on
        self.assertTrue(page.headers["Content-Type"].startswith("text/html; charset=utf-8"))
        self.assertEqual(self.get("/api/ping").body, {"app": "reddit-tracker", "version": VERSION, "network": False})
        self.assertEqual(self.get("/api/nothing").status, 404)

    def test_status_shape_matches_the_spec(self):
        self.sign_in()
        acme = self.add("acme_jane", "Acme Jane")
        self.ok("/api/sheets/new", {"target": acme["id"]})
        self.wait_sheets()
        self.drop(POSTS, COMMENTS, ACCOUNT)
        self.wait_sheets()
        s = self.status()
        self.assertEqual(set(s), {"app", "rev", "google", "clients", "all_sheet", "sheets", "last_import", "first_run",
                                  "report"})
        self.assertEqual(set(s["app"]), {"version", "network", "remote", "data_dir", "links"})
        self.assertEqual(set(s["google"]) - {"test_mode"},
                         {"configured", "signed_in", "email", "name", "problem", "busy", "last"})
        self.assertLessEqual({"ok", "message", "at"}, set(s["google"]["last"]))
        self.assertEqual(set(s["sheets"]), {"running", "done", "total", "pending", "finished_at", "next_retry_at"})
        c, = s["clients"]
        self.assertEqual(set(c), {"id", "name", "username", "paused", "last", "items", "comment_reach",
                                  "username_locked", "sheet"})
        self.assertEqual(set(c["sheet"]), {"id", "title", "url", "state", "updated_at", "message", "code", "fix",
                                           "report"})
        self.assertEqual(set(s["last_import"]), {"at", "receipt"})
        self.assertEqual(s["report"], {"id": REPORT_TEMPLATE, "is_default": True,
                                       "url": f"https://datastudio.google.com/reporting/{REPORT_TEMPLATE}"})
        link = urllib.parse.urlsplit(c["sheet"]["report"]["url"])
        self.assertEqual(link[:3], ("https", "datastudio.google.com", "/reporting/create"))
        q = dict(urllib.parse.parse_qsl(link.query))
        self.assertEqual(q, {"c.reportId": REPORT_TEMPLATE, "r.reportName": "Acme Jane | Reddit organic report",
                             "ds.*.connector": "googleSheets", "ds.*.spreadsheetId": c["sheet"]["id"],
                             "ds.*.worksheetId": q["ds.*.worksheetId"], "ds.*.hasHeader": "true",
                             "ds.*.refreshFields": "false"})
        self.assertEqual(int(q["ds.*.worksheetId"]),
                         self.app.job.sheet_status(c["sheet"]["id"])["tabs"]["content"])
        self.assertEqual(c["last"], {"post": "2026-10-08", "comment": "2026-10-08", "account": "2026-10-08"})
        self.assertEqual((c["items"], c["comment_reach"], c["username_locked"]), (5, None, True))
        self.assertEqual((c["sheet"]["state"], c["sheet"]["title"]), ("ok", "Reddit Tracker – Acme Jane"))
        self.assertRegex(c["sheet"]["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(s["last_import"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(s["google"]["last"]["at"], r"Z$")
        self.assertFalse(s["first_run"])
        self.assertEqual(s["app"]["data_dir"], str(self.data))
        everything = json.dumps(s)
        token = json.loads((self.data / "google_token.json").read_text(encoding="utf-8"))
        self.assertNotIn(token["refresh_token"], everything)
        self.assertNotIn(token["access_token"], everything)

    def test_a_failing_status_says_why(self):
        self.add("acme_jane")
        (self.data / "reddit.db").unlink()  # e.g. a damaged SD card
        r = self.get("/api/status")
        self.assertEqual((r.status, r.body["code"]), (500, "status_failed"))
        self.assertEqual(r.body["error"], f"Reddit Tracker can't show your clients: {server.DAMAGED}.")

    def test_a_data_folder_that_cant_be_written(self):
        with mock.patch.object(self.app.job, "bump", side_effect=OSError(errno.EROFS, "Read-only file system")):
            r = self.post("/api/sheets/update")
        self.assertEqual((r.status, r.body["error"]), (500, "That didn't work: " + server.CANT_SAVE))
        for e in (PermissionError(errno.EACCES, "Permission denied"), OSError(errno.ENOSPC, "No space left"),
                  sqlite3.OperationalError("attempt to write a readonly database"),
                  sqlite3.OperationalError("unable to open database file"),
                  sqlite3.OperationalError("database or disk is full")):
            self.assertEqual(server.plain(e) + ".", server.CANT_SAVE, e)
        self.assertIn("busy", server.plain(sqlite3.OperationalError("database is locked")))
        self.assertIn("log.txt", server.plain(KeyError("x")))

    def test_the_database_stays_open_while_running(self):
        self.status()
        self.assertTrue((self.data / "reddit.db-wal").exists())  # not made and deleted again at every poll
        self.app.close()
        self.assertTrue(self.app.status(remote=False)["first_run"])

    def test_small_pages_keep_their_style(self):
        page = self.get("/oauth?code=x&state=y")
        self.assertIn("<style>body{font:16px/1.5", page.body)
        self.assertNotIn("{{", page.body)

    def test_first_run(self):
        s = self.status()
        self.assertTrue(s["first_run"])
        self.assertEqual((s["clients"], s["all_sheet"], s["last_import"]), ([], None, None))
        self.assertTrue(s["google"]["configured"] and not s["google"]["signed_in"])
        self.add("acme_jane")
        self.assertTrue(self.status()["first_run"])  # a client, but nothing imported yet

    def test_clients(self):
        rev = self.status()["rev"]
        r = self.post("/api/clients/add", {"rev": rev, "username": "x y"})
        self.assertEqual((r.status, r.body["code"], r.body["field"]), (422, "bad_username", "username"))
        acme = self.ok("/api/clients/add", {"rev": rev, "username": "https://www.reddit.com/user/Acme_Jan/?a=1"})
        self.assertEqual((acme["client"]["name"], acme["client"]["reddit_username"]), ("Acme Jan", "Acme_Jan"))
        self.assertIsNone(acme["sheets"])  # no numbers yet
        r = self.post("/api/clients/add", {"rev": rev, "username": "bolt_bikes"})
        self.assertEqual((r.status, r.body["code"]), (409, "stale"))
        r = self.post("/api/clients/add", {"rev": acme["rev"], "username": "acme_jan"})
        self.assertEqual((r.status, r.body["code"], r.body["field"]), (422, "username_taken", "username"))
        cid = acme["client"]["id"]
        renamed = self.ok("/api/clients/update", {"rev": acme["rev"], "id": cid, "name": "Acme"})
        self.assertEqual(renamed["client"]["name"], "Acme")
        moved = self.ok("/api/clients/update", {"rev": renamed["rev"], "id": cid, "username": "acme_jane"})
        self.assertEqual(moved["client"]["reddit_username"], "acme_jane")
        self.drop(POSTS)
        self.assertTrue(self.client(cid)["username_locked"])
        r = self.post("/api/clients/update", {"rev": self.status()["rev"], "id": cid, "username": "acme_other"})
        self.assertEqual((r.status, r.body["code"]), (422, "username_locked"))
        r = self.post("/api/clients/update", {"rev": self.status()["rev"], "id": "c-nobody", "name": "X"})
        self.assertEqual((r.status, r.body["code"]), (404, "no_such_client"))
        paused = self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": cid, "paused": True})
        self.assertTrue(paused["client"]["paused"])
        self.assertEqual(paused["sheets"], "not_signed_in")
        r = self.post("/api/clients/pause", {"rev": paused["rev"], "id": cid, "paused": "yes"})
        self.assertEqual(r.status, 400)
        self.ok("/api/clients/pause", {"rev": paused["rev"], "id": cid, "paused": False})
        removed = self.ok("/api/clients/remove", {"rev": self.status()["rev"], "id": cid})
        self.assertEqual(self.status()["clients"], [])
        self.assertEqual(removed["rev"], self.status()["rev"])
        # adding it back: its saved numbers count again, so the Sheets are told
        again = self.ok("/api/clients/add", {"rev": removed["rev"], "username": "ACME_JANE"})
        self.assertEqual(again["client"]["reddit_username"], "acme_jane")  # the database's spelling
        self.assertEqual(again["sheets"], "not_signed_in")

    def test_files_are_checked(self):
        acme = self.add("acme_jane")
        good = b.dropped("a", *POSTS)
        for files in ([], "x", [good, dict(good)], [{**good, "modified": "yesterday"}], [{**good, "modified": True}],
                      [{**good, "text": None}], [{**good, "client": 3}], [dict(good, id=str(i)) for i in range(201)]):
            r = self.post("/api/files/preview", {"files": files})
            self.assertEqual((r.status, r.body["code"]), (400, "bad_request"), str(files)[:80])
            self.assertEqual(self.post("/api/files/import", {"files": files}).status, 400)
        plan = self.ok("/api/files/preview", {"files": [good, b.dropped("b", *COMMENTS, modified=b.modified_ms(
            "2026-10-08"))]})
        self.assertEqual([(f["status"], f["client"]) for f in plan["files"]], [("ready", acme["id"])] * 2)
        self.assertEqual(self.status()["clients"][0]["items"], 0)  # a preview saves nothing

    def test_import(self):
        acme = self.add("acme_jane", "Acme Jane")
        r = self.drop(POSTS, COMMENTS, ACCOUNT)
        self.assertEqual(r["imported"], 3)
        self.assertEqual(r["sheets"], "not_signed_in")
        self.assertEqual(r["receipt"], [{"client": acme["id"], "name": "Acme Jane", "lines": [
            "3 posts (3 new)", "2 comments (2 new)", "Account numbers up to Thu 8 Oct"]}])
        s = self.status()
        self.assertEqual(s["last_import"]["receipt"], r["receipt"])
        self.assertTrue(s["sheets"]["pending"])
        # the same files again: nothing new, and nothing for the Sheets to do
        files = self.files(POSTS, COMMENTS, ACCOUNT)
        again = self.ok("/api/files/preview", {"files": files})
        self.assertEqual({f["status"] for f in again["files"]}, {"already"})
        self.assertEqual(sorted(p.name.split(" ")[0] for p in (self.data / "uploads").iterdir()), ["acme_jane"] * 3)
        # a file the page showed for another client is checked again, not imported
        changed = self.ok("/api/files/import", {"files": [dict(b.dropped("x", *POSTS), expect="c-someone")]})
        self.assertEqual((changed["files"][0]["status"], changed["imported"]), ("changed", 0))

    def test_google_sign_in_routes(self):
        for body in ({"purpose": "hack"}, {"purpose": "signin", "target": 3}, {"purpose": "pick"},
                     {"purpose": "pick", "target": ""}):
            self.assertEqual(self.post("/api/google/begin", body).status, 400, body)
        r = self.post("/api/google/begin", {"purpose": "pick", "target": "c-nobody"})
        self.assertEqual((r.status, r.body["code"]), (404, "no_such_client"))
        url = self.ok("/api/google/begin", {"purpose": "signin"})["url"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/drive.file"])
        self.assertEqual(query["redirect_uri"], [f"http://127.0.0.1:{self.port}/oauth"])
        self.assertTrue(self.status()["google"]["busy"])
        for text in ("", "hello", "https://evil.example/oauth?code=1&state=2"):
            r = self.post("/api/google/finish", {"url": text})
            self.assertEqual((r.status, r.body["code"]), (400, "bad_paste"), text)
        params = self.fake.approve(url)
        page = self.get("/oauth?" + urllib.parse.urlencode(params))
        self.assertIn("You're signed in.", page.body)
        self.assertIn("window.close()", page.body)
        self.assertNotIn(params["code"], page.body)
        s = self.status()["google"]
        self.assertEqual((s["signed_in"], s["email"], s["busy"], s["last"]["ok"]), (True, "jane@example.com", False,
                                                                                  True))
        # a repeated answer gets the same outcome
        self.assertIn("You're signed in.", self.get("/oauth?" + urllib.parse.urlencode(params)).body)
        # an address from a sign-in started elsewhere: handed over to paste there, never acted on
        other = self.get("/oauth?code=4%2Fabc&state=from-the-pi")
        self.assertIn("started on another device", other.body)
        self.assertIn("value=\"http://127.0.0.1:%d/oauth?code=4%%2Fabc&amp;state=from-the-pi\"" % self.port,
                      other.body)
        # cancelled on Google's page: our own words, not Google's
        self.fake.deny = True
        url = self.ok("/api/google/begin", {"purpose": "signin"})["url"]
        page = self.get("/oauth?" + urllib.parse.urlencode(self.fake.approve(url)) + "&error_description=%3Cb%3Ex")
        self.assertIn("cancelled", page.body)
        self.assertNotIn("<b>x", page.body)
        self.assertFalse(self.status()["google"]["last"]["ok"])
        self.fake.deny = False
        # the paste flow
        url = self.ok("/api/google/begin", {"purpose": "signin"})["url"]
        self.assertEqual(self.ok("/api/google/finish", {"url": self.fake.callback_url(url)}),
                         {"ok": True, "purpose": "signin", "target": None})
        self.assertEqual(self.ok("/api/google/signout"), {"ok": True})
        self.assertFalse(self.status()["google"]["signed_in"])

    def test_choose_a_sheet(self):
        self.sign_in()
        acme = self.add("acme_jane", "Acme Jane")
        theirs = self.fake.make_sheet("Acme numbers", granted=False)
        self.fake.pick_ids = [theirs]
        page = self.sign_in("pick", target=acme["id"])
        self.assertIn("Got your Sheet “Acme numbers”.", page.body)
        self.wait_sheets()
        card = self.client(acme["id"])["sheet"]
        self.assertEqual((card["id"], card["title"], card["state"]), (theirs, "Acme numbers", "ok"))
        self.assertEqual(self.fake.tab_names(theirs), ["content", "account_daily", "content_history"])
        self.assertEqual(self.status()["google"]["last"]["message"], "Chose “Acme numbers”. Filling it now.")
        # a Sheet that can't be used keeps the old one and says why
        view_only = self.fake.make_sheet("View only", granted=False)
        self.fake.files[view_only].can_edit = False
        self.fake.pick_ids = [view_only]
        seen, check = [], self.app.google.check_file
        self.app.google.check_file = lambda f: seen.append(self.app.google.last) or check(f)
        page = self.sign_in("pick", target=acme["id"])
        del self.app.google.check_file
        # while the file is checked the page still sees the last outcome, never an early "it worked"
        self.assertEqual(seen[0]["message"], "Chose “Acme numbers”. Filling it now.")
        self.assertIn("You can only view this Sheet", page.body)
        self.assertEqual(self.client(acme["id"])["sheet"]["id"], theirs)
        self.assertFalse(self.status()["google"]["last"]["ok"])
        # nothing picked
        self.fake.pick_ids = []
        self.assertIn("No Sheet was chosen", self.sign_in("pick", target=acme["id"]).body)
        # stopped, then chosen again with the same numbers: filled anew (it may have been changed meanwhile),
        # while the other client's unchanged Sheet is left alone
        bolt = self.add("bolt_bikes", "Bolt Bikes")
        bolt_sheet = self.ok("/api/sheets/new", {"target": bolt["id"]})["sheet"]["id"]
        self.wait_sheets()
        self.ok("/api/sheets/stop", {"target": acme["id"]})
        self.fake.pick_ids = [theirs]
        calls = len(self.fake.calls)
        self.sign_in("pick", target=acme["id"])
        self.wait_sheets()
        puts = [c[1] for c in self.fake.calls[calls:] if c[0] == "PUT"]
        self.assertTrue(any(theirs in path for path in puts))
        self.assertFalse(any(bolt_sheet in path for path in puts))
        # 'Try again' on one Sheet's card also rewrites only that Sheet
        calls = len(self.fake.calls)
        self.ok("/api/sheets/check", {"target": acme["id"]})
        self.wait_sheets()
        puts = [c[1] for c in self.fake.calls[calls:] if c[0] == "PUT"]
        self.assertTrue(any(theirs in path for path in puts) and not any(bolt_sheet in path for path in puts))
        # an unexpected failure while saving the choice still gives the page an answer
        with mock.patch.object(self.app.settings, "set_sheet", side_effect=OSError(errno.ENOSPC, "No space")):
            self.sign_in("pick", target=acme["id"])
        last = self.status()["google"]["last"]
        self.assertEqual((last["ok"], last["purpose"], last["message"]), (False, "pick", CANT_SAVE))
        # the sign-in itself can't be saved (a full or read-only card): the Google tab and the page both say why
        with mock.patch("rt.google.write_private", side_effect=OSError(errno.EROFS, "Read-only file system")):
            page = self.sign_in("pick", target=acme["id"])
        self.assertIn("can&#x27;t save to its data folder", page.body.replace("'", "&#x27;"))
        last = self.status()["google"]["last"]
        self.assertEqual((last["ok"], last["message"]), (False, CANT_SAVE))

    def test_a_pass_part_way_through_an_import_leaves_the_rest_pending(self):
        self.add("acme_jane", "Acme Jane")
        real = engine.run_import

        def run(*args, on_saved=None, **kw):
            def saved():  # the first file is in, and a pass that starts now marks what it saw as written
                on_saved()
                self.app.job._change(lambda d: d.update(written=d.get("wanted")))
            return real(*args, on_saved=saved, **kw)
        with mock.patch.object(engine, "run_import", run):
            self.assertEqual(self.drop(POSTS, COMMENTS, ACCOUNT)["imported"], 3)
        self.assertTrue(self.app.job.pending())  # the files after the first still need the Sheets

    def test_sheet_routes(self):
        acme = self.add("acme_jane", "Acme Jane")
        r = self.post("/api/sheets/new", {"target": acme["id"]})
        self.assertEqual((r.status, r.body["code"]), (409, "signed_out"))
        self.assertEqual(self.post("/api/sheets/new", {"target": "c-nobody"}).status, 404)
        self.assertEqual(self.post("/api/sheets/new", {}).status, 400)
        self.sign_in()
        self.fake.fail(r"^/v4/spreadsheets$", 403, method="POST", reason="storageQuotaExceeded")
        r = self.post("/api/sheets/new", {"target": acme["id"]})
        self.assertEqual((r.status, r.body["code"]), (502, "drive_full"))  # our status, not Google's 403
        self.assertIn("Google Drive is full", r.body["error"])
        made = self.ok("/api/sheets/new", {"target": acme["id"]})
        sheet = made["sheet"]
        self.assertEqual(sheet["title"], "Reddit Tracker – Acme Jane")
        self.assertIn(made["sheets"], ("started", "queued"))
        self.wait_sheets()
        self.assertEqual(self.fake.data(sheet["id"], "content")[0][:3], ["kind", "account", "posted_date"])
        self.assertEqual(self.client(acme["id"])["sheet"]["state"], "ok")
        self.assertEqual(self.ok("/api/sheets/update")["sheets"], "started")
        self.wait_sheets()
        self.assertEqual(self.ok("/api/sheets/check", {"target": acme["id"]})["sheet"]["code"], "ok")
        self.wait_sheets()
        self.fake.files[sheet["id"]].trashed = True
        r = self.post("/api/sheets/check", {"target": acme["id"]})
        self.assertEqual((r.status, r.body["code"]), (422, "trashed"))
        self.fake.files[sheet["id"]].trashed = False
        self.ok("/api/sheets/replace-anyway", {"target": acme["id"]})
        self.wait_sheets()
        self.assertFalse(self.app.job.sheet_status(sheet["id"]).get("allow_shrink"))  # used up by the write
        self.assertEqual(self.ok("/api/sheets/stop", {"target": acme["id"]}), {"ok": True})
        self.assertEqual(self.client(acme["id"])["sheet"], {**sheet_card(None, None, None, None, False, False, True,
                                                                         False), "report": None})
        r = self.post("/api/sheets/check", {"target": acme["id"]})
        self.assertEqual((r.status, r.body["code"]), (422, "no_sheet"))
        all_made = self.ok("/api/sheets/new", {"target": "all"})
        self.assertEqual(all_made["sheet"]["title"], "Reddit Tracker – All clients")
        self.wait_sheets()
        self.assertEqual(self.status()["all_sheet"]["state"], "ok")
        self.assertEqual(self.ok("/api/sheets/stop", {"target": "all"}), {"ok": True})
        self.assertIsNone(self.status()["all_sheet"])

    def test_only_asked_for_updates_rewrite_unchanged_sheets(self):
        calls = []

        def start(reason="", force=False):
            calls.append((reason, force))
            return "queued"
        with mock.patch.object(self.app.job, "start", start):
            self.sign_in()
            acme = self.add("acme_jane", "Acme Jane")
            sheet = self.ok("/api/sheets/new", {"target": acme["id"]})["sheet"]["id"]
            self.drop(POSTS)
            self.ok("/api/sheets/update")
            self.ok("/api/sheets/check", {"target": acme["id"]})
            self.fake.pick_ids = [sheet]
            self.sign_in("pick", target=acme["id"])  # the same Sheet again
            self.fake.pick_ids = [self.fake.make_sheet("Acme numbers", granted=False)]
            self.sign_in("pick", target=acme["id"])
            self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": acme["id"], "paused": True})
            url = self.ok("/api/google/begin", {"purpose": "signin"})["url"]
            self.ok("/api/google/finish", {"url": self.fake.callback_url(url)})
        self.assertEqual(calls, [("signed in", True), ("new Sheet", False), ("new numbers", False), ("asked", True),
                                 ("Sheet checked", False), ("Sheet chosen", False), ("Sheet chosen", False),
                                 ("paused or resumed", False), ("signed in", True)])

    def test_each_sheets_tables_are_read_from_one_snapshot_without_db_lock(self):
        acme = self.add("acme_jane", "Acme Jane")
        bolt = self.add("bolt_bikes", "Bolt Bikes")
        self.drop(POSTS, BOLT_POSTS)
        self.app.settings.set_sheet(acme["id"], "SHEET_A", "Acme")
        self.app.settings.set_sheet(bolt["id"], "SHEET_B", "Bolt")
        seen, got = [], []
        real = store.build_tables

        def spy(con, users, tab=None):
            seen.append((sorted(users), con.in_transaction))
            return real(con, users, tab)

        def write_each():
            for t in self.app.build_targets():  # as the Sheets job does: one Sheet's tables at a time
                with t["read_tables"]() as tables:
                    got.append((t["sheet_id"], len(tables["content"]), type(tables["content_history"]),
                                len(list(tables["content_history"]))))
        with mock.patch.object(store, "build_tables", spy), self.app.db_lock:  # an import holds the lock
            reader = threading.Thread(target=write_each)
            reader.start()
            reader.join(10)
        self.assertEqual(seen, [(["acme_jane"], True), (["bolt_bikes"], True)])  # each Sheet's from one moment
        self.assertEqual(got, [("SHEET_A", 1 + 3, store.History, 1 + 3), ("SHEET_B", 1 + 4, store.History, 1 + 4)])

    def test_google_no_longer_accepting_this_copy(self):
        refused = GoogleError("app_refused", "Google no longer accepts this copy of Reddit Tracker.")
        with mock.patch.object(self.google, "begin", side_effect=refused):
            r = self.post("/api/google/begin", {"purpose": "signin"})
        self.assertEqual((r.status, r.body["code"]), (409, "app_refused"))

    def test_export_csv(self):
        acme = self.add("acme_jane", "Acme Jane")
        self.drop(POSTS, ("acme_jane Comment Insights Report.csv", b.report("comment", [
            b.comment_row("c9", title="=HYPERLINK(\"http://x\")", sub="r/test")])))
        r = self.get(f"/api/export.csv?client={acme['id']}&tab=content")
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.headers["Content-Length"])  # sent as it's read; the connection's end ends it
        self.assertTrue(r.headers["Content-Type"].startswith("text/csv; charset=utf-8"))
        self.assertIn('attachment; filename="Acme Jane content.csv"', r.headers["Content-Disposition"])
        self.assertTrue(r.body.startswith("\ufeffkind,account,posted_date"))
        self.assertIn("'=HYPERLINK", r.body)
        self.assertEqual(len(r.body.strip().splitlines()), 1 + 4)
        self.assertEqual(self.get(f"/api/export.csv?client={acme['id']}&tab=secrets").status, 400)
        self.assertEqual(self.get("/api/export.csv?client=c-nobody").status, 404)
        history = self.get(f"/api/export.csv?client={acme['id']}&tab=content_history")
        self.assertTrue(history.body.startswith("\ufeffexport_date,"))
        rows = self.tables({"acme_jane"})["content_history"]
        self.assertEqual(history.body, "\ufeff" + "".join(store.export_csv(rows)))
        self.assertEqual(len(history.body.strip().splitlines()), 1 + 4)
        with mock.patch.object(store, "export_csv", side_effect=sqlite3.DatabaseError("disk I/O error")):
            broken = self.get(f"/api/export.csv?client={acme['id']}&tab=content_history")
        self.assertEqual(broken.body, "\ufeff")  # a problem once the download had begun: it ends short
        self.assertIn("GET /api/export.csv failed: DatabaseError", (self.data / "log.txt").read_text(encoding="utf-8"))

    def test_settings_damaged_and_restored(self):
        acme = self.add("acme_jane", "Acme Jane")
        self.add("bolt_bikes")
        (self.data / "settings.json").write_text('{"clients": [}', encoding="utf-8")
        s = self.status()
        self.assertEqual(s["clients"], [])
        self.assertIn("settings file is damaged", s["settings_problem"]["message"])
        self.assertTrue(s["settings_problem"]["backup"].endswith("settings.last-saved.json"))
        for path, body in (("/api/files/preview", {"files": [b.dropped("a", *POSTS)]}),
                           ("/api/files/import", {"files": [b.dropped("a", *POSTS, expect=acme["id"])]}),
                           ("/api/clients/add", {"rev": s["rev"], "username": "cedar_lane"}),
                           ("/api/clients/pause", {"rev": s["rev"], "id": acme["id"], "paused": True}),
                           ("/api/sheets/update", {}), ("/api/sheets/new", {"target": "all"})):
            r = self.post(path, body)
            self.assertEqual((r.status, r.body["code"]), (409, "broken_file"), path)
        self.assertEqual(self.app.job.start(), "not_signed_in")
        restored = self.ok("/api/settings/restore")
        self.assertTrue(restored["restored"].endswith("settings.last-saved.json"))
        s = self.status()
        self.assertNotIn("settings_problem", s)
        self.assertEqual([c["name"] for c in s["clients"]], ["Acme Jane", "bolt bikes"])


class SheetsThroughTheServerTests(Base):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.acme = self.add("acme_jane", "Acme Jane")
        self.bolt = self.add("bolt_bikes", "Bolt Bikes")
        self.ids = {}
        for target in ("all", self.acme["id"], self.bolt["id"]):
            self.ids[target] = self.ok("/api/sheets/new", {"target": target})["sheet"]["id"]
            self.wait_sheets()

    def test_import_updates_every_sheet(self):
        r = self.drop(POSTS, COMMENTS, ACCOUNT, BOLT_POSTS)
        self.assertIn(r["sheets"], ("started", "queued"))
        state = self.wait_sheets()
        self.assertEqual([(t["name"], t["state"]) for t in state["targets"]],
                         [("All clients", "done"), ("Acme Jane", "done"), ("Bolt Bikes", "done")])
        self.assert_sheet(self.ids["all"], {"acme_jane", "bolt_bikes"})
        self.assert_sheet(self.ids[self.acme["id"]], {"acme_jane"})
        self.assert_sheet(self.ids[self.bolt["id"]], {"bolt_bikes"})
        s = self.status()
        self.assertFalse(s["sheets"]["pending"])
        self.assertEqual([c["sheet"]["state"] for c in s["clients"]] + [s["all_sheet"]["state"]], ["ok"] * 3)

    def test_unchanged_sheets_are_left_alone_unless_asked(self):
        self.drop(POSTS, COMMENTS)
        self.wait_sheets()
        writes = len(self.fake.writes())
        # Bolt Bikes has no numbers: pausing it changes no Sheet, so Google isn't called, and the cards stay ok
        self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": self.bolt["id"], "paused": True})
        state = self.wait_sheets()
        self.assertEqual([(t["name"], t["state"]) for t in state["targets"]],
                         [("All clients", "done"), ("Acme Jane", "done")])
        self.assertEqual(len(self.fake.writes()), writes)
        s = self.status()
        self.assertEqual([s["all_sheet"]["state"], self.client(self.acme["id"])["sheet"]["state"]], ["ok", "ok"])
        self.assertTrue(s["all_sheet"]["updated_at"])
        self.assertFalse(s["sheets"]["pending"])
        self.ok("/api/sheets/update")  # "Update Google Sheets now" writes every Sheet again
        self.wait_sheets()
        self.assertGreater(len(self.fake.writes()), writes)
        self.assert_sheet(self.ids["all"], {"acme_jane"})

    def test_offline_stops_the_pass_and_try_again_finishes_it(self):
        self.fake.offline = True
        self.drop(POSTS)
        state = self.wait_sheets()
        self.assertEqual([t["state"] for t in state["targets"]], ["failed", "skipped", "skipped"])
        self.assertEqual(state["targets"][0]["code"], "offline")
        s = self.status()
        self.assertTrue(s["sheets"]["pending"] and s["sheets"]["next_retry_at"])
        self.assertEqual((s["all_sheet"]["state"], s["all_sheet"]["fix"]), ("problem", "retry"))
        skipped = s["clients"][0]["sheet"]  # not reached: it doesn't have the new numbers yet, and says so
        self.assertEqual((skipped["state"], skipped["message"]), ("waiting", "Not updated yet: waiting for Google."))
        self.assertEqual(NoTimer.made[-1].seconds, 300)
        r = self.post("/api/sheets/check", {"target": "all"})
        self.assertEqual((r.status, r.body["code"]), (502, "offline"))
        self.fake.offline = False
        self.assertEqual(self.ok("/api/sheets/update")["sheets"], "started")
        self.wait_sheets()
        s = self.status()
        self.assertFalse(s["sheets"]["pending"] or s["sheets"]["next_retry_at"])
        self.assert_sheet(self.ids["all"], {"acme_jane", "bolt_bikes"})

    def test_pausing_takes_a_client_out_of_the_all_clients_sheet(self):
        self.drop(POSTS, COMMENTS, BOLT_POSTS)
        self.wait_sheets()
        self.assertEqual(len(self.fake.data(self.ids["all"], "content")), 1 + 9)
        bolt_before = self.fake.data(self.ids[self.bolt["id"]], "content")
        self.assertIn(self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": self.bolt["id"],
                                                     "paused": True})["sheets"], ("started", "queued"))
        state = self.wait_sheets()
        self.assertEqual([t["name"] for t in state["targets"]], ["All clients", "Acme Jane"])
        self.assert_sheet(self.ids["all"], {"acme_jane"})
        self.assertEqual(self.fake.data(self.ids[self.bolt["id"]], "content"), bolt_before)  # left as it was
        # nobody active: nothing is written, and the All clients Sheet keeps its rows
        self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": self.acme["id"], "paused": True})
        self.assertEqual(self.app.job.state()["targets"], [])
        self.assert_sheet(self.ids["all"], {"acme_jane"})
        self.assertFalse(self.status()["sheets"]["pending"])

    def test_one_sheet_for_two_clients_is_shown_and_skipped(self):
        settings = json.loads((self.data / "settings.json").read_text(encoding="utf-8"))
        settings["clients"][1]["sheet_id"] = settings["clients"][0]["sheet_id"]  # a hand edit
        (self.data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        self.drop(POSTS)
        state = self.wait_sheets()
        self.assertEqual([t["name"] for t in state["targets"]], ["All clients"])
        for c in self.status()["clients"]:
            self.assertEqual((c["sheet"]["state"], c["sheet"]["code"], c["sheet"]["fix"]), ("problem", "shared", None))

    def test_a_sheet_google_lost_asks_to_choose_it_again(self):
        acme_sheet = self.ids[self.acme["id"]]
        self.fake.files[acme_sheet].granted.clear()
        self.drop(POSTS)
        self.wait_sheets()
        card = self.client(self.acme["id"])["sheet"]
        self.assertEqual((card["state"], card["code"], card["fix"]), ("problem", "not_found", "choose"))
        self.assertFalse(self.status()["sheets"]["pending"])  # a person has to fix it: not retried
        self.fake.pick_ids = [acme_sheet]
        self.sign_in("pick", target=self.acme["id"], file_id=acme_sheet)  # the same Sheet again
        self.assertEqual(self.fake.last_auth.get("file_ids"), acme_sheet)
        self.wait_sheets()
        self.assertEqual(self.client(self.acme["id"])["sheet"]["state"], "ok")
        self.assert_sheet(acme_sheet, {"acme_jane"})

    def test_signed_out_then_back(self):
        self.ok("/api/google/signout")
        r = self.drop(POSTS)
        self.assertEqual(r["sheets"], "not_signed_in")
        card = self.client(self.acme["id"])["sheet"]
        self.assertEqual((card["state"], card["fix"]), ("waiting", "signin"))
        self.sign_in()
        self.wait_sheets()
        self.assertEqual(self.client(self.acme["id"])["sheet"]["state"], "ok")
        self.assert_sheet(self.ids[self.acme["id"]], {"acme_jane"})

    def test_no_all_clients_sheet_is_fine(self):
        self.ok("/api/sheets/stop", {"target": "all"})
        self.drop(POSTS)
        state = self.wait_sheets()
        self.assertEqual([(t["name"], t["state"]) for t in state["targets"]],
                         [("Acme Jane", "done"), ("Bolt Bikes", "done")])
        self.assertFalse(self.status()["sheets"]["pending"])


class StartTests(Base):
    """main(): an already running Reddit Tracker, a port another program uses, --show-link."""

    def start(self, *argv: str) -> tuple[int | None, str, object]:
        import reddit_tracker
        out, got = io.StringIO(), {}

        def ready(srv):
            got["server"] = srv
            threading.Timer(0.05, srv.shutdown).start()
        with tempfile.TemporaryDirectory() as other, contextlib.redirect_stdout(out):
            code = server.main(reddit_tracker.parse(["--no-browser", "--data", other, *argv]), on_ready=ready)
        return code, out.getvalue(), got.get("server")

    def test_already_running_opens_it_instead(self):
        code, said, srv = self.start("--port", str(self.port))
        self.assertEqual((code, srv), (0, None))
        self.assertIn(f"already running: http://127.0.0.1:{self.port}", said)  # --no-browser: nothing opened
        import reddit_tracker
        with mock.patch.object(server.webbrowser, "open") as opened, contextlib.redirect_stdout(io.StringIO()) as out, \
                tempfile.TemporaryDirectory() as other:
            code = server.main(reddit_tracker.parse(["--data", other, "--port", str(self.port)]))
        self.assertEqual((code, opened.call_args[0][0]), (0, f"http://127.0.0.1:{self.port}"))
        self.assertIn(f"already running. Opening it: http://127.0.0.1:{self.port}", out.getvalue())

    def test_network_start_while_a_copy_for_this_computer_only_runs(self):
        code, said, srv = self.start("--network", "--port", str(self.port))  # the Raspberry Pi service
        self.assertEqual((code, srv), (3, None))  # 3: the service doesn't keep restarting
        self.assertIn("already open on this computer, but only for this computer. Close that window, then start "
                      "it again.", said)
        self.app.network = True
        code, said, srv = self.start("--network", "--port", str(self.port))
        self.assertEqual((code, srv), (0, None))
        self.assertIn(f"already running: http://127.0.0.1:{self.port}", said)

    def test_two_starts_at_the_same_moment(self):
        other = {"app": "reddit-tracker", "version": VERSION, "network": False}
        with socket.socket() as busy:  # the other copy took the port just after this one checked it
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            answers = iter([None, other])
            with mock.patch.object(server, "ping", lambda port, tries=1: next(answers)):
                code, said, srv = self.start("--port", str(taken))
        self.assertEqual((code, srv), (0, None))
        self.assertIn(f"already running: http://127.0.0.1:{taken}", said)
        # a first start: the other copy is still making the database
        for answers, want in ((iter([None, other]), 0), (iter([None, None]), 1)):
            with mock.patch.object(server, "ping", lambda port, tries=1: next(answers)), \
                    mock.patch.object(server, "App", side_effect=sqlite3.OperationalError("database is locked")):
                code, said, srv = self.start("--port", str(self.port))
            self.assertEqual((code, srv), (want, None))
        self.assertIn("can't be opened (database is locked)", said)

    def test_a_port_that_cant_be_used(self):
        import reddit_tracker
        for bad in ("0", "70000", "-1", "eighty"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                reddit_tracker.parse(["--port", bad])
            self.assertEqual(stop.exception.code, 2)
            self.assertIn("argument --port: use a number from 1 to 65535, for example 8765", err.getvalue())
        self.assertEqual(reddit_tracker.parse(["--port", "80"]).port, 80)

    def test_a_port_another_program_uses(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            code, said, srv = self.start("--port", str(taken))  # a chosen port is kept: plain advice
            self.assertEqual((code, srv), (1, None))
            self.assertIn(f"another program is using port {taken}", said)
            self.assertIn(f'for example: python3 "{paths.app_dir() / "reddit_tracker.py"}" --port {taken + 20}', said)
            with mock.patch.object(server, "DEFAULT_PORT", taken):  # the usual port: the next free one is used
                code, said, srv = self.start()
            self.assertEqual(code, 0)
            self.assertGreater(srv.app.port, taken)
            self.assertIn(f"Open it in your browser: http://127.0.0.1:{srv.app.port}", said)
            self.assertIn("Keep this window open", said)

    def test_a_start_that_fails_keeps_the_crash_mark(self):
        import reddit_tracker
        with socket.socket() as busy, tempfile.TemporaryDirectory() as data:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            status = Path(data) / "sheets_status.json"
            status.write_text(json.dumps({"wanted": 3, "written": 1, "pass_running": "2026-11-10T01:00:00Z"}))
            with contextlib.redirect_stdout(io.StringIO()):
                code = server.main(reddit_tracker.parse(["--no-browser", "--data", data, "--port", str(taken)]))
            self.assertEqual(code, 1)
            self.assertIn("pass_running", json.loads(status.read_text()))  # it never ran: still waits after a crash

    def test_stopping_runs_once_and_leaves_the_signals_as_they_were(self):
        import os
        import signal
        import reddit_tracker
        names = [n for n in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, n)]
        before = {n: signal.getsignal(getattr(signal, n)) for n in names}
        code, said, srv = self.start()
        self.assertEqual(code, 0)
        self.assertEqual({n: signal.getsignal(getattr(signal, n)) for n in names}, before)
        if os.name != "nt":  # stopped like systemd stops it (SIGTERM), here in this process
            def stop_like_systemd(srv):
                def later():
                    for _ in range(500):
                        if signal.getsignal(signal.SIGTERM) is server._stop:
                            break
                        time.sleep(0.01)
                    os.kill(os.getpid(), signal.SIGTERM)
                threading.Thread(target=later, daemon=True).start()
            with socket.socket() as free:
                free.bind(("127.0.0.1", 0))
                port = free.getsockname()[1]
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as data, contextlib.redirect_stdout(out):
                code = server.main(reddit_tracker.parse(["--no-browser", "--data", data, "--port", str(port)]),
                                   on_ready=stop_like_systemd)
            self.assertEqual(code, 0)
            self.assertIn("Reddit Tracker stopped.", out.getvalue())
            self.assertEqual({n: signal.getsignal(getattr(signal, n)) for n in names}, before)
        try:
            with self.assertRaises(KeyboardInterrupt):
                server._stop()  # the first SIGHUP of a closed window
            # the second one (and Ctrl+C or SIGTERM) can't cut the clean-up short
            self.assertEqual({signal.getsignal(getattr(signal, n)) for n in names}, {signal.SIG_IGN})
        finally:
            for n, handler in before.items():
                signal.signal(getattr(signal, n), handler)

    def test_show_link_starts_nothing(self):
        code, said, srv = self.start("--show-link")
        self.assertEqual((code, srv), (0, None))
        self.assertIn("hasn't started in network mode yet", said)


class SheetCardTests(unittest.TestCase):
    OK = {"ok": True, "code": None, "message": None, "rows": 3, "at": "2026-11-01T10:00:00Z",
          "ok_at": "2026-11-01T10:00:00Z", "title": "Acme Sheet"}

    def card(self, saved=None, live=None, running=False, shared=False, signed_in=True, pending=False, sid="S1"):
        return sheet_card(sid, "Settings title", saved, live, running, shared, signed_in, pending)

    def failed(self, code):
        return {**self.OK, "ok": False, "code": code, "message": f"{code} message", "ok_at": "2026-10-01T09:00:00Z"}

    def test_states(self):
        none = self.card(sid=None)
        self.assertEqual((none["state"], none["fix"], none["id"]), ("none", "choose", None))
        ok = self.card(self.OK)
        self.assertEqual((ok["state"], ok["title"], ok["updated_at"], ok["fix"]),
                         ("ok", "Acme Sheet", "2026-11-01T10:00:00Z", None))
        self.assertEqual(ok["url"], "https://docs.google.com/spreadsheets/d/S1/edit")
        fresh = self.card(None)
        self.assertEqual((fresh["state"], fresh["message"], fresh["title"]), ("waiting", "Not filled yet",
                                                                              "Settings title"))
        self.assertEqual(self.card({"allow_shrink": True})["state"], "waiting")
        shared = self.card(self.OK, shared=True)
        self.assertEqual((shared["state"], shared["code"], shared["fix"], shared["message"]),
                         ("problem", "shared", None, server.SHARED))

    def test_a_running_pass(self):
        live = {"key": "c-1", "sheet_id": "S1", "state": "waiting", "code": None, "message": None}
        self.assertEqual(self.card(self.OK, live, running=True)["state"], "writing")
        self.assertEqual(self.card(self.OK, {**live, "state": "writing"}, running=True)["state"], "writing")
        self.assertEqual(self.card(self.OK, {**live, "sheet_id": "S2"}, running=True)["state"], "ok")
        skipped = self.card(self.OK, {**live, "state": "skipped"})  # the pass stopped before this Sheet
        self.assertEqual((skipped["state"], skipped["message"], skipped["updated_at"]),
                         ("waiting", "Not updated yet: waiting for Google.", "2026-11-01T10:00:00Z"))
        after_crash = self.card(self.OK, {**live, "state": "skipped", "message": "Not updated yet: soon."})
        self.assertEqual((after_crash["state"], after_crash["message"]), ("waiting", "Not updated yet: soon."))
        done = self.card(self.failed("offline"), {**live, "state": "done"}, running=True)
        self.assertEqual(done["state"], "ok")
        failed = self.card(self.OK, {**live, "state": "failed", "code": "quota", "message": "Slow down"})
        self.assertEqual((failed["state"], failed["code"], failed["message"], failed["fix"]),
                         ("problem", "quota", "Slow down", "retry"))

    def test_fix_for_every_code(self):
        table = {"not_found": "choose", "trashed": "choose", "not_a_sheet": "choose", "excel_file": "choose",
                 "read_only": "choose", "rejected": "signin", "signed_out": "signin", "shrink": "replace",
                 "too_big": "new", "drive_full": "new", "offline": "retry", "quota": "retry", "google_down": "retry",
                 "protected": "retry", "cert": "retry", "other": "retry", "storage_full": "retry", "api_off": None,
                 "app_refused": None, "too_much": None}
        for code, fix in table.items():
            card = self.card(self.failed(code), signed_in=False)
            self.assertEqual((card["state"], card["code"], card["fix"], card["message"]),
                             ("problem", code, fix, f"{code} message"), code)
            self.assertEqual(card["updated_at"], "2026-10-01T09:00:00Z")

    def test_sign_in_states(self):
        back = self.card(self.failed("rejected"), signed_in=True, pending=True)
        self.assertEqual((back["state"], back["fix"], back["message"]),
                         ("waiting", None, "Will update now that you're signed in."))
        waiting = self.card(self.OK, signed_in=False, pending=True)
        self.assertEqual((waiting["state"], waiting["fix"]), ("waiting", "signin"))
        self.assertEqual(self.card(self.OK, signed_in=False, pending=False)["state"], "ok")
        lasting = self.card(self.failed("not_found"), signed_in=False, pending=True)
        self.assertEqual((lasting["state"], lasting["fix"]), ("problem", "choose"))
        offline = self.card(self.failed("offline"), signed_in=False, pending=True)
        self.assertEqual((offline["state"], offline["fix"]), ("waiting", "signin"))

    def test_no_sign_in_when_google_cant_be_used(self):
        # Google isn't set up, or no longer accepts this copy (app_refused): signing in can't help
        def card(saved):
            return sheet_card("S1", "t", saved, None, False, False, False, True, configured=False)
        self.assertEqual((card(self.OK)["state"], card(self.OK)["fix"]), ("ok", None))
        for code, fix in (("app_refused", None), ("rejected", None), ("offline", "retry")):
            got = card(self.failed(code))
            self.assertEqual((got["state"], got["code"], got["fix"]), ("problem", code, fix))


class HelperTests(unittest.TestCase):
    def test_port_80_needs_no_port_in_the_address(self):
        def local(port: int, host: str) -> bool:
            handler = types.SimpleNamespace(server=types.SimpleNamespace(server_address=("127.0.0.1", port)),
                                            headers={"Host": host}, client_address=("127.0.0.1", 50000))
            return server.Handler.local(handler)
        for host in ("127.0.0.1", "localhost", "[::1]", "127.0.0.1:80"):
            self.assertTrue(local(80, host), host)
        for port, host in ((8765, "127.0.0.1"), (80, "evil.example"), (80, "127.0.0.1:8765")):
            self.assertFalse(local(port, host), host)

    def test_commands_say_py_on_windows(self):
        script = paths.app_dir() / "reddit_tracker.py"
        self.assertEqual(server.run_command("--show-link"), f'python3 "{script}" --show-link')
        with mock.patch.object(server.os, "name", "nt"):
            self.assertEqual(server.run_command("--port", "8785"), f'py "{script}" --port 8785')

    def test_the_start_file_gets_to_its_version_check_on_old_pythons(self):
        source = (HERE.parent / "reddit_tracker.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):  # each of these stops Python 3.6 or older before the check
            self.assertFalse(isinstance(node, ast.ImportFrom) and node.module == "__future__", "a __future__ import")
            self.assertNotIsInstance(node, (ast.JoinedStr, ast.AnnAssign))
            if isinstance(node, ast.FunctionDef):
                self.assertFalse(node.returns or any(a.annotation for a in node.args.args), node.name)



class DataStudioTests(Base):
    """"Connect a Data Studio report": a Linking API link to a copy of the template, on the client's Sheet."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.acme = self.add("acme_jane", "Acme Jane")

    def report(self) -> dict | None:
        c, = self.status()["clients"]
        return c["sheet"]["report"]

    def with_sheet(self) -> str:
        sid = self.ok("/api/sheets/new", {"target": self.acme["id"]})["sheet"]["id"]
        self.wait_sheets()
        return sid

    def test_no_button_without_a_sheet(self):
        self.assertIsNone(self.report())
        self.assertEqual(self.post("/api/report", {"client": self.acme["id"]}).body["code"], "no_sheet")

    def test_a_written_sheet_has_its_link_and_an_unchanged_pass_keeps_it(self):
        sid = self.with_sheet()
        url = self.report()["url"]
        self.assertIn(f"ds.%2A.spreadsheetId={sid}", url)
        self.app.job.start("again")  # unchanged: skipped without a Google call
        self.wait_sheets()
        self.assertEqual(self.report()["url"], url)

    def test_a_sheet_written_before_tab_ids_were_kept_asks_google_once(self):
        sid = self.with_sheet()
        content = self.app.job.sheet_status(sid)["tabs"]["content"]

        def drop(d):
            d["sheets"][sid].pop("tabs")
        self.app.job._change(drop)
        self.assertEqual(self.report(), {"url": None})
        url = self.ok("/api/report", {"client": self.acme["id"]})["url"]
        self.assertIn(f"ds.%2A.worksheetId={content}", url)
        self.assertEqual(self.report()["url"], url)

    def test_your_own_template_turning_it_off_and_back(self):
        self.with_sheet()
        mine = "1a2b3c4d-aaaa-bbbb-cccc-0123456789ab"
        got = self.ok("/api/settings/report", {"link": f"https://lookerstudio.google.com/reporting/{mine}/page/x"})
        self.assertEqual(got["report"], {"id": mine, "is_default": False,
                                         "url": f"https://datastudio.google.com/reporting/{mine}"})
        self.assertIn(f"c.reportId={mine}", self.report()["url"])
        bad = self.post("/api/settings/report", {"link": "my report"})
        self.assertEqual((bad.status, bad.body["code"], bad.body["field"]), (400, "bad_report_link", "link"))
        self.assertEqual(self.ok("/api/settings/report", {"link": ""})["report"],
                         {"id": None, "is_default": False, "url": None})
        self.assertIsNone(self.report())
        self.assertEqual(self.post("/api/report", {"client": self.acme["id"]}).body["code"], "report_off")
        self.assertTrue(self.ok("/api/settings/report", {"link": None})["report"]["is_default"])
        self.assertIn(f"c.reportId={REPORT_TEMPLATE}", self.report()["url"])

    def test_fonts_are_served_from_the_app_and_nothing_else(self):
        r = self.get("/fonts/montserrat.woff2")
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "font/woff2"))
        self.assertIn("font-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(self.get("/fonts/../server.py").status, 404)
        self.assertEqual(self.get("/fonts/other.woff2").status, 404)


class KeepTheSheetsNumbersTests(Base):
    """A Sheet filled elsewhere (an older tracker, another computer): its numbers can be copied back."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.acme = self.add("acme_jane", "Acme Jane")
        self.sid = self.ok("/api/sheets/new", {"target": self.acme["id"]})["sheet"]["id"]
        self.wait_sheets()
        self.drop(POSTS, COMMENTS, ACCOUNT)
        self.wait_sheets()
        self.before = {t: self.fake.values(self.sid, t) for t in ("content", "content_history", "account_daily")}

    def wipe(self):
        con = sqlite3.connect(self.data / "reddit.db")
        with con:
            for t in ("pro_items", "pro_snapshots", "account_daily", "uploads"):
                con.execute(f"DELETE FROM {t}")
        con.close()

    def test_a_computer_with_nothing_gets_the_sheets_numbers_back(self):
        self.fake.set_rows(self.sid, "content", [self.before["content"][0]] + [
            r[:6] + ["Thanks, glad it helped!"] + r[7:] if r[0] == "comment" else r for r in self.before["content"][1:]])
        self.wipe()
        got = self.ok("/api/sheets/use-sheet", {"target": self.acme["id"]})
        self.assertEqual(got["kept"]["items"], len(self.before["content"]) - 1)
        self.wait_sheets()
        c, = self.status()["clients"]
        self.assertEqual(c["last"], {"post": "2026-10-08", "comment": "2026-10-08", "account": "2026-10-08"})
        self.assertEqual(c["sheet"]["state"], "ok")
        after = {t: self.fake.values(self.sid, t) for t in self.before}
        self.assertEqual(sorted(after["content_history"]), sorted(self.before["content_history"]))
        self.assertEqual(after["account_daily"], self.before["account_daily"])
        comments = [r for r in after["content"][1:] if r[0] == "comment"]
        self.assertTrue(comments and all(r[6] == "Thanks, glad it helped!" for r in comments))

    def test_numbers_already_on_this_computer_are_never_replaced(self):
        rows = [list(r) for r in self.before["account_daily"]]
        for r in rows[1:]:
            r[4] = 1  # followers: the Sheet says 1
        self.fake.set_rows(self.sid, "account_daily", rows)
        self.ok("/api/sheets/use-sheet", {"target": self.acme["id"]})
        self.wait_sheets()
        self.assertEqual(self.fake.values(self.sid, "account_daily"), self.before["account_daily"])

    def test_a_sheet_laid_out_differently_is_refused_and_nothing_changes(self):
        self.fake.set_rows(self.sid, "content", [["Date", "Title", "Views"], ["2026-10-01", "Hi", "3"]])
        self.wipe()
        r = self.post("/api/sheets/use-sheet", {"target": self.acme["id"]})
        self.assertEqual((r.status, r.body["code"]), (422, "not_ours"))
        con = sqlite3.connect(self.data / "reddit.db")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM pro_items").fetchone()[0], 0)
        con.close()

    def test_another_accounts_rows_are_left_out(self):
        self.wipe()
        for t in self.before:
            self.fake.files[self.sid].tab(t).cells.clear()
        self.fake.set_rows(self.sid, "content", [self.before["content"][0]] + [
            [r[0], "someone_else", *r[2:]] for r in self.before["content"][1:]])
        self.fake.set_rows(self.sid, "content_history", self.before["content_history"][:1])
        self.fake.set_rows(self.sid, "account_daily", self.before["account_daily"][:1])
        r = self.post("/api/sheets/use-sheet", {"target": self.acme["id"]})
        self.assertEqual((r.status, r.body.get("code")), (422, "nothing_to_keep"), r)

if __name__ == "__main__":
    unittest.main()
