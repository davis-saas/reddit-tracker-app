"""Writing the tabs to a Sheet (grow, overwrite, trim, shrink guard) and the background Sheets job,
against the strict fake Google in fake_google.py."""
from __future__ import annotations

import contextlib
import gc
import json
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import weakref
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _folder in (HERE.parent, HERE):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))

from fake_google import FakeClock, FakeGoogle  # noqa: E402
from rt import sheets  # noqa: E402
from rt.google import GoogleError  # noqa: E402
from rt.sheets import SheetsJob, ShrinkError, tables_digest, write_tables  # noqa: E402
from rt.google import MESSAGES  # noqa: E402


def table(n: int, cols: int = 3) -> list[list]:
    return [[f"h{c}" for c in range(cols)]] + [[f"r{i}c{c}" for c in range(cols)] for i in range(n)]


OUT = {"content": table(5), "account_daily": table(2, 2), "content_history": table(0)}


class Rows(list):
    """A table whose lifetime a test can watch (a weak reference to it)."""


class Source:
    """Rows made anew each time they're read, like store.History: len() and iter(), no list."""

    def __init__(self, rows: list[list]):
        self.rows, self.reads = rows, 0

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        for r in self.rows:
            yield list(r)
        self.reads += 1  # read to the end (a look at the header costs nothing, as with History)


def ready(tables: dict):
    """A target's read_tables for tables already made."""
    return lambda: contextlib.nullcontext(tables)


class CountingGoogle:
    """Answers write_tables like Google but keeps nothing, so only write_tables' own memory counts."""

    def __init__(self, rows: int, cols: int):
        self.meta = {"properties": {"title": "Big"}, "sheets": [{"properties": {
            "sheetId": 0, "title": "content", "gridProperties": {"rowCount": rows, "columnCount": cols}}}]}
        self.sent = 0

    def get_meta(self, sheet_id, fields=None):
        return self.meta

    def values_update(self, sheet_id, a1, values):
        self.sent += len(values)

    def batch_update(self, sheet_id, requests):
        return {}


class Base(unittest.TestCase):
    write_limit = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = FakeClock()
        self.fake = FakeGoogle(clock=self.clock.now, write_limit=self.write_limit).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.google = self.fake.client(self.dir, self.clock)

    def write(self, sheet_id: str, tables: dict, **kw) -> dict:
        return write_tables(self.google, sheet_id, tables, **kw)

    def put_queries(self) -> list[dict]:
        return [c[2] for c in self.fake.writes() if c[0] == "PUT"]


