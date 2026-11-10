"""Preview and import of dropped files (rt/engine.py): whose file it is, guards, warnings, receipt."""
from __future__ import annotations

import datetime as dt
import errno
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _p in (str(HERE.parent), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import builders as b  # noqa: E402
from builders import ACCOUNT_CSV, COMMENTS_CSV, POSTS_CSV, dropped  # noqa: E402
from rt import csvfiles, engine, store  # noqa: E402

TODAY = dt.date(2026, 10, 12)
ACME = b.client("c-acme", "Acme Jane", "acme_jane", sheet_id="SHEET_A")
BOLT = b.client("c-bolt", "Bolt Bikes", "bolt_bikes", sheet_id="SHEET_B")


class EngineCase(unittest.TestCase):
    clients = [ACME, BOLT]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "reddit.db"
        self.backups, self.uploads = self.dir / "backups", self.dir / "uploads"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)

    def preview(self, files: list[dict]) -> dict:
        return engine.preview(self.con, self.clients, files, TODAY)

    def run_import(self, files: list[dict], **kw) -> dict:
        return engine.run_import(self.con, self.clients, files, TODAY, self.backups, self.uploads, **kw)

    def store_file(self, name: str, text: str, client_id: str, **kw) -> dict:
        """Import one file the way the page does: as the client the preview showed."""
        return self.run_import([dropped("s", name, text, expect=client_id, **kw)])

    def file(self, plan: dict, name: str) -> dict:
        return next(f for f in plan["files"] if f["name"] == name)

    def one(self, name: str, text: str, **kw) -> dict:
        """The preview of a single dropped file."""
        return self.preview([dropped("x", name, text, **kw)])["files"][0]


class NameRouting(EngineCase):
    clients = [b.client("c-1", "Acme Jane", "Acme_Jane", sheet_id="SHEET_A"), b.client("c-2", "Bolt", "bolt_bikes"),
               b.client("c-3", "EU", "Acme_Jane_EU", paused=True), b.client("c-4", "Cedar Lane", "Cedar_Lane", paused=True),
               b.client("c-5", "Zed", "zed_real", paused=True)]

    def route(self, name: str) -> tuple:
        how, who = engine.name_route(name, self.clients)
        return how, who["id"] if isinstance(who, dict) else who

    def test_name_routing(self):
        # a username in Reddit's own file name decides alone: one that only starts like a client's is a stranger
        self.assertEqual(self.route("Acme_Jane2 Post Insights Report.csv"), ("new_client", "Acme_Jane2"))
        self.assertEqual(self.route("Acme_Jane_EU Comment Insights Report.csv"), ("paused", "c-3"))
        self.assertEqual(self.route("acme_jane Post Insights Report (1).csv"), ("client", "c-1"))
        self.assertEqual(self.route("2026-11-03_091502_123456_Acme_Jane Post Insights Report.csv"), ("client", "c-1"))
        self.assertEqual(self.route("Copy of Acme_Jane Post Insights Report.csv"), ("client", "c-1"))
        self.assertEqual(self.route("Copy of Acme_Jane_EU Post Insights Report.csv"), ("paused", "c-3"))  # longest
        self.assertEqual(self.route("Acme Jane Account Performance Report.csv"), ("client", "c-1"))  # the client's name
        # "Bolt" is a client's name and could also be the username u/Bolt: the contents or a choice decide
        self.assertEqual(self.route("Bolt Post Insights Report.csv"), ("label_user", "c-2"))
        self.assertEqual(self.route("Zed Post Insights Report.csv"), ("paused", "c-5"))  # a paused one-word name
        self.assertEqual(self.route("Copy of Cedar Lane Comment Insights Report.csv"), ("paused", "c-4"))
        self.assertEqual(self.route("Cedar Lane comments.csv"), ("paused", "c-4"))
        self.assertEqual(self.route("Copy of Acme Jane Post Insights Report.csv"), ("client", "c-1"))
        self.assertEqual(self.route("Copy of Acme Jane2 Post Insights Report.csv"), ("unknown", None))  # not a whole name
        self.assertEqual(self.route("posts.csv"), ("unknown", None))

    def test_label_user_needs_a_choice(self):
        f = self.one("Bolt Post Insights Report.csv", POSTS_CSV)
        self.assertEqual(f["status"], "needs_client")
        self.assertIn("also the Reddit username u/Bolt", f["message"])
        f = self.one("Bolt Post Insights Report.csv", POSTS_CSV, client="c-2")
        self.assertEqual((f["status"], f["client"], f["client_name"], f["username"]), ("ready", "c-2", "Bolt", "bolt_bikes"))

    def test_a_strangers_account_file_never_touches_a_client(self):
        self.store_file("Acme_Jane Account Performance Report.csv", ACCOUNT_CSV, "c-1")
        newer = ACCOUNT_CSV.replace("120,5,10", "1,0,0").replace("130,6,11", "2,0,0") + "2026-10-09,3,0,0\n"
        f = self.one("Acme_Jane2 Account Performance Report.csv", newer, client="c-1")
        self.assertEqual((f["status"], f["new_username"]), ("new_client", "Acme_Jane2"))
        f = self.one("Acme_Jane_EU Account Performance Report.csv", newer, client="c-1")  # a choice can't override
        self.assertEqual((f["status"], f["paused_client"]), ("paused", "c-3"))
        self.assertEqual(dict(self.con.execute("SELECT date, views FROM account_daily")),
                         {"2026-10-07": 120, "2026-10-08": 130})


