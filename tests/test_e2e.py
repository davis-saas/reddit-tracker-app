"""The whole user journey over HTTP: reddit_tracker.py's main() runs in a thread with a temporary data
folder, and RT_GOOGLE_BASE points it at the strict fake Google. Synthetic clients only."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import queue
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _folder in (HERE.parent, HERE):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))

import builders as b  # noqa: E402
import reddit_tracker  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402
from rt import server, store  # noqa: E402
from rt.settings import Settings  # noqa: E402

D1, D2, D3 = "2026-10-08", "2026-10-11", "2026-10-14"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def acme_files(day: str, times: int = 1) -> list[tuple[str, str]]:
    """Acme Jane's three downloads of `day`; `times` scales the views (newer numbers)."""
    posts = b.post_rows(20, day=day, newest="2026-10-07", prefix="A")
    comments = b.comment_rows(30, day=day, newest="2026-10-07", prefix="a")
    for r in posts + comments:
        r["Total Views"] *= times
    start = b.shift(day, -59)
    return [("acme_jane Post Insights Report.csv", b.report("post", posts)),
            ("acme_jane Comment Insights Report.csv", b.report("comment", comments)),
            ("acme_jane Account Performance Report.csv", b.report("account", b.day_rows(start, 60, views=3)))]


def bolt_files(day: str) -> list[tuple[str, str]]:
    return [("bolt_bikes Post Insights Report.csv", b.report("post", b.post_rows(6, day=day, prefix="B",
                                                                                sub="r/bikes"))),
            ("bolt_bikes Comment Insights Report.csv", b.report("comment", b.comment_rows(4, day=day, prefix="b")))]