class WriteTablesTests(Base):
    def test_writes_every_tab_and_removes_the_empty_default_tab(self):
        sheet = self.fake.make_sheet(tabs={"Sheet1": (1000, 26)})
        self.assertEqual(self.write(sheet, OUT), {"title": "Fake Sheet", "rows": 5, "digest": tables_digest(OUT),
                                                 "tabs": {"content": 1, "account_daily": 2, "content_history": 3}})
        self.assertEqual(self.fake.tab_names(sheet), ["content", "account_daily", "content_history"])
        self.assertEqual(self.fake.values(sheet, "content"), table(5))
        self.assertEqual(self.fake.values(sheet, "account_daily"), table(2, 2))
        self.assertEqual(self.fake.values(sheet, "content_history"), [table(0)[0], ["", "", ""]])  # header only
        self.assertTrue(self.put_queries() and all(q == {"valueInputOption": "RAW"} for q in self.put_queries()))

    def test_an_empty_default_tab_in_any_language_goes(self):
        sheet = self.fake.make_sheet(tabs={"Feuille 1": (1000, 26)})
        self.write(sheet, OUT)
        self.assertEqual(self.fake.tab_names(sheet), list(OUT))

    def test_other_tabs_are_kept(self):
        notes = self.fake.make_sheet(tabs={"Sheet1": (10, 3)})  # a Sheet chosen by mistake keeps its data
        self.fake.set_rows(notes, "Sheet1", [["my notes"]])
        self.write(notes, OUT)
        self.assertEqual(self.fake.values(notes, "Sheet1")[0][0], "my notes")
        chart = self.fake.make_sheet(tabs={"Dashboard": (10, 3)})  # only a chart on it: still kept
        self.fake.files[chart].tab("Dashboard").charts.append(7)
        self.write(chart, OUT)
        self.assertIn("Dashboard", self.fake.tab_names(chart))
        several = self.fake.make_sheet(tabs={"Sheet1": (10, 3), "Notes": (10, 3)})  # never touched when not alone
        self.write(several, OUT)
        self.assertEqual(self.fake.tab_names(several)[:2], ["Sheet1", "Notes"])

    def test_growing_tabs_get_room_first_and_shrinking_tabs_are_trimmed_after_their_write(self):
        sheet = self.fake.make_sheet(tabs={"content": (4, 2), "account_daily": (10, 2), "content_history": (2, 3)})
        self.write(sheet, OUT)
        self.assertEqual(self.fake.values(sheet, "content"), table(5))
        self.assertEqual(self.fake.values(sheet, "account_daily"), table(2, 2))
        self.assertEqual(self.fake.batch_requests(), [["updateSheetProperties"], ["updateSheetProperties"]])
        order = [c[1].split(":")[-1] if c[0] == "POST" else c[1].split("'")[1] for c in self.fake.writes()]
        self.assertEqual(order, ["batchUpdate", "content", "account_daily", "batchUpdate", "content_history",
                                 "batchClear"])

    def test_a_failure_on_a_later_tab_leaves_earlier_tabs_complete(self):
        sheet = self.fake.make_sheet(tabs={"content": (30, 3), "content_history": (5, 3)})
        self.fake.set_rows(sheet, "content", table(28))
        self.fake.fail(r"content_history", method="PUT", status=400, message="Google failed on content_history")
        with self.assertRaises(GoogleError) as caught:
            self.write(sheet, {"content": table(5), "content_history": table(2)})
        self.assertIn("content_history", caught.exception.message)
        self.assertEqual(self.fake.values(sheet, "content"), table(5))  # no old rows below the new ones

    def test_big_tabs_go_in_parts_that_fit_the_grid(self):
        sheet = self.fake.make_sheet(tabs={"content": (3, 3)})
        with mock.patch.object(sheets, "PART_BYTES", 200):
            self.write(sheet, {"content": table(40)})
        self.assertEqual(self.fake.values(sheet, "content"), table(40))
        self.assertGreater(len(self.put_queries()), 3)

    def test_a_tab_read_as_it_is_sent(self):
        sheet = self.fake.make_sheet(tabs={"content": (3, 3), "content_history": (90, 3)})
        self.fake.set_rows(sheet, "content_history", table(89))
        history = Source(table(40))
        with mock.patch.object(sheets, "PART_BYTES", 200):
            self.write(sheet, {"content": table(5), "content_history": history})
        self.assertEqual(self.fake.values(sheet, "content_history"), table(40))
        self.assertEqual(self.fake.grid(sheet, "content_history"), (41, 3))  # sized and trimmed by its len()
        puts = [c for c in self.fake.writes() if c[0] == "PUT" and "content_history" in c[1]]
        self.assertGreater(len(puts), 3)

    def test_a_database_that_lost_its_history_never_wipes_a_sheet(self):
        sheet = self.fake.make_sheet(tabs={"content": (301, 3)})
        with self.assertRaises(ShrinkError) as caught:
            self.write(sheet, {"content": table(100)})
        self.assertIn("has 300 rows for these clients but Reddit Tracker on this computer has only 100",
                      caught.exception.message)
        self.assertEqual((caught.exception.code, caught.exception.before, caught.exception.after), ("shrink", 300, 100))
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.fake.grid(sheet, "content"), (301, 3))
        self.write(sheet, {"content": table(160)})  # a smaller cut is fine
        self.assertEqual(self.fake.values(sheet, "content"), table(160))
        big = self.fake.make_sheet(tabs={"content": (3734, 3)})  # paused clients explain a big one
        self.write(big, {"content": table(1623)}, allow_drop=2110)
        self.assertEqual(self.fake.grid(big, "content"), (1624, 3))
        again = self.fake.make_sheet(tabs={"content": (301, 3)})  # "Replace anyway"
        self.write(again, {"content": table(100)}, allow_shrink=True)
        self.assertEqual(self.fake.grid(again, "content"), (101, 3))

    def test_rows_of_other_accounts_may_go(self):
        header = ["kind", "account", "views"]
        mine = {"content": [header] + [["post", "Me", "1"]] * 3}

        def sheet_with(accounts: list[str]) -> str:
            sheet = self.fake.make_sheet(tabs={"content": (len(accounts) + 1, 3)})
            self.fake.set_rows(sheet, "content", [header] + [["post", a, "1"] for a in accounts])
            return sheet
        copied = sheet_with(["Someone_Else"] * 300)
        self.write(copied, mine, users={"me"})
        self.assertEqual(self.fake.values(copied, "content"), mine["content"])
        mixed = sheet_with(["Me"] * 250 + ["Someone_Else"] * 50)
        with self.assertRaises(ShrinkError) as caught:
            self.write(mixed, mine, users={"me"})
        self.assertEqual((caught.exception.before, caught.exception.after), (250, 3))

    def test_cells_are_written_as_they_are(self):
        sheet = self.fake.make_sheet(tabs={"content": (2, 4)})
        self.write(sheet, {"content": [["a", "b", "c", "d"], [None, "x" * 60_000, 1800.0, "=1+1"]]})
        row = self.fake.values(sheet, "content")[1]
        self.assertEqual((row[0], len(row[1]), row[2], row[3]), ("", 49_000, 1800.0, "=1+1"))

    def test_a_sheet_past_googles_cell_limit(self):
        sheet = self.fake.make_sheet(tabs={"content": (2, 3), "Big": (1_000_000, 20)})
        with self.assertRaises(GoogleError) as caught:
            self.write(sheet, OUT)
        self.assertEqual(caught.exception.code, "too_big")
        self.assertEqual(self.fake.writes(), [])
        self.fake.cell_limit = 50  # Google's own answer means the same
        small = self.fake.make_sheet(tabs={"content": (2, 3)})
        with self.assertRaises(GoogleError) as caught:
            self.write(small, {"content": table(30)})
        self.assertEqual(caught.exception.code, "too_big")
        self.assertIn("Make a new Sheet", caught.exception.message)
        # more than any one Sheet can hold: a new Sheet won't help, so it isn't offered
        with mock.patch.object(sheets, "CELL_LIMIT", 50), self.assertRaises(GoogleError) as caught:
            self.write(self.fake.make_sheet(tabs={"content": (2, 3)}), {"content": table(30)})
        self.assertEqual(caught.exception.code, "too_much")
        self.assertNotIn("Make a new Sheet", caught.exception.message)

    def test_a_tab_of_ours_named_in_another_case_is_used(self):
        sheet = self.fake.make_sheet(tabs={"Content": (10, 3)})  # Google tab names don't care about case
        self.fake.set_rows(sheet, "Content", table(8))
        self.write(sheet, OUT)
        self.assertEqual(self.fake.tab_names(sheet), ["Content", "account_daily", "content_history"])
        self.assertEqual(self.fake.values(sheet, "Content"), table(5))

    def test_rows_someone_froze_stay_visible(self):
        sheet = self.fake.make_sheet(tabs={"content": (10, 3), "content_history": (50, 3)})
        self.fake.files[sheet].tab("content_history").frozen = 2  # Google needs a row below frozen ones
        self.fake.set_rows(sheet, "content_history", table(40))
        self.write(sheet, OUT)  # content_history is only its header now
        self.assertEqual(self.fake.values(sheet, "content_history"), [table(0)[0], ["", "", ""], ["", "", ""]])

    def test_a_lost_reply_to_removing_the_empty_default_tab_is_not_a_failure(self):
        sheet = self.fake.make_sheet(tabs={"Sheet1": (1000, 26)})
        send = self.google.batch_update

        def batch_update(sheet_id, requests):
            if "deleteSheet" in requests[0]:
                self.fake.fail(r":batchUpdate$", after_apply=True)  # Google deleted it, the answer got lost
            return send(sheet_id, requests)
        with mock.patch.object(self.google, "batch_update", batch_update):
            self.assertEqual(self.write(sheet, OUT)["rows"], 5)
        self.assertEqual(self.fake.tab_names(sheet), list(OUT))

    def test_tables_are_cleaned_a_part_at_a_time(self):
        rows = [[f"h{c}" for c in range(11)]] + [[f"r{i}", i, 1.5, None, "x", 2, 3, 4, 5, 6, 7] for i in range(20_000)]
        tracemalloc.start()
        copy = [[sheets._cell(v) for v in r] for r in rows]  # what a full second copy costs
        full = tracemalloc.get_traced_memory()[0]
        del copy
        tracemalloc.stop()
        google = CountingGoogle(len(rows), 11)
        tracemalloc.start()
        try:
            with mock.patch.object(sheets, "PART_BYTES", 100_000):
                write_tables(google, "S", {"content": rows})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(google.sent, len(rows))
        self.assertLess(peak, full / 2)

    def test_the_digest_follows_the_numbers_and_the_version(self):
        same = tables_digest({"content": table(5), "content_history": table(2)})
        self.assertEqual(same, tables_digest({"content_history": table(2), "content": table(5)}))
        self.assertEqual(same, tables_digest({"content": table(5), "content_history": Source(table(2))}))
        big = table(12_345)  # hashed 5000 rows at a time, whatever holds them
        self.assertEqual(tables_digest({"content_history": big}), tables_digest({"content_history": Source(big)}))
        changed = table(5)
        changed[3][1] = 7
        self.assertNotEqual(same, tables_digest({"content": changed, "content_history": table(2)}))
        with mock.patch.object(sheets, "VERSION", "99.0.0"):  # an update writes each Sheet once more
            self.assertNotEqual(same, tables_digest({"content": table(5), "content_history": table(2)}))

    def test_a_lost_reply_to_adding_tabs_is_not_a_failure(self):
        sheet = self.fake.make_sheet(tabs={"Sheet1": (1000, 26)})
        self.fake.fail(r":batchUpdate$", after_apply=True)  # Google added them, the answer got lost
        self.write(sheet, OUT)
        adds = [kinds for kinds in self.fake.batch_requests() if kinds[0] == "addSheet"]
        self.assertEqual(adds, [["addSheet"] * 3] * 2)  # the retry was refused ("already exists")...
        metas = [c for c in self.fake.calls if c[0] == "GET" and c[1] == f"/v4/spreadsheets/{sheet}"]
        self.assertEqual(len(metas), 2)  # ...so it looked again instead of failing
        self.assertEqual(self.fake.tab_names(sheet), list(OUT))
        self.assertEqual(self.fake.values(sheet, "content"), table(5))