class PreviewAndImport(EngineCase):
    clients = [ACME, BOLT, b.client("c-old", "Old One", "old_one", paused=True)]

    def test_preview_and_import(self):
        other_comments = COMMENTS_CSV.replace("/c1/", "/c3/").replace("/c2/", "/c4/")
        files = [
            dropped("a", "acme_jane Comment Insights Report.csv", COMMENTS_CSV, 1),
            dropped("b", "acme_jane Comment Insights Report (1).csv", COMMENTS_CSV, 2),
            dropped("c", "ACME_JANE Post Insights Report.csv", POSTS_CSV, 1),
            dropped("d", "old_one Post Insights Report.csv", POSTS_CSV.replace("Other", "Old"), 1),
            dropped("e", "stranger Post Insights Report.csv", POSTS_CSV.replace("Other", "New"), 1),
            dropped("f", "renamed.csv", other_comments, 1),
            dropped("g", "acme_jane Account Performance Report.csv", ACCOUNT_CSV.replace("2026-10-07", "10/7/26"), 1),
            dropped("h", "Copy of bolt_bikes Account Performance Report.csv", ACCOUNT_CSV, 1),
        ]
        saved_before = list(self.con.iterdump())
        plan = self.preview(files)
        self.assertEqual(list(self.con.iterdump()), saved_before)  # a preview saves nothing
        self.assertEqual({f["id"]: f["status"] for f in plan["files"]},
                         {"a": "ready", "b": "same_file", "c": "ready", "d": "paused", "e": "new_client",
                          "f": "needs_client", "g": "cant_use", "h": "ready"})
        by_id = {f["id"]: f for f in plan["files"]}
        self.assertEqual((by_id["c"]["username"], by_id["c"]["client"], by_id["c"]["client_name"]),
                         ("acme_jane", "c-acme", "Acme Jane"))
        self.assertIn("Excel", by_id["g"]["message"])
        self.assertEqual((by_id["d"]["paused_client"], by_id["e"]["new_username"], by_id["f"]["guess"]),
                         ("c-old", "stranger", None))
        self.assertIn("acme_jane Comment Insights Report.csv", by_id["b"]["message"])
        self.assertEqual(plan["counts"], {"ready": 3, "needs_you": 4, "not_needed": 1})
        self.assertEqual(plan["slots"], {"c-acme": {"post": "c", "comment": "a", "account": None, "older": []},
                                         "c-bolt": {"post": None, "comment": None, "account": "h", "older": []}})
        self.assertEqual(plan["no_files"], [])
        self.assertNotIn("receipt", plan)
        # the user picks the client for the renamed file; Import stores exactly what the preview showed
        files[5]["client"] = "c-bolt"
        plan = self.preview(files)
        self.assertEqual((self.file(plan, "renamed.csv")["status"], plan["counts"]["ready"]), ("ready", 4))
        sent = [{**f, "expect": r["client"]} for f in files for r in plan["files"] if r["id"] == f["id"] and r["import"]]
        sent[0]["expect"] = "c-bolt"  # this one's client changed since the preview: refused, not misfiled
        saved = []
        result = self.run_import(sent, on_saved=lambda: saved.append(1))
        got = {r["id"]: r["status"] for r in result["files"]}
        self.assertEqual(got, {"a": "changed", "c": "ready", "f": "ready", "h": "ready"})
        self.assertEqual((result["imported"], saved, result["counts"]["needs_you"]), (3, [1], 1))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM pro_items WHERE account='acme_jane' AND kind='comment'")
                         .fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM pro_items WHERE account='bolt_bikes'").fetchone()[0], 2)
        self.assertEqual(result["receipt"], [
            {"client": "c-acme", "name": "Acme Jane", "lines": ["3 posts (3 new)"]},
            {"client": "c-bolt", "name": "Bolt Bikes", "lines": ["2 comments (2 new)", "Account numbers up to Thu 8 Oct"]}])
        self.assertEqual(len(list(self.backups.iterdir())), 1)  # the database was copied first
        # kept copies are named after the account they were stored under, so dropping them again files them the same way
        kept = sorted(self.uploads.iterdir())
        self.assertEqual([p.name.rsplit(" ", 1)[0] for p in kept], ["acme_jane Post Insights Report",
                                                                    "bolt_bikes Account Performance Report",
                                                                    "bolt_bikes Comment Insights Report"])
        self.assertEqual(kept[2].read_bytes(), other_comments.encode("utf-8"))
        self.assertEqual({csvfiles.account_from_filename(p.name) for p in kept}, {"acme_jane", "bolt_bikes"})
        # dropping the same files again: already imported, nothing new
        self.assertEqual({f["status"] for f in self.preview(files[2:3] + files[5:6])["files"]}, {"already"})
        # a file named for one client that holds 2 of another's comments: too little evidence to stop it, and
        # those comments stay the other client's, so nothing new is stored
        self.assertEqual(self.one("acme_jane Comment Insights Report.csv", other_comments)["status"], "already")
        many = b.report("comment", b.comment_rows(day="2026-10-09", posted=["2026-10-01"] * 6, prefix="z"))
        self.store_file("bolt_bikes Comment Insights Report.csv", many, "c-bolt")
        f = self.one("acme_jane Comment Insights Report.csv", many)
        self.assertEqual((f["status"], f["guess"]), ("name_mismatch", {"client": "c-bolt", "name": "Bolt Bikes",
                                                                       "matched": 6, "of": 6}))
        self.assertIn("The name says Acme Jane, but these comments are Bolt Bikes's (6 of 6)", f["message"])
        self.assertIn("You chose Acme Jane", self.one("x.csv", many, client="c-acme")["message"])
        # ...and a renamed file with clear evidence goes to its owner by itself
        f = self.one("x.csv", many)
        self.assertEqual((f["client"], f["evidence"]),
                         ("c-bolt", "Matched by contents: 6 of 6 comments are already Bolt Bikes's."))
        # the receipt counts each comment once, however many of the client's files list it
        later = b.report("comment", b.comment_rows(day="2026-10-10", posted=["2026-10-01"] * 6, prefix="z")
                         + b.comment_rows(day="2026-10-10", posted=["2026-10-02"] * 3, prefix="w"))
        r = self.run_import([dropped("a", "bolt_bikes Comment Insights Report.csv", many.replace("2026-10-09", "2026-10-11"),
                                     expect="c-bolt"),
                             dropped("b", "bolt_bikes Comment Insights Report (1).csv", later, expect="c-bolt")])
        self.assertEqual(r["receipt"], [{"client": "c-bolt", "name": "Bolt Bikes", "lines": ["9 comments (3 new)"]}])

    def test_a_paused_client_cant_be_chosen(self):
        f = self.one("renamed.csv", POSTS_CSV, client="c-old")
        self.assertEqual(f["status"], "needs_client")
        self.assertNotIn("c-old", self.preview([dropped("x", "posts.csv", POSTS_CSV)])["slots"])

    def test_import_needs_the_previewed_client(self):
        r = self.run_import([dropped("a", "acme_jane Post Insights Report.csv", POSTS_CSV)])  # no expect
        self.assertEqual((r["files"][0]["status"], r["imported"], r["receipt"]), ("changed", 0, []))
        self.assertFalse(self.backups.exists())  # nothing was going to be saved: no database copy