class Journey(unittest.TestCase):
    network = False

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "data"
        self.fake = FakeGoogle().__enter__()
        self.addCleanup(self.fake.__exit__)
        app_file = self.fake.write_app_file(Path(tmp.name))
        env = mock.patch.dict(os.environ, {"RT_GOOGLE_BASE": self.fake.base, "RT_GOOGLE_APP_FILE": str(app_file)})
        env.start()
        self.addCleanup(env.stop)
        limit = mock.patch("rt.google.WRITES_PER_MINUTE", 100_000)  # real clock here; test_sheets tests the limit
        limit.start()
        self.addCleanup(limit.stop)
        self.out = io.StringIO()
        quiet = contextlib.redirect_stdout(self.out)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.port = free_port()
        argv = ["--no-browser", "--data", str(self.data), "--port", str(self.port)] + (["--network"] if self.network
                                                                                         else [])
        ready: queue.Queue = queue.Queue()
        self.exit_code = None

        def run():
            real = server.create  # bind to 127.0.0.1 even in network mode: nothing outside this computer
            with mock.patch.object(server, "create", lambda app, port, host=None: real(app, port, "127.0.0.1")):
                self.exit_code = server.main(reddit_tracker.parse(argv), on_ready=ready.put)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.srv = ready.get(timeout=10)
        self.host = f"127.0.0.1:{self.port}"

        def stop():
            self.wait_sheets()
            self.srv.shutdown()
            thread.join(10)
            self.assertEqual(self.exit_code, 0)
            with self.assertRaises(sqlite3.ProgrammingError):  # main() closed the database it held open
                self.srv.app._held.execute("SELECT 1")
        self.addCleanup(stop)

    # ---------------------------------------------------------------- like the page and the browser

    def call(self, method: str, path: str, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": self.host, "Cookie": self.local_cookie()}  # what opening the start file's link sets
        if method == "POST":
            h.update({"Content-Type": "application/json", "X-RT": "1", "Origin": f"http://{h['Host']}"})
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body or {}).encode("utf-8") if method == "POST" else None,
                  headers={k: v for k, v in h.items() if v is not None})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if "json" in (r.getheader("Content-Type") or "") else raw.decode("utf-8")), r

    def local_cookie(self) -> str:
        return f"rt_local={Settings(self.data / 'settings.json', self.data / 'backups').option('local_key')}"

    def ok(self, path: str, body=None, **headers) -> dict:
        status, answer, _ = self.call("POST", path, body, headers)
        self.assertEqual(status, 200, answer)
        return answer

    def status(self, **headers) -> dict:
        code, answer, _ = self.call("GET", "/api/status", headers=headers)
        self.assertEqual(code, 200, answer)
        return answer

    def browse(self, url: str) -> str:
        """Open an address like a browser does (following Google's redirect back to /oauth)."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.addheaders = [("Cookie", self.local_cookie())]  # the browser that opened the page has its key
        with opener.open(url, timeout=30) as r:
            return r.read().decode("utf-8")

    def sign_in(self, purpose: str = "signin", **body) -> str:
        url = self.ok("/api/google/begin", {"purpose": purpose, **body})["url"]
        return self.browse(url)

    def drop(self, named: list[tuple[str, str]], expect_ready: int | None = None) -> dict:
        files = [b.dropped(f"f{i}", name, text, modified=b.modified_ms(D1)) for i, (name, text) in enumerate(named)]
        plan = self.ok("/api/files/preview", {"files": files})
        ready = [{**f, "expect": p["client"]} for f, p in zip(files, plan["files"]) if p["import"]]
        self.assertEqual(len(ready), len(files) if expect_ready is None else expect_ready, plan["files"])
        return self.ok("/api/files/import", {"files": ready})

    def wait_sheets(self) -> dict:
        for _ in range(1500):
            s = self.status()
            if not s["sheets"]["running"]:
                return s
            time.sleep(0.01)
        self.fail("The Sheets update didn't finish")

    def assert_sheet(self, sheet_id: str, users: set) -> None:
        con = store.open_read(self.data / "reddit.db")
        try:
            tables = {tab: list(rows) for tab, rows in store.build_tables(con, users).items()}
        finally:
            con.close()
        for tab, rows in tables.items():
            self.assertEqual(self.fake.data(sheet_id, tab), [["" if v is None else v for v in r] for r in rows], tab)
            self.assertEqual(self.fake.grid(sheet_id, tab), (max(len(rows), 2), len(rows[0])), tab)  # trimmed

    def card(self, cid: str) -> dict:
        return next(c for c in self.status()["clients"] if c["id"] == cid)["sheet"]


class LocalJourneyTests(Journey):
    def test_the_whole_journey(self):
        s = self.status()
        self.assertTrue(s["first_run"] and s["google"]["configured"] and s["google"]["test_mode"])
        self.assertFalse(s["google"]["signed_in"])
        self.assertIn("TEST MODE", self.out.getvalue())
        self.assertIn("Test mode: Google is simulated", self.call("GET", "/")[1])
        self.assertFalse(self.call("GET", "/api/ping")[1]["network"])
        self.assertTrue((self.data / "reddit.db-wal").exists())  # the database stays open while it runs

        # 1. Sign in: Google sends the browser back to /oauth
        self.assertIn("You're signed in.", self.sign_in())
        s = self.status()
        self.assertEqual((s["google"]["signed_in"], s["google"]["email"]), (True, "jane@example.com"))
        self.assertTrue(s["google"]["last"]["ok"])

        # 2. Add a client and make its Sheet
        acme = self.ok("/api/clients/add", {"rev": s["rev"], "username": "https://www.reddit.com/user/acme_jane/"})
        acme_id = acme["client"]["id"]
        self.assertEqual(acme["client"]["name"], "acme jane")
        acme = self.ok("/api/clients/update", {"rev": acme["rev"], "id": acme_id, "name": "Acme Jane"})
        sheet = self.ok("/api/sheets/new", {"target": acme_id})["sheet"]
        self.assertEqual(sheet["title"], "Reddit Tracker – Acme Jane")
        self.wait_sheets()
        self.assert_sheet(sheet["id"], {"acme_jane"})  # headers only so far

        # 3. Drop the three downloads, preview, import: the Sheet gets the numbers
        r = self.drop(acme_files(D1))
        self.assertEqual(r["imported"], 3)
        self.assertIn(r["sheets"], ("started", "queued"))
        self.assertEqual(r["receipt"][0]["lines"], ["20 posts (20 new)", "30 comments (30 new)",
                                                    "Account numbers up to Thu 8 Oct"])
        s = self.wait_sheets()
        self.assert_sheet(sheet["id"], {"acme_jane"})
        self.assertEqual(len(self.fake.data(sheet["id"], "content")), 1 + 50)
        self.assertFalse(s["first_run"] or s["sheets"]["pending"])
        self.assertEqual(self.card(acme_id)["state"], "ok")
        self.assertEqual(next(c for c in s["clients"] if c["id"] == acme_id)["last"],
                         {"post": D1, "comment": D1, "account": D1})

        # 4. Newer downloads with more views: updated in place, no duplicates
        r = self.drop(acme_files(D2, times=3))
        self.assertEqual(r["receipt"][0]["lines"], ["20 posts", "30 comments", "Account numbers up to Sun 11 Oct"])
        self.wait_sheets()
        self.assert_sheet(sheet["id"], {"acme_jane"})
        content = self.fake.data(sheet["id"], "content")
        self.assertEqual(len(content), 1 + 50)
        views = content[0].index("views")
        self.assertEqual(sorted(row[views] for row in content[1:] if row[0] == "post"),
                         [3 * (i + 1) for i in range(20)])
        self.assertEqual(len(self.fake.data(sheet["id"], "content_history")), 1 + 100)
        # the same files again: nothing new
        files = [b.dropped(f"f{i}", n, t) for i, (n, t) in enumerate(acme_files(D2, times=3))]
        again = self.ok("/api/files/preview", {"files": files})
        self.assertEqual({f["status"] for f in again["files"]}, {"already"})

        # 5. A second client chooses an existing Sheet in Google's file chooser
        bolt = self.ok("/api/clients/add", {"rev": self.status()["rev"], "username": "bolt_bikes",
                                            "name": "Bolt Bikes"})["client"]
        theirs = self.fake.make_sheet("Bolt numbers", granted=False)
        self.fake.pick_ids = [theirs]
        self.assertIn("Got your Sheet “Bolt numbers”.", self.sign_in("pick", target=bolt["id"]))
        self.wait_sheets()
        self.assertEqual((self.card(bolt["id"])["id"], self.card(bolt["id"])["state"]), (theirs, "ok"))
        self.assertEqual(self.fake.tab_names(theirs), ["content", "account_daily", "content_history"])
        self.drop(bolt_files(D2))
        self.wait_sheets()
        self.assert_sheet(theirs, {"bolt_bikes"})

        # 6. The All clients Sheet, and pausing a client takes its rows out of it
        everyone = self.ok("/api/sheets/new", {"target": "all"})["sheet"]
        self.assertEqual(everyone["title"], "Reddit Tracker – All clients")
        self.wait_sheets()
        self.assert_sheet(everyone["id"], {"acme_jane", "bolt_bikes"})
        self.assertEqual(self.status()["all_sheet"]["state"], "ok")
        bolt_rows = self.fake.data(theirs, "content")
        paused = self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": bolt["id"], "paused": True})
        self.assertIn(paused["sheets"], ("started", "queued"))
        self.wait_sheets()
        self.assert_sheet(everyone["id"], {"acme_jane"})
        self.assertEqual(self.fake.data(theirs, "content"), bolt_rows)  # a paused client's Sheet is left alone
        plan = self.ok("/api/files/preview", {"files": [b.dropped("p", *bolt_files(D3)[0])]})
        self.assertEqual(plan["files"][0]["status"], "paused")
        self.ok("/api/clients/pause", {"rev": self.status()["rev"], "id": bolt["id"], "paused": False})
        self.wait_sheets()
        self.assert_sheet(everyone["id"], {"acme_jane", "bolt_bikes"})

        # 7. Signed out: imports still work and wait for the Sheets
        self.ok("/api/google/signout")
        r = self.drop(acme_files(D3, times=5))
        self.assertEqual(r["sheets"], "not_signed_in")
        s = self.status()
        self.assertTrue(s["sheets"]["pending"])
        self.assertEqual((self.card(acme_id)["state"], self.card(acme_id)["fix"]), ("waiting", "signin"))
        con = store.open_read(self.data / "reddit.db")
        try:
            newest = store.build_tables(con, {"acme_jane"}, "content")
        finally:
            con.close()
        self.assertNotEqual(self.fake.data(sheet["id"], "content")[1:],
                            [["" if v is None else v for v in r] for r in newest["content"][1:]])

        # 8. Signing in again catches every Sheet up
        self.assertIn("You're signed in.", self.sign_in())
        s = self.wait_sheets()
        self.assertFalse(s["sheets"]["pending"])
        self.assert_sheet(sheet["id"], {"acme_jane"})
        self.assert_sheet(everyone["id"], {"acme_jane", "bolt_bikes"})
        self.assertEqual({c["sheet"]["state"] for c in s["clients"]} | {s["all_sheet"]["state"]}, {"ok"})
        log = (self.data / "log.txt").read_text(encoding="utf-8")
        self.assertIn("GET /oauth 200", log)
        self.assertNotIn("code=", log)
        self.assertEqual(len(list((self.data / "uploads").iterdir())), 3 + 3 + 2 + 3)
        self.assertTrue(list((self.data / "backups").glob("reddit-before-import.*.db")))


class RemoteJourneyTests(Journey):
    network = True

    def lan(self, method: str, path: str, body=None):
        host = f"raspberrypi.local:{self.port}"
        headers = {"Host": host, "Cookie": f"rt_key={self.key}"}
        if method == "POST":
            headers["Origin"] = f"http://{host}"
        with mock.patch.object(server, "peer_is_loopback", return_value=False):
            return self.call(method, path, body, headers)

    def lan_ok(self, path: str, body=None) -> dict:
        status, answer, _ = self.lan("POST", path, body)
        self.assertEqual(status, 200, answer)
        return answer

    def test_sign_in_by_pasting_and_the_wrong_account(self):
        self.key = Settings(self.data / "settings.json", self.data / "backups").option("access_key")
        self.assertNotIn(self.key, self.out.getvalue())  # not a window someone's looking at: the key stays out
        self.assertIn("?key=… (run with --show-link to see it)", self.out.getvalue())
        key, self.key = self.key, "wrong-key"
        self.assertEqual(self.lan("GET", "/api/status")[0], 403)
        self.key = key
        with mock.patch.object(server, "peer_is_loopback", return_value=False):
            code, _, r = self.call("GET", f"/?key={self.key}", headers={"Host": f"raspberrypi.local:{self.port}"})
        self.assertEqual((code, r.getheader("Location")), (302, "/"))
        self.assertIn(f"rt_key={self.key}", r.getheader("Set-Cookie"))
        s = self.lan("GET", "/api/status")[1]
        self.assertTrue(s["app"]["remote"] and s["app"]["network"])
        self.assertTrue(self.call("GET", "/api/ping")[1]["network"])  # a second --network start leaves it be
        self.assertTrue(all(link.endswith(f":{self.port}/?key={self.key}") for link in s["app"]["links"]))

        # Sign in from a phone: Google lands on the phone's own 127.0.0.1, so the address is pasted back
        url = self.lan_ok("/api/google/begin", {"purpose": "signin"})["url"]
        pasted = self.fake.callback_url(url)
        self.assertTrue(pasted.startswith(f"http://127.0.0.1:{self.port}/oauth?"))
        self.assertEqual(self.lan_ok("/api/google/finish", {"url": pasted})["purpose"], "signin")
        s = self.lan("GET", "/api/status")[1]
        self.assertEqual((s["google"]["signed_in"], s["google"]["email"]), (True, "jane@example.com"))
        # without http:// it works too, and the same address twice gives the same answer
        url = self.lan_ok("/api/google/begin", {"purpose": "signin"})["url"]
        pasted = self.fake.callback_url(url).replace("http://", "")
        self.assertTrue(self.lan_ok("/api/google/finish", {"url": pasted})["ok"])
        self.assertTrue(self.lan_ok("/api/google/finish", {"url": pasted})["ok"])

        # Choosing a Sheet while signed in to Google as someone else: refused, the sign-in stays
        acme = self.lan_ok("/api/clients/add", {"rev": s["rev"], "username": "acme_jane"})["client"]
        theirs = self.fake.make_sheet("Acme numbers", granted=False)
        self.fake.pick_ids = [theirs]
        self.fake.account = "bolt@example.com"
        url = self.lan_ok("/api/google/begin", {"purpose": "pick", "target": acme["id"]})["url"]
        code, answer, _ = self.lan("POST", "/api/google/finish", {"url": self.fake.callback_url(url)})
        self.assertEqual((code, answer["code"]), (409, "wrong_account"))
        self.assertIn("bolt@example.com", answer["error"])
        self.assertIn("jane@example.com", answer["error"])
        s = self.lan("GET", "/api/status")[1]
        self.assertEqual(s["google"]["email"], "jane@example.com")
        self.assertFalse(s["google"]["last"]["ok"])
        self.assertIsNone(s["clients"][0]["sheet"]["id"])
        # picked again with the right account
        self.fake.account = "jane@example.com"
        url = self.lan_ok("/api/google/begin", {"purpose": "pick", "target": acme["id"]})["url"]
        done = self.lan_ok("/api/google/finish", {"url": self.fake.callback_url(url)})
        self.assertEqual(done["picked_title"], "Acme numbers")
        self.wait_sheets()
        self.assertEqual(self.lan("GET", "/api/status")[1]["clients"][0]["sheet"]["state"], "ok")
        # the sign-in answer itself never reaches the Raspberry Pi's /oauth from another device
        self.assertEqual(self.lan("GET", "/oauth?code=x&state=y")[0], 403)


if __name__ == "__main__":
    unittest.main()