class FakeTimer:
    def __init__(self, interval: float, fn):
        self.interval, self.fn, self.started, self.cancelled = interval, fn, False, False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class BrokenSettings(Exception):
    code = "broken_file"


class JobTests(Base):
    def setUp(self):
        super().setUp()
        self.db_lock = threading.RLock()
        self.lines: list[str] = []
        self.timers: list[FakeTimer] = []
        self.a = self.fake.make_sheet("Acme Jane", tabs={"Sheet1": (1000, 26)})
        self.b = self.fake.make_sheet("Bolt Bikes", tabs={"Sheet1": (1000, 26)})
        self.content = {"c-a": table(5), "c-b": table(3)}
        self.targets = [("c-a", "Acme Jane", self.a), ("c-b", "Bolt Bikes", self.b)]
        self.job = self.make_job()

    def build(self) -> list[dict]:
        return [{"key": key, "name": name, "sheet_id": sheet, "allow_drop": 0, "users": None, "read_tables": ready(
                 {"content": self.content[key], "account_daily": table(1, 2), "content_history": table(0)})}
                for key, name, sheet in self.targets]

    def timer(self, interval: float, fn) -> FakeTimer:
        self.timers.append(FakeTimer(interval, fn))
        return self.timers[-1]

    def make_job(self) -> SheetsJob:
        return SheetsJob(self.dir / "sheets_status.json", self.db_lock, self.build, self.google, self.lines.append,
                         timer=self.timer)

    def bump(self) -> None:
        with self.db_lock:
            self.job.bump()

    def wait(self) -> dict:
        for _ in range(500):
            state = self.job.state()
            if not state["running"]:
                return state
            time.sleep(0.01)
        raise AssertionError("the Sheets job never finished")

    def states(self, state: dict) -> list:
        return [(t["state"], t["code"]) for t in state["targets"]]

    def test_a_pass_writes_every_sheet(self):
        self.bump()
        self.assertTrue(self.job.pending())
        self.assertEqual(self.job.start("import"), "started")
        state = self.wait()
        self.assertEqual(self.states(state), [("done", None), ("done", None)])
        self.assertEqual((state["done"], state["total"], state["pending"], state["next_retry_at"]), (2, 2, False, None))
        self.assertEqual([(t["key"], t["sheet_id"], t["rows"]) for t in state["targets"]],
                         [("c-a", self.a, 5), ("c-b", self.b, 3)])
        self.assertTrue(state["started_at"].endswith("Z") and state["finished_at"].endswith("Z"))
        saved = self.job.sheet_status(self.a)
        self.assertEqual((saved["ok"], saved["rows"], saved["title"], saved["code"]), (True, 5, "Acme Jane", None))
        self.assertEqual(saved["ok_at"], saved["at"])
        self.assertEqual(self.fake.values(self.b, "content"), table(3))
        self.assertEqual(self.timers, [])

    def test_a_change_made_during_a_pass_stays_pending(self):
        self.bump()
        gate = self.fake.hold(self.a + "/values/")
        self.assertEqual(self.job.start(), "started")
        self.assertTrue(self.fake.held.wait(5))
        self.bump()  # an import lands while the pass waits on Google
        gate.set()
        self.assertEqual(self.states(self.wait()), [("done", None), ("done", None)])
        self.assertTrue(self.job.pending())  # that pass started before the change
        self.job.start()
        self.assertFalse(self.wait()["pending"])
        # started again while running: one more pass right after, with the new numbers
        gate = self.fake.hold(self.a + "/values/")
        self.assertEqual(self.job.start(force=True), "started")
        self.assertTrue(self.fake.held.wait(5))
        self.content["c-a"] = table(7)
        self.bump()
        self.assertEqual(self.job.start(), "queued")
        gate.set()
        state = self.wait()
        self.assertFalse(state["pending"])
        self.assertEqual(self.fake.values(self.a, "content"), table(7))

    def test_no_internet_stops_the_pass_and_tries_again_in_five_minutes(self):
        self.bump()
        self.fake.offline = True
        self.job.start()
        state = self.wait()
        self.assertEqual(self.states(state), [("failed", "offline"), ("skipped", None)])
        self.assertTrue(state["pending"])
        self.assertEqual([(t.interval, t.started) for t in self.timers], [(300, True)])
        self.assertIsNotNone(state["next_retry_at"])
        self.assertEqual(self.job.sheet_status(self.a)["code"], "offline")
        self.assertIsNone(self.job.sheet_status(self.b))  # skipped Sheets keep what they had
        self.timers[0].fn()  # five minutes later, still offline: tried again later still
        self.wait()
        self.assertEqual(len(self.timers), 2)
        self.fake.offline = False
        self.timers[1].fn()
        state = self.wait()
        self.assertEqual((state["pending"], state["next_retry_at"]), (False, None))
        self.assertEqual(len(self.timers), 2)
        self.assertIsNone(self.job.sheet_status(self.a)["code"])

    def test_a_manual_update_cancels_the_timed_retry_when_it_works(self):
        self.bump()
        self.fake.fail(self.a, status=429, times=4)  # Google keeps saying "slow down"
        self.job.start()
        state = self.wait()
        # it outlasted every wait: the pass stops there instead of making each other Sheet wait again
        self.assertEqual(self.states(state), [("failed", "quota"), ("skipped", None)])
        self.assertEqual(self.clock.slept.count(65), 1)
        self.assertTrue(state["pending"] and self.timers[0].started)
        self.job.start()
        self.assertEqual(self.states(self.wait()), [("done", None), ("done", None)])
        self.assertFalse(self.job.pending())
        self.assertTrue(self.timers[0].cancelled)
        self.assertIsNone(self.job.state()["next_retry_at"])

    def test_a_problem_only_a_person_can_fix_still_completes_the_pass(self):
        self.fake.files[self.b].granted.clear()  # no longer opened through the app
        self.bump()
        self.job.start()
        state = self.wait()
        self.assertEqual(self.states(state), [("done", None), ("failed", "not_found")])
        self.assertFalse(state["pending"])
        self.assertEqual(self.timers, [])
        saved = self.job.sheet_status(self.b)
        self.assertEqual((saved["ok"], saved["code"], saved["ok_at"]), (False, "not_found", None))

    def test_a_turned_down_sign_in_stops_the_pass_and_waits_for_sign_in(self):
        self.bump()
        self.fake.refresh_invalid = True
        self.fake.expire_access_tokens()
        self.assertEqual(self.job.start(), "started")
        state = self.wait()
        self.assertEqual(self.states(state), [("failed", "rejected"), ("skipped", None)])
        self.assertTrue(state["pending"])
        self.assertEqual(self.timers, [])  # no timed retry: only signing in again helps
        self.assertEqual(self.job.start(), "not_signed_in")
        self.fake.refresh_invalid = False
        self.fake.sign_in(self.google)
        self.job.start("signed in")
        self.assertFalse(self.wait()["pending"])

    def test_nothing_to_write_and_not_signed_in(self):
        self.targets = []
        self.bump()
        self.assertEqual(self.job.start(), "nothing_to_do")
        self.assertFalse(self.job.pending())
        self.assertEqual(self.job.state()["targets"], [])
        self.google.sign_out()
        self.bump()
        self.assertEqual(self.job.start(), "not_signed_in")
        self.assertEqual(self.job.resume(), "not_signed_in")
        self.assertTrue(self.job.pending())

    def test_broken_settings(self):
        def broken():
            raise BrokenSettings("settings.json can't be read")
        self.job.build_targets = broken
        self.assertEqual(self.job.start(), "settings_broken")
        state = self.job.state()
        self.assertFalse(state["running"])
        self.assertIsNotNone(state["finished_at"])
        self.assertEqual(self.timers, [])  # only fixing the settings helps

    def test_resume_only_when_pending(self):
        self.assertIsNone(self.job.resume())
        self.bump()
        self.job = self.make_job()  # e.g. after a restart
        self.assertEqual(self.job.resume(), "started")
        self.assertFalse(self.wait()["pending"])

    def status_file(self) -> dict:
        return json.loads((self.dir / "sheets_status.json").read_text(encoding="utf-8"))

    def test_a_run_that_died_during_a_pass_waits_five_minutes_before_trying_again(self):
        self.bump()
        with mock.patch.object(sheets, "write_tables", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.job._one_pass(lambda outcome: None, "")  # the run dies here (e.g. out of memory: no clean-up)
        self.assertIn("pass_running", self.status_file())
        self.job = self.make_job()  # started again (the Raspberry Pi's service does that at once)
        self.assertEqual(self.job.resume(), "waiting")  # not the same pass again at once: it could die again
        self.assertEqual(self.lines[-1], "Reddit Tracker stopped while it was updating the Google Sheets. It will "
                                         "try again in 5 minutes.")
        state = self.job.state()
        self.assertEqual((state["running"], state["pending"]), (False, True))
        self.assertIsNotNone(state["next_retry_at"])
        self.assertEqual([(t.interval, t.started) for t in self.timers], [(300, True)])
        # meanwhile the page says these Sheets don't have the newest numbers yet
        self.assertEqual([(t["sheet_id"], t["state"], t["message"]) for t in state["targets"]],
                         [(sheet, "skipped", "Not updated yet: Reddit Tracker will try again in a few minutes.")
                          for sheet in (self.a, self.b)])
        self.timers[0].fn()  # five minutes later
        self.assertEqual(self.states(self.wait()), [("done", None), ("done", None)])
        self.assertNotIn("pass_running", self.status_file())
        self.assertFalse(self.job.pending())

    def test_a_pass_cut_off_by_stopping_on_purpose_resumes_at_once(self):
        self.bump()
        gate = self.fake.hold(self.b + "/values/")
        self.job.start()
        self.assertTrue(self.fake.held.wait(5))
        self.assertIn("pass_running", self.status_file())
        self.job.close()  # Ctrl+C, or the Pi's service stopped
        self.assertNotIn("pass_running", self.status_file())
        gate.set()
        self.wait()

    def test_stopping_on_purpose_while_waiting_after_a_crash_resumes_at_once(self):
        self.bump()
        with mock.patch.object(sheets, "write_tables", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.job._one_pass(lambda outcome: None, "")  # died
        self.job = self.make_job()
        self.assertEqual(self.job.resume(), "waiting")
        self.job.close()  # stopped on purpose before the five minutes were up
        self.assertNotIn("pass_running", self.status_file())
        self.job = self.make_job()
        self.assertEqual(self.job.resume(), "started")
        self.assertFalse(self.wait()["pending"])

    def test_a_sheet_asked_to_be_written_anew_during_a_pass_is_written_by_the_next(self):
        self.bump()
        self.job.start()
        self.wait()
        build = self.build

        def asked_while_checked():  # "Sheet checked" for Acme while this pass looks at Acme's numbers
            targets = build()
            plain = targets[0]["read_tables"]

            @contextlib.contextmanager
            def read_tables():
                with plain() as tables:
                    self.job.rewrite(self.a)
                    yield tables
            targets[0]["read_tables"] = read_tables
            return targets
        self.job.build_targets = asked_while_checked
        self.bump()
        self.job.start()
        self.wait()  # it was unchanged when this pass checked it...
        self.assertIsNone(self.job.sheet_status(self.a)["hash"])  # ...but it must still be written anew
        self.job.build_targets = build
        before = len(self.fake.writes())
        self.bump()
        self.job.start()
        self.wait()
        self.assertGreater(len(self.fake.writes()), before)  # written, though nothing changed
        self.assertIsNotNone(self.job.sheet_status(self.a)["hash"])
        gate = self.fake.hold(self.a + "/values/")  # and asked while it's being written
        self.job.start(force=True)
        self.assertTrue(self.fake.held.wait(5))
        self.job.rewrite(self.a)
        gate.set()
        self.wait()
        self.assertIsNone(self.job.sheet_status(self.a)["hash"])
        self.assertIsNotNone(self.job.sheet_status(self.b)["hash"])

    def crash(self) -> None:
        self.bump()
        with mock.patch.object(sheets, "write_tables", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.job._one_pass(lambda outcome: None, "")  # died
        self.job = self.make_job()

    def test_after_a_crash_an_update_already_done_is_left_alone(self):
        self.crash()
        build, done = self.job.build_targets, {}

        def update_now_meanwhile():  # "Update now" runs to the end while the start-up check lists the Sheets
            self.job.build_targets = build
            self.job.start("asked", force=True)
            done["state"] = state = self.wait()
            self.assertEqual(self.states(state), [("done", None), ("done", None)])
            return build()
        self.job.build_targets = update_now_meanwhile
        self.assertIsNone(self.job.resume())
        state = done["state"]
        self.assertEqual(self.job.state()["targets"], state["targets"])  # still "done", not "will try again"
        self.assertEqual(self.timers, [])
        self.assertNotIn("stopped while it was updating", " ".join(self.lines))

    def test_after_a_crash_when_signed_out_no_update_is_promised(self):
        self.crash()
        self.google.sign_out()
        self.assertEqual(self.job.resume(), "waiting")
        self.assertEqual(self.job.state()["targets"], [])  # the page says "sign in" instead
        self.fake.sign_in(self.google)
        self.crash()
        self.assertEqual(self.job.resume(), "waiting")
        self.assertEqual(len(self.job.state()["targets"]), 2)
        self.google.sign_out()  # signed out during the five minutes
        self.timers[-1].fn()
        self.assertEqual(self.job.state()["targets"], [])
        self.assertEqual(self.job.state()["next_retry_at"], None)

    def test_too_much_for_one_client_says_what_helps(self):
        self.targets = [("all", "All clients", self.a), ("c-b", "Bolt Bikes", self.b)]
        self.content = {"all": table(5), "c-b": table(5)}
        self.bump()
        with mock.patch.object(sheets, "CELL_LIMIT", 10):
            self.job.start()
            state = self.wait()
        self.assertEqual(self.states(state), [("failed", "too_much"), ("failed", "too_much")])
        self.assertIn("press Stop using it", state["targets"][0]["message"])  # the All clients Sheet
        self.assertEqual(state["targets"][1]["message"], MESSAGES["too_much_one"])
        self.assertIn("open Edit and press Stop using this Sheet", MESSAGES["too_much_one"])

    def test_a_sheet_written_anyway_is_read_once(self):
        history = {key: Source(table(4)) for key, _, _ in self.targets}
        self.job.build_targets = lambda: [{**t, "read_tables": ready(
            {"content": self.content[t["key"]], "account_daily": table(1, 2), "content_history": history[t["key"]]})}
            for t in self.build()]
        self.bump()
        self.job.start()
        self.wait()  # never written before: hashed as it's sent, not read first
        self.assertEqual([h.reads for h in history.values()], [1, 1])
        self.assertEqual(self.job.sheet_status(self.a)["hash"], tables_digest(
            {"content": table(5), "account_daily": table(1, 2), "content_history": table(4)}))
        before = len(self.fake.writes())
        self.bump()
        self.job.start()
        self.wait()  # unchanged: read once to check, and nothing sent
        self.assertEqual([h.reads for h in history.values()], [2, 2])
        self.assertEqual(len(self.fake.writes()), before)
        self.job.start(force=True)
        self.wait()  # forced: written without reading it first
        self.assertEqual([h.reads for h in history.values()], [3, 3])
        self.assertGreater(len(self.fake.writes()), before)

    def test_replace_anyway_works_once(self):
        self.fake.files[self.a].tabs[0].title = "content"
        self.fake.files[self.a].tabs[0].resize(301, 3)
        self.bump()
        self.job.start()
        state = self.wait()
        self.assertEqual(self.states(state)[0], ("failed", "shrink"))
        self.assertFalse(state["pending"])  # waits for the person, not for a retry
        self.job.allow_shrink_once(self.a)
        self.job.start()
        self.assertEqual(self.states(self.wait())[0], ("done", None))
        saved = self.job.sheet_status(self.a)
        self.assertNotIn("allow_shrink", saved)
        self.assertEqual(self.fake.grid(self.a, "content"), (6, 3))

    def test_a_sheet_that_already_shows_the_numbers_is_left_alone(self):
        self.bump()
        self.job.start()
        self.wait()
        calls = len(self.fake.calls)
        self.bump()  # e.g. a file with nothing new for these Sheets
        self.assertEqual(self.job.start(), "started")
        state = self.wait()
        self.assertEqual(len(self.fake.calls), calls)  # not one call to Google
        self.assertEqual([(t["state"], t["rows"]) for t in state["targets"]], [("done", 5), ("done", 3)])
        self.assertFalse(state["pending"])
        self.assertTrue(self.job.sheet_status(self.a)["ok"])
        self.job.start(force=True)  # "Update Google Sheets now": every Sheet is written again
        self.wait()
        puts = [c[1] for c in self.fake.calls[calls:] if c[0] == "PUT"]
        self.assertEqual({sheet for sheet in (self.a, self.b) if any(sheet in path for path in puts)}, {self.a, self.b})
        # a forced write that fails part-way may leave the Sheet half-written: the next pass writes it again
        self.fake.fail(self.a + r"/values/'content_history'", method="PUT", status=400, message="Not now")
        self.job.start(force=True)
        self.assertEqual(self.states(self.wait())[0], ("failed", "other"))
        self.assertIsNone(self.job.sheet_status(self.a)["hash"])
        calls = len(self.fake.calls)
        self.job.start()
        self.assertEqual(self.states(self.wait()), [("done", None), ("done", None)])
        touched = {sheet for c in self.fake.calls[calls:] for sheet in (self.a, self.b) if sheet in c[1]}
        self.assertEqual(touched, {self.a})

    def test_a_forced_pass_cut_short_stays_forced(self):
        self.bump()
        self.job.start()
        self.wait()
        self.fake.offline = True  # "Update Google Sheets now" while offline: no Sheet is reached
        self.bump()
        self.job.start(force=True)
        self.assertEqual(self.states(self.wait()), [("failed", "offline"), ("skipped", None)])
        self.fake.offline = False
        calls = len(self.fake.calls)
        self.timers[0].fn()  # the timed retry isn't forced, but Bolt Bikes was never rewritten
        self.assertEqual(self.states(self.wait()), [("done", None), ("done", None)])
        puts = [c[1] for c in self.fake.calls[calls:] if c[0] == "PUT"]
        self.assertTrue(any(self.b in path for path in puts))

    def test_a_write_cut_off_half_way_is_never_skipped(self):
        self.bump()
        self.job.start()
        self.wait()
        self.content["c-a"] = table(6)
        seen = {}

        def died(*args, **kwargs):  # the Pi lost power while writing Acme Jane's Sheet
            seen["hash"] = self.job.sheet_status(self.a).get("hash")
            raise KeyboardInterrupt
        with mock.patch.object(sheets, "write_tables", died), mock.patch.object(self.job, "_end_pass"):
            with self.assertRaises(KeyboardInterrupt):
                self.job._pass(0, self.build(), False)
        self.assertIsNone(seen["hash"])
        self.content["c-a"] = table(5)  # the numbers go back to what the Sheet showed before
        self.job.running = False
        self.bump()
        calls = len(self.fake.calls)
        self.job.start()
        self.wait()
        self.assertTrue(any(self.a in c[1] for c in self.fake.calls[calls:] if c[0] == "PUT"))

    def test_retries_while_one_sheet_keeps_failing_leave_the_others_alone(self):
        self.bump()
        self.job.start()
        self.wait()
        self.content["c-a"] = table(6)  # Acme Jane has new numbers, but Google keeps failing on her Sheet
        self.fake.fail(self.a, status=503, times=999)
        self.bump()
        self.job.start()
        self.assertEqual(self.states(self.wait()), [("failed", "google_down"), ("done", None)])
        for timer in (0, 1):
            calls = len(self.fake.calls)
            self.timers[timer].fn()  # five minutes later
            self.wait()
            self.assertFalse(any(self.b in c[1] for c in self.fake.calls[calls:]))
        self.assertTrue(self.job.pending())

    def test_start_answers_before_the_tables_are_built(self):
        building, release, seen = threading.Event(), threading.Event(), {}

        def slow_build():
            seen["thread"] = threading.current_thread()
            building.set()
            self.assertTrue(release.wait(5))
            return self.build()
        self.job.build_targets = slow_build
        self.bump()
        began = time.monotonic()
        self.assertEqual(self.job.start("import"), "started")
        self.assertLess(time.monotonic() - began, 2)
        self.assertTrue(building.wait(5))
        self.assertIsNot(seen["thread"], threading.current_thread())
        self.bump()  # db_lock isn't held while the tables are built...
        release.set()
        state = self.wait()
        self.assertEqual(self.states(state), [("done", None), ("done", None)])
        self.assertTrue(state["pending"])  # ...and a change counted after the build began still waits

    def test_each_sheets_tables_are_built_right_before_it_is_written_and_let_go_after(self):
        alive, held = [], []

        def reader(key: str):
            @contextlib.contextmanager
            def read_tables():
                gc.collect()
                held.append(sum(ref() is not None for ref in alive))  # tables of other Sheets still held
                tables = {"content": Rows(self.content[key]), "account_daily": table(1, 2),
                          "content_history": table(0)}
                alive.append(weakref.ref(tables["content"]))
                yield tables
            return read_tables
        self.job.build_targets = lambda: [{**t, "read_tables": reader(t["key"])} for t in self.build()]
        self.bump()
        gate = self.fake.hold(self.b + "/values/")
        self.assertEqual(self.job.start(), "started")
        self.assertTrue(self.fake.held.wait(5))
        gc.collect()
        self.assertEqual([ref() is None for ref in alive], [True, False])  # Acme Jane's went once written
        self.content["c-a"] = table(7)
        self.bump()
        self.assertEqual(self.job.start(), "queued")
        gate.set()
        self.wait()
        self.assertEqual(held, [0, 0, 0, 0])  # never two Sheets' tables at once, in this pass or the next
        self.assertEqual(self.fake.values(self.a, "content"), table(7))

    def test_a_timed_retry_due_during_a_pass_adds_no_pass(self):
        builds = []
        self.job.build_targets = lambda: builds.append(1) or self.build()
        self.bump()
        self.fake.offline = True
        self.job.start()
        self.wait()
        self.fake.offline = False
        gate = self.fake.hold(self.b + "/values/")
        self.job.start("asked", force=True)
        self.assertTrue(self.fake.held.wait(5))
        self.timers[0].fn()  # the five-minute retry from the first pass comes due now
        gate.set()
        state = self.wait()
        self.assertEqual((len(builds), state["pending"], self.job.again), (2, False, False))

    def test_a_full_google_drive_is_tried_again_later(self):
        self.bump()
        self.fake.fail(self.a, method="PUT", status=403, reason="storageQuotaExceeded", times=99)
        self.job.start()
        state = self.wait()
        self.assertEqual(self.states(state), [("failed", "storage_full"), ("done", None)])
        self.assertIn("out of storage", state["targets"][0]["message"])
        self.assertTrue(state["pending"] and self.timers and self.timers[0].started)

    def test_google_turning_down_this_copy_stops_without_a_timed_retry(self):
        self.bump()
        self.fake.client_id = "newer-client.apps.googleusercontent.com"  # the app file names a deleted client
        self.fake.expire_access_tokens()
        self.job.start()
        state = self.wait()
        self.assertEqual(self.states(state), [("failed", "app_refused"), ("skipped", None)])
        self.assertTrue(state["pending"])
        self.assertEqual(self.timers, [])
        self.assertEqual(self.job.start(), "not_signed_in")

    def test_a_change_is_not_lost_when_its_mark_cant_be_saved(self):
        with mock.patch.object(sheets, "write_private", side_effect=PermissionError("in use")):
            self.bump()  # e.g. Windows: the status file was open for a moment
        self.assertIn("Couldn't note the change for the Sheets", self.lines[-1])
        self.assertEqual(self.job.start("new numbers"), "started")
        self.assertEqual(self.states(self.wait()), [("done", None), ("done", None)])

    def test_status_file_keeps_what_it_doesnt_know_and_the_last_import(self):
        path = self.dir / "sheets_status.json"
        path.write_text(json.dumps({"wanted": 3, "written": 3, "future_key": [1]}), encoding="utf-8")
        receipt = [{"client": "c-a", "name": "Acme Jane", "lines": ["12 new posts"]}]
        self.job.set_last_import("2026-11-09T12:00:00Z", receipt)
        self.bump()
        self.job = self.make_job()  # after a restart
        self.assertEqual(self.job.last_import(), {"at": "2026-11-09T12:00:00Z", "receipt": receipt})
        self.assertTrue(self.job.pending())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["future_key"], data["wanted"]), ([1], 4))
        self.job.start()
        self.wait()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["future_key"], data["written"], data["last_import"]["receipt"]), ([1], 4, receipt))
        self.job.forget_sheet(self.a)
        self.assertIsNone(self.job.sheet_status(self.a))
        self.assertEqual(self.job.sheet_status(self.b)["ok"], True)