class Evidence(EngineCase):
    def test_account_evidence(self):
        mine = b.report("account", b.day_rows("2026-08-01", 60, views=lambda i: 1 + i % 3))
        self.store_file("acme_jane Account Performance Report.csv", mine, "c-acme")
        # a stranger's renamed file that agrees with Acme Jane's saved days on 10 of 57
        theirs = b.report("account", b.day_rows("2026-08-01", 60, views=lambda i: 1 + i % 3 if i < 10 else 9))
        f = self.one("account.csv", theirs)
        self.assertEqual((f["status"], f["guess"]), ("needs_client", None))
        f = self.one("account.csv", theirs, client="c-acme")  # choosing Acme Jane is stopped
        self.assertEqual((f["status"], f["against"], f["client"]), ("doesnt_match", "c-acme", None))
        self.assertIn("only 10 of 57 days agree", f["message"])
        self.assertNotIn("GoLogin", f["message"])
        # ...while Acme Jane's own renamed file finds its way by itself
        again = b.report("account", b.day_rows("2026-08-01", 61, views=lambda i: 1 + i % 3 if i < 60 else 5))
        f = self.one("download.csv", again)
        self.assertEqual((f["status"], f["client"]), ("ready", "c-acme"))
        self.assertIn("58 of 61 days", f["evidence"])
        # an account that only turned active lately: its zeros against days with views disagree too
        late = b.report("account", b.day_rows("2026-08-01", 60, views=lambda i: int(i >= 50), followers=0))
        f = self.one("account (1).csv", late, client="c-acme")
        self.assertEqual(f["status"], "doesnt_match")
        self.assertIn("only 0 of 57 days agree", f["message"])
        self.assertEqual((self.one("account (1).csv", late)["status"], self.one("account (1).csv", late)["guess"]),
                         ("needs_client", None))
        # a small renamed Posts file whose posts are all Acme Jane's goes to Acme Jane by itself
        self.store_file("acme_jane Post Insights Report.csv", POSTS_CSV, "c-acme")
        f = self.one("posts.csv", POSTS_CSV)
        self.assertEqual((f["status"], f["client"]), ("already", "c-acme"))
        self.assertIn("3 of 3 posts", f["evidence"])


