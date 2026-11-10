"""Google sign-in, the saved sign-in, retries, error wording and the Drive/Sheets helpers, against the
strict fake Google in fake_google.py."""
from __future__ import annotations

import json
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _folder in (HERE.parent, HERE):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))

from fake_google import EXCEL_MIME, FOLDER_MIME, SCOPE, FakeClock, FakeGoogle  # noqa: E402
from rt import google as g  # noqa: E402
from rt.google import Google, GoogleError, problem  # noqa: E402


def query_of(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = FakeClock()
        self.fake = FakeGoogle(clock=self.clock.now).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.logs: list[str] = []
        self.google = self.fake.client(self.dir, self.clock, sign_in=False, log=self.logs.append)

    def sign_in(self, **kw) -> dict:
        return self.fake.sign_in(self.google, **kw)

    def token(self) -> dict:
        return json.loads((self.dir / "google_token.json").read_text(encoding="utf-8"))

    def raises(self, code: str, fn, *args, **kw) -> GoogleError:
        with self.assertRaises(GoogleError) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def token_calls(self) -> int:
        return sum(1 for c in self.fake.calls if c[1] == "/token")


class SignInTests(Base):
    def test_sign_in_asks_only_for_drive_file_with_pkce(self):
        started = self.google.begin("signin", port=8765)
        q = query_of(started["url"])
        self.assertTrue(started["url"].startswith(self.fake.base + "/o/oauth2/v2/auth?"))
        self.assertEqual(q["scope"], SCOPE)
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8765/oauth")
        self.assertEqual((q["response_type"], q["code_challenge_method"]), ("code", "S256"))
        self.assertEqual((q["access_type"], q["prompt"], q["state"]), ("offline", "consent", started["state"]))
        self.assertNotIn("trigger_onepick", q)
        self.assertNotIn("login_hint", q)  # nobody signed in yet
        self.assertTrue(self.google.busy)
        result = self.google.finish(self.fake.approve(started["url"]))
        self.assertEqual(result, {"purpose": "signin", "target": None, "picked": []})
        self.assertFalse(self.google.busy)
        status = self.google.status()
        self.assertEqual((status["signed_in"], status["email"], status["name"], status["problem"]),
                         (True, "jane@example.com", "Acme Jane", None))
        self.assertTrue(status["test_mode"])
        token = self.token()
        self.assertEqual(token["permission_id"], "perm-jane")
        self.assertTrue(token["refresh_token"] and token["access_token"] and token["saved_at"].endswith("Z"))
        if os.name == "posix":
            self.assertEqual((self.dir / "google_token.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.google.last["ok"], True)
        self.assertIn("jane@example.com", self.google.last["message"])
        self.assertEqual(self.google.about(), {"email": "jane@example.com", "name": "Acme Jane",
                                               "permission_id": "perm-jane"})

    def test_choosing_a_sheet(self):
        self.sign_in()
        signed_in = self.token()
        sheet = self.fake.make_sheet(granted=False)
        self.assertEqual(self.google.check_file(sheet)["code"], "not_found")  # drive.file: not picked yet
        self.fake.pick_ids = [sheet]
        started = self.google.begin("pick", target="c-1a2b3c4d", file_id=sheet)
        q = query_of(started["url"])
        self.assertEqual((q["trigger_onepick"], q["mimetypes"], q["file_ids"]),
                         ("true", "application/vnd.google-apps.spreadsheet", sheet))
        self.assertEqual(q["login_hint"], "jane@example.com")
        # a choice makes no new lasting sign-in (Google keeps only so many per account): the saved one stays
        self.assertEqual((q["access_type"], q["prompt"]), ("online", "consent"))
        result = self.google.finish(self.fake.approve(started["url"]))
        self.assertEqual(result, {"purpose": "pick", "target": "c-1a2b3c4d", "picked": [sheet]})
        self.assertEqual(self.google.check_file(sheet), {"code": "ok", "title": "Fake Sheet", "message": None})
        # no "it worked" yet: the server says how the choice went once it has checked the file
        self.assertEqual(self.google.last["purpose"], "signin")
        self.assertEqual(self.token()["refresh_token"], signed_in["refresh_token"])
        self.assertEqual(len(self.fake.refresh_tokens), 1)
        self.fake.expire_access_tokens()
        self.assertEqual(self.google.about()["email"], "jane@example.com")  # the kept one still refreshes

    def test_choosing_a_sheet_before_signing_in_signs_in_too(self):
        sheet = self.fake.make_sheet(granted=False)
        self.fake.pick_ids = [sheet]
        started = self.google.begin("pick", target="c-1", file_id=sheet)
        self.assertEqual(query_of(started["url"])["access_type"], "offline")
        self.assertEqual(self.google.finish(self.fake.approve(started["url"]))["picked"], [sheet])
        self.assertTrue(self.token()["refresh_token"])
        self.fake.expire_access_tokens()
        self.assertEqual(self.google.about()["email"], "jane@example.com")  # still signed in an hour later

    def test_choosing_while_in_another_google_account(self):
        self.sign_in()
        before = self.token()
        self.fake.account, self.fake.pick_ids = "bolt@example.com", []
        e = self.raises("wrong_account", self.sign_in, purpose="pick", target="all")
        self.assertIn("bolt@example.com", e.message)
        self.assertIn("Reddit Tracker uses jane@example.com", e.message)
        after = self.token()
        self.assertEqual((after["permission_id"], after["refresh_token"]), ("perm-jane", before["refresh_token"]))
        self.assertFalse(self.google.last["ok"])
        # signing in (not choosing a Sheet) with another account is allowed; the page asks first
        self.sign_in()
        self.assertEqual(self.google.status()["email"], "bolt@example.com")

    def test_signing_in_as_someone_else_after_signing_out(self):
        self.sign_in()
        sheet = self.fake.make_sheet()  # set up while signed in as jane
        self.google.sign_out()
        self.fake.account = "bolt@example.com"
        self.sign_in()
        self.assertEqual((self.google.last["ok"], self.google.last["message"]), (True, (
            "You're now signed in as bolt@example.com. Your Sheets were set up with jane@example.com: sign in as "
            "jane@example.com to keep updating them, or choose each client's Sheet again.")))
        self.assertTrue(self.google.last["keep"])  # the page keeps it on screen until it's closed
        status = self.google.status()
        self.assertEqual((status["signed_in"], status["email"], status["problem"]), (True, "bolt@example.com", None))
        self.assertEqual(self.google.check_file(sheet)["code"], "not_found")  # why every Sheet stops
        self.google.sign_out()
        self.sign_in()  # bolt again: said once is enough
        self.assertEqual(self.google.last["message"], "You're signed in to Google as bolt@example.com.")
        self.assertFalse(self.google.last["keep"])
        self.google.sign_out()
        self.fake.account = "jane@example.com"  # as the message says: her Sheets work again
        self.sign_in()
        self.assertEqual(self.google.last["message"], "You're signed in to Google as jane@example.com.")
        self.assertEqual(self.google.check_file(sheet)["code"], "ok")
        self.fake.account = "bolt@example.com"  # and bolt after that is told again
        self.sign_in()
        self.assertIn("Your Sheets were set up with jane@example.com", self.google.last["message"])

    def test_use_a_different_account(self):
        """Signed in, "Use a different account": Google still hears who is signed in, and the switch is allowed."""
        self.sign_in()
        started = self.google.begin("signin")
        self.assertEqual(query_of(started["url"])["login_hint"], "jane@example.com")
        self.fake.account = "bolt@example.com"
        self.google.finish(self.fake.approve(started["url"]))
        self.assertEqual(self.google.status()["email"], "bolt@example.com")
        self.assertTrue(self.google.last["message"].startswith(
            "You're now signed in as bolt@example.com. Your Sheets were set up with jane@example.com:"))
        self.assertEqual(query_of(self.google.begin("signin")["url"])["login_hint"], "bolt@example.com")
        # a third account: the Sheets are still jane's, not bolt's
        self.fake.accounts["carol@example.com"] = {"permissionId": "perm-carol", "name": "Carol"}
        self.fake.account = "carol@example.com"
        self.sign_in()
        self.assertTrue(self.google.last["message"].startswith(
            "You're now signed in as carol@example.com. Your Sheets were set up with jane@example.com:"))
        self.assertEqual(self.fake.revokes, 0)

    def test_signing_in_again_as_someone_else_after_google_turned_it_down(self):
        self.sign_in()
        self.fake.refresh_invalid = True
        self.fake.expire_access_tokens()
        self.raises("rejected", self.google.about)
        self.fake.refresh_invalid, self.fake.account = False, "bolt@example.com"
        self.sign_in()
        self.assertIn("Your Sheets were set up with jane@example.com", self.google.last["message"])

    def test_choosing_a_sheet_after_signing_out_signs_in_too(self):
        self.sign_in()
        self.google.sign_out()
        self.fake.account = "bolt@example.com"  # nobody is signed in, so it's not "the wrong account"
        sheet = self.fake.make_sheet(granted=False)
        self.fake.pick_ids = [sheet]
        self.assertEqual(self.sign_in(purpose="pick", target="c-1", file_id=sheet)["picked"], [sheet])
        self.assertEqual(self.google.status()["email"], "bolt@example.com")
        self.fake.expire_access_tokens()
        self.assertEqual(self.google.check_file(sheet)["code"], "ok")

    def test_choosing_a_sheet_when_google_cant_be_reached_afterwards(self):
        self.sign_in()
        before = self.token()
        self.fake.fail(r"^/drive/v3/about$", drop=True, times=4)
        e = self.raises("offline", self.sign_in, purpose="pick")
        self.assertEqual(e.message, "Couldn't finish choosing the Sheet. Check the internet and try again.")
        self.assertEqual(self.token(), before)
        self.assertEqual(self.clock.slept, [0, 10, 20])

    def test_pasted_addresses(self):
        for text in ("", "hello", "https://accounts.google.com/o/oauth2/v2/auth?code=4/x&state=s",
                     "http://127.0.0.1:8765/oauth", "http://127.0.0.1:8765/oauth?state=abc"):
            self.raises("bad_paste", self.google.parse_pasted, text)
        self.assertEqual(self.google.parse_pasted("  http://127.0.0.1:8765/oauth?state=s1&code=4/abc&scope=x  "),
                         {"state": "s1", "code": "4/abc", "scope": "x"})
        self.assertEqual(self.google.parse_pasted("127.0.0.1:8765/oauth?state=s1&code=c")["code"], "c")
        self.assertEqual(self.google.parse_pasted("localhost:8765/oauth?error=access_denied&state=s")["error"],
                         "access_denied")
        self.assertEqual(self.google.parse_pasted("state=s1&code=c&picked_file_ids=A,B")["picked_file_ids"], "A,B")
        # the whole round trip, as on a Raspberry Pi opened from a laptop
        pasted = self.fake.callback_url(self.google.begin("signin")["url"])
        self.assertEqual(self.google.finish(self.google.parse_pasted(pasted))["purpose"], "signin")
        self.assertTrue(self.google.status()["signed_in"])

    def test_code_pasted_too_late(self):
        params = self.fake.approve(self.google.begin("signin")["url"])
        self.clock.t += 301  # the fake's codes last 300 s
        e = self.raises("code_expired", self.google.finish, params)
        self.assertIn("ran out of time", e.message)
        self.assertFalse(self.google.status()["signed_in"])

    def test_unknown_old_and_repeated_answers(self):
        self.raises("bad_state", self.google.finish, {"state": "made-up", "code": "4/x"})
        self.assertIsNone(self.google.last)  # nothing to tell the page about
        params = self.fake.approve(self.google.begin("signin")["url"])
        first = self.google.finish(params)
        self.assertEqual(self.google.finish(params), first)  # the same outcome, the code isn't used twice
        self.assertEqual(self.token_calls(), 1)
        # an answer that failed fails the same way again
        self.fake.deny = True
        denied = self.fake.approve(self.google.begin("signin")["url"])
        self.raises("denied", self.google.finish, denied)
        self.raises("denied", self.google.finish, denied)
        # a sign-in link older than 15 minutes
        old = self.fake.approve(self.google.begin("signin")["url"])
        self.clock.t += 16 * 60
        self.assertFalse(self.google.busy)
        self.raises("bad_state", self.google.finish, old)

    def test_at_most_five_sign_ins_wait(self):
        states = [self.google.begin("signin")["state"] for _ in range(7)]
        for state in states[:2]:
            self.raises("bad_state", self.google.finish, {"state": state, "code": "4/x"})
        self.assertEqual(self.token_calls(), 0)

    def test_access_not_allowed(self):
        self.fake.deny = True
        e = self.raises("denied", self.sign_in)
        self.assertEqual((self.google.last["ok"], self.google.last["message"]), (False, e.message))
        self.fake.deny, self.fake.grant_scope = False, ""  # the user unticked the Drive box
        self.raises("denied", self.sign_in)
        self.assertFalse(self.google.status()["signed_in"])

    def test_chooser_answer_without_state(self):
        self.sign_in()
        sheet = self.fake.make_sheet(granted=False)
        self.fake.pick_ids, self.fake.omit_state = [sheet], True
        url = self.google.begin("pick", target="all")["url"]
        params = self.fake.approve(url)
        self.assertNotIn("state", params)
        self.assertEqual(self.google.finish(params)["picked"], [sheet])
        # two choices waiting for different clients: no way to tell which one this is
        self.google.begin("pick", target="c-1")
        params = self.fake.approve(self.google.begin("pick", target="c-2")["url"])
        self.raises("bad_state", self.google.finish, params)

    def test_an_answer_without_state_is_only_taken_from_the_account_already_signed_in(self):
        sheet = self.fake.make_sheet(granted=False)
        self.fake.pick_ids, self.fake.omit_state = [sheet], True
        params = self.fake.approve(self.google.begin("pick", target="c-1")["url"])
        self.raises("bad_state", self.google.finish, params)  # nobody signed in here yet
        self.assertFalse(self.google.status()["signed_in"])
        self.fake.omit_state = False
        self.sign_in()
        self.fake.omit_state = True
        self.fake.account = next(a for a in self.fake.accounts if a != self.fake.account)
        params = self.fake.approve(self.google.begin("pick", target="c-1")["url"])
        self.raises("bad_state", self.google.finish, params)
        self.assertEqual(self.google.status()["email"], "jane@example.com")

    def test_choosing_again_after_closing_the_chooser_without_state(self):
        self.sign_in()
        sheet = self.fake.make_sheet(granted=False)
        self.fake.pick_ids, self.fake.omit_state = [sheet], True
        self.google.begin("pick", target="c-1")  # that chooser window was closed
        params = self.fake.approve(self.google.begin("pick", target="c-1")["url"])
        self.assertEqual(self.google.finish(params), {"purpose": "pick", "target": "c-1", "picked": [sheet]})
        self.assertFalse(self.google.busy)  # the abandoned one went too
        self.google.begin("pick", target="c-1")  # one left over for more than 10 minutes doesn't block either
        self.clock.t += 11 * 60
        params = self.fake.approve(self.google.begin("pick", target="c-1")["url"])
        self.assertEqual(self.google.finish(params)["picked"], [sheet])

    def test_the_same_answer_again_while_the_first_is_still_finishing(self):
        params = self.fake.approve(self.google.begin("signin")["url"])
        gate = self.fake.hold(r"^/token$")  # Google is slow to answer the first one
        first = {}
        worker = threading.Thread(target=lambda: first.update(result=self.google.finish(dict(params))))
        worker.start()
        self.assertTrue(self.fake.held.wait(5))
        threading.Timer(0.2, gate.set).start()
        self.assertEqual(self.google.finish(dict(params)), {"purpose": "signin", "target": None, "picked": []})
        worker.join(5)
        self.assertEqual(first["result"]["purpose"], "signin")
        self.assertEqual(self.token_calls(), 1)
        self.assertTrue(self.google.status()["signed_in"])

    def test_old_chooser_answer_without_state(self):
        self.sign_in()
        self.fake.omit_state = True
        params = self.fake.approve(self.google.begin("pick")["url"])
        self.clock.t += 11 * 60
        self.raises("bad_state", self.google.finish, params)

    def test_fake_google_is_strict(self):
        """The fake refuses what real Google refuses, so these tests can't pass by luck."""
        url = self.google.begin("signin")["url"]
        q = query_of(url)
        with self.assertRaises(AssertionError):  # short scope name
            self.fake.approve(url.replace(urllib.parse.quote(SCOPE, safe=""), "drive.file"))
        params = self.fake.approve(url)

        def token(**changes) -> tuple[int, dict]:
            form = {"client_id": self.fake.client_id, "client_secret": self.fake.client_secret,
                    "code": params["code"], "code_verifier": "wrong" * 10, "grant_type": "authorization_code",
                    "redirect_uri": q["redirect_uri"], **changes}
            form = {k: v for k, v in form.items() if v is not None}
            req = urllib.request.Request(self.fake.base + "/token", urllib.parse.urlencode(form).encode(),
                                         {"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        self.assertEqual(token(client_secret=None)[1]["error_description"], "client_secret is missing.")
        self.assertEqual(token()[1]["error"], "invalid_grant")  # wrong verifier (and the code is now used)
        params = self.fake.approve(url)
        self.assertEqual(token(code=params["code"], redirect_uri="http://127.0.0.1:9999/oauth")[1]["error"],
                         "redirect_uri_mismatch")
        self.assertEqual(token(code=params["code"])[1]["error"], "invalid_grant")  # used once already


class TokenTests(Base):
    def test_refresh_when_nearly_expired(self):
        self.sign_in()
        token = self.token()
        token["expires_at"] = time.time() + 30
        (self.dir / "google_token.json").write_text(json.dumps(token), encoding="utf-8")
        self.google.about()
        self.assertEqual(self.fake.refreshes, 1)
        self.assertNotEqual(self.token()["access_token"], token["access_token"])
        self.assertEqual(self.token()["refresh_token"], token["refresh_token"])  # kept when none comes back

    def test_a_full_disk_during_a_refresh_still_gives_a_token(self):
        self.sign_in()
        self.fake.expire_access_tokens()
        with mock.patch.object(g, "write_private", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.google.about()["email"], "jane@example.com")

    def test_refresh_once_when_google_says_401(self):
        self.sign_in()
        self.fake.expire_access_tokens()
        self.assertEqual(self.google.about()["email"], "jane@example.com")
        self.assertEqual(self.fake.refreshes, 1)

    def test_turned_down_sign_in(self):
        self.sign_in()
        self.fake.refresh_invalid = True
        self.fake.expire_access_tokens()
        self.raises("rejected", self.google.about)
        status = self.google.status()
        self.assertEqual((status["signed_in"], status["problem"], status["email"]),
                         (False, "rejected", "jane@example.com"))
        calls = len(self.fake.calls)
        self.raises("rejected", self.google.access_token)
        self.assertEqual(len(self.fake.calls), calls)  # no new call to Google
        self.assertEqual(query_of(self.google.begin("signin")["url"])["login_hint"], "jane@example.com")
        self.fake.refresh_invalid = False
        self.sign_in()
        self.assertEqual(self.google.status()["problem"], None)

    def test_google_turns_down_this_copys_app_file(self):
        """The publisher reset the secret, or deleted the Google app this copy's google_app.json names."""
        self.sign_in()
        self.fake.client_id, self.fake.client_secret = "newer-client.apps.googleusercontent.com", "newer-secret"
        self.fake.expire_access_tokens()
        e = self.raises("app_refused", self.google.about)
        self.assertIn("Download the newest Reddit Tracker", e.message)
        status = self.google.status()
        self.assertEqual((status["configured"], status["signed_in"], status["problem"]),
                         (False, False, "app_refused"))
        refreshes = self.token_calls()
        self.raises("app_refused", self.google.access_token)  # it stays that way: no more calls to Google
        self.raises("app_refused", self.google.begin, "signin")
        self.assertEqual(self.token_calls(), refreshes)
        self.assertFalse(self.token().get("rejected"))  # the newest Reddit Tracker may still use it
        # signing in when Google turns the app down says so too
        (self.dir / "another").mkdir()
        another = self.fake.client(self.dir / "another", self.clock, sign_in=False)  # the fake's app file
        self.fake.client_secret = "reset-by-the-publisher"
        e = self.raises("app_refused", self.fake.sign_in, another)
        self.assertEqual((another.status()["problem"], another.last["message"]), ("app_refused", e.message))

    def test_a_sign_in_made_with_an_older_app_file(self):
        """A newer Reddit Tracker comes with a new google_app.json: the old sign-in isn't for it."""
        self.sign_in()
        self.fake.client_id, self.fake.client_secret = "newer-client.apps.googleusercontent.com", "newer-secret"
        newer = self.fake.client(self.dir, self.clock, sign_in=False)  # the same data folder, the new app file
        self.fake.expire_access_tokens()
        self.raises("rejected", newer.about)
        status = newer.status()
        self.assertEqual((status["configured"], status["signed_in"], status["problem"]), (True, False, "rejected"))
        self.fake.sign_in(newer)
        self.assertEqual(newer.about()["email"], "jane@example.com")

    def test_remove_access_cancels_the_sign_in_at_google(self):
        self.sign_in()
        self.assertTrue(self.google.remove_access())
        self.assertEqual(self.fake.revokes, 1)
        self.assertFalse(self.google.status()["signed_in"])
        self.assertFalse(self.google.remove_access())  # nothing left to cancel
        self.assertEqual(self.fake.revokes, 1)

    def test_remove_access_signs_out_even_when_google_cant_be_reached(self):
        self.sign_in()
        self.fake.fail("/revoke", status=500, times=10)
        self.assertFalse(self.google.remove_access())
        self.assertFalse(self.google.status()["signed_in"])
        self.assertNotIn("refresh_token", self.token())

    def test_sign_out_never_revokes(self):
        self.sign_in()
        with mock.patch.object(g, "write_private", wraps=g.write_private) as write:
            self.google.sign_out()
            self.google.sign_out()  # twice is fine
        self.assertEqual(write.call_count, 1)  # replaced in one step
        # only who it was is kept (nothing that signs in), so a later sign-in as someone else can say why
        self.assertEqual(self.token(), {"signed_out": True, "email": "jane@example.com", "name": "Acme Jane",
                                        "permission_id": "perm-jane"})
        if os.name == "posix":
            self.assertEqual((self.dir / "google_token.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.fake.revokes, 0)
        status = self.google.status()
        self.assertEqual((status["signed_in"], status["problem"]), (False, None))
        calls = len(self.fake.calls)
        self.raises("signed_out", self.google.about)
        self.raises("signed_out", self.google.access_token, force=True)
        self.assertEqual(len(self.fake.calls), calls)  # nothing sent to Google
        self.assertNotIn("login_hint", query_of(self.google.begin("signin")["url"]))  # Google asks which account
        self.assertEqual(query_of(self.google.begin("pick", target="c-1")["url"])["access_type"], "offline")
        self.sign_in()  # the same account again: an ordinary sign-in
        self.assertEqual(self.google.last["message"], "You're signed in to Google as jane@example.com.")
        self.assertNotIn("signed_out", self.token())
        self.fake.expire_access_tokens()
        self.assertEqual(self.google.about()["email"], "jane@example.com")

    def test_sign_out_with_nothing_or_a_damaged_sign_in_saved(self):
        token = self.dir / "google_token.json"
        self.google.sign_out()
        self.assertFalse(token.exists())
        token.write_text("{not json", encoding="utf-8")
        self.google.sign_out()
        self.assertFalse(token.exists())
        self.sign_in()
        with mock.patch.object(g, "write_private", side_effect=OSError(28, "No space left on device")):
            self.google.sign_out()  # a full disk: signed out all the same
        self.assertFalse(token.exists())
        self.raises("signed_out", self.google.access_token)

    def test_app_file(self):
        missing = Google(self.dir, None)
        self.assertEqual((missing.configured, missing.status()["problem"]), (False, "no_app_file"))
        self.raises("no_app_file", missing.begin, "signin")
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(Google(self.dir, bad).status()["problem"], "bad_app_file")
        bad.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
        self.assertEqual(Google(self.dir, bad).status()["problem"], "bad_app_file")
        flat = self.dir / "flat.json"
        flat.write_text(json.dumps({"client_id": "x", "client_secret": "y"}), encoding="utf-8")
        status = Google(self.dir, flat).status()
        self.assertEqual((status["configured"], status["problem"]), (True, None))
        self.assertNotIn("test_mode", status)

    def test_test_address_only_on_this_computer(self):
        logs = []
        with mock.patch.dict(os.environ, {"RT_GOOGLE_BASE": "http://evil.example.com:80"}):
            other = Google(self.dir, self.fake.write_app_file(self.dir), log=logs.append)
        self.assertFalse(other.test_mode)
        self.assertEqual(other.token_url, "https://oauth2.googleapis.com/token")
        self.assertTrue(other.begin("signin")["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertTrue(logs and "ignored" in logs[0])
        with mock.patch.dict(os.environ, {"RT_GOOGLE_BASE": "http://localhost:8123"}):
            self.assertTrue(Google(self.dir, None).test_mode)

    def test_ssl_context_uses_macos_certificates_when_python_has_none(self):
        context = g.ssl_context()
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
        paths = ssl.get_default_verify_paths()._replace(openssl_cafile="/nowhere/cert.pem")
        loaded = []
        g.ssl_context.cache_clear()
        self.addCleanup(g.ssl_context.cache_clear)
        with mock.patch.object(g.sys, "platform", "darwin"), \
                mock.patch.object(g.ssl, "get_default_verify_paths", return_value=paths), \
                mock.patch.object(g.os.path, "exists", side_effect=lambda p: p == "/etc/ssl/cert.pem"), \
                mock.patch.object(ssl.SSLContext, "load_verify_locations", lambda self, path: loaded.append(path)):
            g.ssl_context()
        self.assertEqual(loaded, ["/etc/ssl/cert.pem"])


class ErrorTests(Base):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.sheet = self.fake.make_sheet()

    def answer(self, **fail) -> tuple[str, str, bool]:
        """Google's answer to a meta read, made to fail like this, as (code, message, offline)."""
        self.clock.slept.clear()
        self.fake.fail(r"^/v4/spreadsheets/", **fail)
        with self.assertRaises(GoogleError) as caught:
            self.google.get_meta(self.sheet)
        self.assertTrue(all(r.times == 0 for r in self.fake._rules), "every injected answer was used")
        return problem(caught.exception)

    def test_error_mapping(self):
        self.assertEqual(self.answer(status=403)[0], "not_found")
        self.assertEqual(self.answer(status=404)[0], "not_found")
        self.assertIn("Choose it again", self.answer(status=404)[1])
        self.assertEqual(self.answer(status=403, reason="SERVICE_DISABLED", reason_in="details")[0], "api_off")
        self.assertEqual(self.answer(status=403, reason="accessNotConfigured", reason_in="errors")[0], "api_off")
        self.assertIn("(Sheets, Drive or Picker)", self.answer(status=403, reason="SERVICE_DISABLED")[1])
        self.assertEqual(self.answer(status=403, message="You are trying to edit a protected cell or object.")[0],
                         "protected")
        # the owner's Drive is full: Google won't let anyone change the Sheet (a new one is drive_full)
        code, message, _ = self.answer(status=403, reason="storageQuotaExceeded")
        self.assertEqual(code, "storage_full")
        self.assertIn("out of storage", message)
        self.assertEqual(self.answer(status=403, message="The user's Drive storage quota has been exceeded.")[0],
                         "storage_full")
        self.assertEqual(self.answer(status=400, message="This action would increase the number of cells in the "
                                     "workbook above the limit of 10000000 cells.")[0], "too_big")
        code, message, offline = self.answer(status=400, message="Invalid range")
        self.assertEqual((code, offline), ("other", False))
        self.assertTrue(message.startswith("Google gave an unexpected answer (error 400: Invalid range)"), message)

    def test_quota_waits_then_gives_up(self):
        self.assertEqual(self.answer(status=429, times=4)[0], "quota")
        self.assertEqual(self.clock.slept, [20, 40, 65])
        self.assertEqual(self.answer(status=403, reason="userRateLimitExceeded", times=4)[0], "quota")
        self.assertEqual(self.clock.slept, [20, 40, 65])
        self.assertEqual(self.answer(status=429, times=4, retry_after="7")[0], "quota")
        self.assertEqual(self.clock.slept, [7, 7, 7])
        self.assertEqual(self.answer(status=429, times=4, retry_after="500")[0], "quota")
        self.assertEqual(self.clock.slept, [120, 120, 120])

    def test_google_down_and_offline(self):
        code, message, offline = self.answer(status=503, times=4)
        self.assertEqual((code, offline), ("google_down", False))
        self.assertIn("temporary problem (error 503)", message)
        self.assertEqual(self.clock.slept, [0, 10, 20])
        code, message, _ = self.answer(status=502, html=True, times=4)  # Google's front door, as a web page
        self.assertEqual(code, "google_down")
        self.assertNotIn("<", message)
        code, message, offline = self.answer(drop=True, times=4)
        self.assertEqual((code, offline), ("offline", True))
        self.assertIn("check the internet", message.lower())

    def test_busy_google_is_retried(self):
        self.fake.fail(r"^/v4/spreadsheets/", status=503, times=2)
        self.assertEqual(self.google.get_meta(self.sheet)["properties"]["title"], "Fake Sheet")
        self.assertEqual(self.clock.slept, [0, 10])
        self.clock.slept.clear()
        self.fake.fail(r"^/v4/spreadsheets/", drop=True, times=1)
        self.google.get_meta(self.sheet)
        self.assertEqual(self.clock.slept, [0])

    def test_certificate_problem_is_not_retried(self):
        broken = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with mock.patch.object(self.google._opener, "open", side_effect=broken):
            e = self.raises("cert", self.google.get_meta, self.sheet)
        self.assertIn("date and time", e.message)
        self.assertEqual(self.clock.slept, [])

    def test_problem_for_errors_that_are_not_googles(self):
        code, message, offline = problem(PermissionError("data/x.csv"))
        self.assertEqual((code, offline), ("other", False))
        self.assertIn("data/x.csv", message)
        self.assertEqual(problem(ConnectionResetError("reset"))[::2], ("offline", True))
        self.assertEqual(problem(urllib.error.URLError(ssl.SSLCertVerificationError("bad")))[0], "cert")

    def test_check_file(self):
        sheet = self.fake.files[self.sheet]
        self.assertEqual(self.google.check_file(self.sheet)["code"], "ok")
        sheet.can_edit = False
        self.assertEqual(self.google.check_file(self.sheet)["code"], "read_only")
        sheet.can_edit, sheet.trashed = True, True
        self.assertEqual(self.google.check_file(self.sheet)["code"], "trashed")
        excel = self.fake.add_file("Report.xlsx", EXCEL_MIME)
        self.assertEqual(self.google.check_file(excel)["code"], "excel_file")
        folder = self.fake.add_file("Clients", FOLDER_MIME)
        self.assertEqual(self.google.check_file(folder)["code"], "not_a_sheet")
        hidden = self.fake.make_sheet(granted=False)
        self.assertEqual(self.google.check_file(hidden)["code"], "not_found")
        self.assertEqual(self.google.check_file("missing" * 4)["code"], "not_found")
        query = next(c[2] for c in reversed(self.fake.calls) if c[1].startswith("/drive/v3/files/"))
        self.assertEqual(query, {"fields": "name,mimeType,trashed,capabilities/canEdit", "supportsAllDrives": "true"})
        self.fake.fail(r"^/drive/v3/files/", status=403, reason="SERVICE_DISABLED")
        self.raises("api_off", self.google.check_file, self.sheet)
        self.fake.fail(r"^/drive/v3/files/", drop=True, times=4)
        self.raises("offline", self.google.check_file, self.sheet)
        self.fake.refresh_invalid = True
        self.fake.expire_access_tokens()
        self.raises("rejected", self.google.check_file, self.sheet)

    def test_create_spreadsheet(self):
        tabs = [("content", ["kind", "account", "views"]), ("account_daily", ["account", "date"]),
                ("content_history", ["export_date", "item_key"])]
        made = self.google.create_spreadsheet("Reddit Tracker – Acme Jane", tabs)
        self.assertEqual(made["url"], f"https://docs.google.com/spreadsheets/d/{made['id']}/edit")
        self.assertEqual(made["title"], "Reddit Tracker – Acme Jane")
        self.assertEqual(self.fake.tab_names(made["id"]), ["content", "account_daily", "content_history"])
        for name, header in tabs:
            tab = self.fake.files[made["id"]].tab(name)
            self.assertEqual(self.fake.data(made["id"], name), [header])
            self.assertEqual((tab.frozen, tab.bold_rows, (tab.rows, tab.cols)), (1, {0}, (2, len(header))))
        self.assertTrue(all(c[2].get("valueInputOption") == "RAW" for c in self.fake.writes() if c[0] == "PUT"))
        self.assertEqual(self.google.check_file(made["id"])["code"], "ok")
        self.fake.fail(r"^/v4/spreadsheets$", status=403, reason="storageQuotaExceeded")
        e = self.raises("drive_full", self.google.create_spreadsheet, "Another", tabs)
        self.assertIn("Google Drive is full", e.message)

    def test_a_new_sheet_is_kept_when_only_its_header_rows_fail(self):
        tabs = [("content", ["kind", "account", "views"])]
        self.fake.fail(r":batchUpdate$", drop=True, times=4)  # the internet goes after the Sheet was made
        made = self.google.create_spreadsheet("Reddit Tracker – Acme Jane", tabs)
        self.assertEqual(self.fake.tab_names(made["id"]), ["content"])  # so its id is saved, not left behind
        self.assertTrue(any("header rows" in line for line in self.logs))

    def test_numbers_google_cant_take(self):
        calls = len(self.fake.calls)
        e = self.raises("other", self.google.values_update, self.sheet, "'Sheet1'!A1", [[float("inf"), 1.5]])
        self.assertIn("isn't a real number", e.message)
        self.assertEqual(len(self.fake.calls), calls)  # nothing sent

    def test_files_are_replaced_with_the_windows_safe_replace(self):
        with mock.patch.object(g.paths, "replace", wraps=g.paths.replace) as replace:
            g.write_private(self.dir / "x.json", b"{}")
        self.assertEqual(replace.call_args[0][1], self.dir / "x.json")
        self.assertEqual((self.dir / "x.json").read_bytes(), b"{}")


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = FakeClock()
        self.fake = FakeGoogle(clock=self.clock.now, write_limit=60).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.google = self.fake.client(Path(tmp.name), self.clock)
        self.sheet = self.fake.make_sheet(tabs={"content": (5, 3)})

    def write(self, times: int) -> None:
        for i in range(times):
            self.google.values_update(self.sheet, "'content'!A1", [[f"v{i}"]])

    def test_writes_wait_instead_of_hitting_googles_limit(self):
        self.write(130)
        self.assertEqual(self.fake.rate_limited, 0)
        self.assertEqual(len(self.clock.slept), 2)  # before the 51st and the 101st write
        self.assertGreater(self.clock.t - 1_000_000.0, 119)

    def test_a_slow_down_answer_is_waited_out(self):
        self.google._limiter.limit = 1000  # as if the limiter were missing
        self.write(70)
        self.assertGreater(self.fake.rate_limited, 0)
        self.assertEqual(self.fake.data(self.sheet, "content"), [["v69", "", ""]])


if __name__ == "__main__":
    unittest.main()