class WriteLimitTests(Base):
    write_limit = 60  # Google's real per-minute limit for one user

    def test_twelve_sheets_never_hit_googles_write_limit(self):
        made = [self.google.create_spreadsheet(f"Reddit Tracker – Client {i}", [
            ("content", table(0)[0]), ("account_daily", table(0, 2)[0]), ("content_history", table(0)[0])])
            for i in range(12)]
        targets = [{"key": f"c-{i}", "name": f"Client {i}", "sheet_id": m["id"], "allow_drop": 0, "users": None,
                    "read_tables": ready({"content": table(5), "account_daily": table(4, 2),
                                          "content_history": table(0)})}
                   for i, m in enumerate(made)]
        job = SheetsJob(self.dir / "sheets_status.json", threading.RLock(), lambda: targets, self.google,
                        lambda line: None)
        with job.db_lock:
            job.bump()
        self.assertEqual(job.start(), "started")
        for _ in range(1000):
            state = job.state()
            if not state["running"]:
                break
            time.sleep(0.01)
        self.assertEqual({t["state"] for t in state["targets"]}, {"done"})
        self.assertFalse(state["pending"])
        self.assertGreater(len(self.fake.writes()), 100)
        self.assertEqual(self.fake.rate_limited, 0)
        self.assertTrue(any(s > 0 for s in self.clock.slept))  # it waited instead


if __name__ == "__main__":
    unittest.main()