class Guards(EngineCase):
    def test_older_same_day_download(self):
        evening = POSTS_CSV.replace("100,0.9", "120,0.97")
        morning = POSTS_CSV.replace("100,0.9", "100,1")
        self.store_file("acme_jane Post Insights Report (1).csv", evening, "c-acme")
        f = self.one("acme_jane Post Insights Report.csv", morning)
        self.assertEqual((f["status"], f["import"], f["message"]), ("already", False, "Already imported. Nothing new in it."))
        self.store_file("acme_jane Post Insights Report.csv", morning, "c-acme")
        self.assertEqual(self.con.execute("SELECT s.views, s.upvote_rate FROM pro_snapshots s JOIN pro_items i USING "
                                          "(item_key) WHERE i.title='Same title' ORDER BY s.views DESC LIMIT 1")
                         .fetchone(), (120, 0.97))

    def test_empty_downloads(self):
        self.store_file("acme_jane Comment Insights Report.csv", COMMENTS_CSV, "c-acme")
        f = self.one("acme_jane Comment Insights Report (1).csv", b.report("comment"))
        self.assertEqual((f["status"], f["client"]), ("cant_use", "c-acme"))
        self.assertIn("2 comments are saved for Acme Jane", f["message"])
        # a full and an empty download of the same day (the empty one newer): the full one is used
        day = b.modified_ms("2026-10-08")
        plan = self.preview([dropped("p", "acme_jane Post Insights Report.csv", POSTS_CSV, day),
                             dropped("q", "acme_jane Post Insights Report (1).csv", b.report("post"), day + 3600_000)])
        self.assertEqual([(f["status"], f["export_day"]) for f in plan["files"]],
                         [("ready", "2026-10-08"), ("older_copy", "2026-10-08")])
        self.assertIn("acme_jane Post Insights Report.csv", plan["files"][1]["message"])
        # an empty Account file is never used
        self.assertEqual(self.one("acme_jane Account Performance Report.csv", b.report("account"))["status"], "cant_use")

    def test_an_empty_report_counts_as_uploaded(self):
        monday = b.modified_ms("2026-10-12", hour=10)
        empty = dropped("e", "bolt_bikes Post Insights Report.csv", b.report("post"), monday)
        f = self.preview([empty])["files"][0]
        self.assertEqual((f["status"], f["export_day"], f["rows"]), ("ready", "2026-10-12", 0))
        self.assertEqual(f["summary"], "No posts in Reddit Pro yet. Nothing to add, and that's fine.")
        r = self.run_import([{**empty, "expect": "c-bolt"}])
        self.assertEqual(r["receipt"], [{"client": "c-bolt", "name": "Bolt Bikes", "lines": ["no posts yet"]}])
        self.assertEqual(store.client_activity(self.con, {"bolt_bikes"})["post"], "2026-10-12")
        self.assertEqual(self.preview([empty])["files"][0]["status"], "already")
        self.assertEqual(self.one("bolt_bikes Post Insights Report.csv", b.report("post"))["export_day"], "2026-10-12")

    def test_two_clients_empty_downloads(self):
        # Reddit's empty Posts download is the same few bytes for every account: each client's still counts
        monday = b.modified_ms("2026-10-12", hour=10)
        files = [dropped("a", "acme_jane Post Insights Report.csv", b.report("post"), monday),
                 dropped("b", "bolt_bikes Post Insights Report.csv", b.report("post"), monday)]
        plan = self.preview(files)
        self.assertEqual([(f["status"], f["client"]) for f in plan["files"]], [("ready", "c-acme"), ("ready", "c-bolt")])
        self.assertEqual(plan["no_files"], [])
        r = self.run_import([{**f, "expect": p["client"]} for f, p in zip(files, plan["files"])])
        self.assertEqual([f["status"] for f in r["files"]], ["ready", "ready"])
        for user in ("acme_jane", "bolt_bikes"):
            self.assertEqual(store.client_activity(self.con, {user})["post"], "2026-10-12", user)
        self.assertEqual([f["status"] for f in self.preview(files)["files"]], ["already", "already"])
        # the same client's two empty copies of a day: one is used
        twice = [dropped("c", "acme_jane Comment Insights Report.csv", b.report("comment"), monday),
                 dropped("d", "acme_jane Comment Insights Report (1).csv", b.report("comment"), monday + 60_000)]
        self.assertEqual([f["status"] for f in self.preview(twice)["files"]], ["older_copy", "ready"])

    def test_past_year_and_past_30_days_on_one_day(self):
        year = b.report("account", b.day_rows("2025-10-12", 365, views=lambda i: i))
        month = b.report("account", b.day_rows("2026-09-12", 30, views=lambda i: 1000 + i))  # newer numbers
        nine, ten = b.modified_ms("2026-10-12", hour=9), b.modified_ms("2026-10-12", hour=10)
        files = [dropped("y", "acme_jane Account Performance Report.csv", year, ten),
                 dropped("m", "acme_jane Account Performance Report (1).csv", month, nine)]
        # the 30 days downloaded first: the later year file has all of them, so the earlier one isn't needed
        self.assertEqual([f["status"] for f in self.preview(files)["files"]], ["ready", "older_copy"])
        # the year file downloaded first: its older days are kept, the later file's numbers win where both have days
        files[0]["modified"], files[1]["modified"] = nine, ten
        plan = self.preview(files)
        self.assertEqual([f["status"] for f in plan["files"]], ["ready", "ready"])
        self.assertEqual(plan["slots"]["c-acme"]["account"], "m")
        self.assertEqual(plan["slots"]["c-acme"]["older"], ["y"])
        self.run_import([{**f, "expect": "c-acme"} for f in files])
        self.assertEqual(self.con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM account_daily").fetchone(),
                         (365, "2025-10-12", "2026-10-11"))
        self.assertEqual(self.con.execute("SELECT views FROM account_daily WHERE date='2026-10-11'").fetchone(), (1029,))
        # dropping both again changes nothing: the year file only fills days the later file lacks
        again = self.run_import([{**f, "expect": "c-acme"} for f in files])
        self.assertEqual([f["status"] for f in again["files"]], ["already", "already"])
        self.assertEqual(self.con.execute("SELECT views FROM account_daily WHERE date='2026-10-11'").fetchone(), (1029,))
        # two downloads of the same days: the earlier one is an older copy, as for Posts and Comments
        evening = month.replace(",1029,", ",1100,")
        plan = self.preview([dropped("m", "acme_jane Account Performance Report.csv", month, nine),
                             dropped("e", "acme_jane Account Performance Report (1).csv", evening, ten)])
        self.assertEqual([f["status"] for f in plan["files"]], ["older_copy", "ready"])

    def test_older_files_fill_gaps(self):
        newer = "Date,Views,Upvotes,Followers\n2026-10-07,120,5,10\n2026-10-08,130,6,11\n"
        older = "Date,Views,Upvotes,Followers\n2026-10-05,90,1,9\n2026-10-06,95,1,9\n2026-10-07,50,2,10\n"
        self.store_file("acme_jane Account Performance Report.csv", newer, "c-acme")
        f = self.one("acme_jane Account Performance Report (1).csv", older)
        self.assertEqual((f["status"], f["import"]), ("fills_gap", True))
        self.store_file("acme_jane Account Performance Report (1).csv", older, "c-acme")
        self.assertEqual(dict(self.con.execute("SELECT date, views FROM account_daily")),
                         {"2026-10-05": 90, "2026-10-06": 95, "2026-10-07": 120, "2026-10-08": 130})
        # an older Posts download fills a gap too
        self.store_file("acme_jane Post Insights Report.csv", POSTS_CSV, "c-acme")
        f = self.one("acme_jane Post Insights Report (1).csv", POSTS_CSV.replace("2026-10-08", "2026-10-05"))
        self.assertEqual(f["status"], "fills_gap")
        # several days of a report in one drop: the newest has the slot, the older ones are imported first
        plan = self.preview([dropped("n", "bolt_bikes Post Insights Report.csv", POSTS_CSV),
                             dropped("o", "bolt_bikes Post Insights Report (1).csv",
                                     POSTS_CSV.replace("2026-10-08", "2026-10-05"))])
        self.assertEqual([f["status"] for f in plan["files"]], ["ready", "ready"])
        self.assertEqual(plan["slots"]["c-bolt"], {"post": "n", "comment": None, "account": None, "older": ["o"]})
        self.assertEqual(plan["no_files"], ["c-acme"])


class Warnings(EngineCase):
    def warnings(self, name: str, text: str) -> list[dict]:
        f = self.one(name, text)
        self.assertEqual(f["status"], "ready", f)
        return f["warnings"]

    def test_warnings(self):
        self.store_file("acme_jane Account Performance Report.csv", ACCOUNT_CSV, "c-acme")
        self.store_file("acme_jane Comment Insights Report.csv", COMMENTS_CSV, "c-acme")
        gap = self.warnings("acme_jane Account Performance Report.csv", b.report("account", b.day_rows("2026-10-12", 3)))
        self.assertEqual(gap, [{"text": "This Account file starts today, but the saved numbers end Thu 8 Oct, so 3 days "
                                        "are missing. Choose Past year and download it again.", "level": "redo"}])
        for kind in ("post", "comment"):
            rows = (b.post_rows if kind == "post" else b.comment_rows)(120, day="2026-10-20", per_day=3, sort="top")
            top = self.warnings(f"bolt_bikes {csvfiles.REPORT_TITLES[kind]}.csv", b.report(kind, rows))
            self.assertIn("sorted by Top", top[0]["text"])
            self.assertEqual(top[0]["level"], "redo")
        # a busy account: comments posted since the last upload are already out of Reddit's file
        busy = b.report("comment", b.comment_rows(100, day="2026-10-20", newest="2026-10-19", per_day=10, prefix="k"))
        self.assertEqual(self.warnings("acme_jane Comment Insights Report (1).csv", busy), [
            {"text": "Comments posted between Thu 8 Oct and Sat 10 Oct weren't in any upload, so they're missing. "
                     "Upload more often.", "level": "note"},
            {"text": "Reddit's file only reaches back 10 days, so these comments stop updating after that and get no "
                     "30-day views.", "level": "note"}])
        calm = b.report("comment", b.comment_rows(100, day="2026-10-09", newest="2026-10-08", per_day=3, prefix="k"))
        self.assertEqual(self.warnings("acme_jane Comment Insights Report (1).csv", calm), [])

    def test_one_day_reads_right(self):
        self.store_file("acme_jane Account Performance Report.csv", ACCOUNT_CSV, "c-acme")
        gap = self.warnings("acme_jane Account Performance Report.csv", b.report("account", b.day_rows("2026-10-10", 2)))
        self.assertIn("end Thu 8 Oct, so 1 day is missing. Choose Past year", gap[0]["text"])
        for per_day, back in ((60, "1 day"), (200, "less than a day")):
            rows = b.comment_rows(100, day="2026-10-20", newest="2026-10-20", per_day=per_day, prefix=f"r{per_day}")
            notes = self.warnings("bolt_bikes Comment Insights Report.csv", b.report("comment", rows))
            self.assertEqual(notes[-1]["text"], f"Reddit's file only reaches back {back}, so these comments stop "
                                                "updating after that and get no 30-day views.")


class Failures(EngineCase):
    def test_partial_import(self):
        real, calls = store.import_rows, []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **kw)
        saved = []
        with mock.patch.object(store, "import_rows", flaky):
            r = self.run_import([
                dropped("a", "acme_jane Account Performance Report.csv", ACCOUNT_CSV, expect="c-acme"),
                dropped("b", "acme_jane Post Insights Report.csv", POSTS_CSV, expect="c-acme"),
                dropped("c", "acme_jane Comment Insights Report.csv", COMMENTS_CSV, expect="c-acme")],
                on_saved=lambda: saved.append(1))
        self.assertEqual({f["id"]: f["status"] for f in r["files"]}, {"a": "ready", "b": "failed", "c": "not_reached"})
        failed = r["files"][1]["message"]
        self.assertEqual(failed, "Couldn't be saved: Reddit Tracker's database is busy. The files before it were saved. "
                                 "Press Import again for the rest.")
        self.assertEqual((r["imported"], saved, r["counts"]["needs_you"]), (1, [1], 1))
        self.assertEqual(r["receipt"], [{"client": "c-acme", "name": "Acme Jane",
                                         "lines": ["Account numbers up to Thu 8 Oct"]}])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM pro_items").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 1)  # rolled back with its rows
        # a kept copy that can't be written is a note; the import itself is saved
        shutil.rmtree(self.uploads)
        self.uploads.write_text("not a folder", encoding="utf-8")
        r = self.store_file("acme_jane Post Insights Report.csv", POSTS_CSV, "c-acme")
        self.assertEqual(r["imported"], 1)
        self.assertTrue(r["files"][0]["warnings"][-1]["text"].startswith("Saved, but no copy of the file could be kept"))
        self.assertEqual(r["files"][0]["warnings"][-1]["level"], "note")
        # the database turning unreadable right after a file was saved: that file counts as imported, the rest wait
        real_fp, reads = store.fingerprint, []

        def locked_after_save(*a):
            reads.append(1)
            if len(reads) == 2:  # the look at what the first file added
                raise sqlite3.OperationalError("database is locked")
            return real_fp(*a)
        with mock.patch.object(store, "fingerprint", locked_after_save):
            r = self.run_import([
                dropped("c", "acme_jane Comment Insights Report.csv", COMMENTS_CSV.replace("2026-10-08", "2026-10-10"),
                        expect="c-acme"),
                dropped("d", "acme_jane Account Performance Report.csv", ACCOUNT_CSV.replace("2026-10-08", "2026-10-11"),
                        expect="c-acme")])
        self.assertEqual({f["id"]: (f["status"], f["import"]) for f in r["files"]},
                         {"c": ("ready", True), "d": ("not_reached", False)})
        self.assertIn({"text": "Saved, but Reddit Tracker couldn't check what it added: Reddit Tracker's database is "
                               "busy.", "level": "note"}, r["files"][0]["warnings"])
        self.assertTrue(r["receipt"])

    def test_a_data_folder_that_cant_be_written(self):
        full = ("Reddit Tracker can't save to its data folder (the disk is full or can't be written to). The files "
                "before it were saved. Press Import again for the rest.")
        for problem in ("attempt to write a readonly database", "database or disk is full", "disk I/O error"):
            with mock.patch.object(store, "import_rows", side_effect=sqlite3.OperationalError(problem)):
                r = self.store_file("acme_jane Post Insights Report.csv", POSTS_CSV, "c-acme")
            self.assertEqual((r["files"][0]["status"], r["files"][0]["message"]), ("failed", "Couldn't be saved: " + full))
        for e in (OSError(errno.EROFS, "Read-only file system"), PermissionError(errno.EACCES, "Permission denied"),
                  OSError(errno.ENOSPC, "No space left on device")):
            self.assertEqual(engine.plain_error(e), engine.UNWRITABLE, e)
        self.assertEqual(engine.plain_error(OSError(errno.EEXIST, "File exists")), "File exists")
        # the kept copy of a file on a full disk: the import is saved, with a plain note
        with mock.patch.object(Path, "write_bytes", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            r = self.store_file("acme_jane Post Insights Report.csv", POSTS_CSV, "c-acme")
        self.assertEqual((r["imported"], r["files"][0]["warnings"][-1]["text"]),
                         (1, f"Saved, but no copy of the file could be kept: {engine.UNWRITABLE}."))

    def test_a_preview_problem_is_raised(self):
        with mock.patch.object(store, "import_rows", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertRaises(sqlite3.OperationalError):
            self.preview([dropped("a", "acme_jane Post Insights Report.csv", POSTS_CSV)])

    def test_import_while_the_page_polls(self):
        errors, stop = [], threading.Event()

        def poll():
            while not stop.is_set():
                try:
                    con = store.open_read(self.db)
                    try:
                        store.client_activity(con, {"acme_jane"})
                        store.memory_copy(con).close()
                    finally:
                        con.close()
                except Exception as e:
                    errors.append(repr(e))
        threads = [threading.Thread(target=poll) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            for day in range(10, 20):
                r = self.store_file("acme_jane Account Performance Report.csv",
                                    ACCOUNT_CSV.replace("2026-10-08", f"2026-10-{day}"), "c-acme")
                self.assertEqual(r["imported"], 1, r["files"])
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(list(self.backups.iterdir())), 5)


if __name__ == "__main__":
    unittest.main()
