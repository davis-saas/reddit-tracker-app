"""The database (rt/store.py): snapshots, dedupe, tables for the Sheets, targets and helpers."""
from __future__ import annotations

import csv
import io
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _p in (str(HERE.parent), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import builders as b  # noqa: E402
from rt import csvfiles, store  # noqa: E402

CONTENT = ["kind", "account", "posted_date", "posted_month", "subreddit", "title", "text", "item_type", "as_of",
           "views", "upvote_rate", "replies", "shares", "awards", "views_7d", "views_14d", "views_30d", "url",
           "item_key"]
HISTORY = ["export_date", "item_key", "kind", "account", "posted_date", "age_days", "views", "upvote_rate",
           "replies", "shares", "awards"]


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "reddit.db"
        self.con = store.connect(self.db)
        self.addCleanup(self.con.close)

    def load(self, text: str, username: str = "acme_jane", **kw) -> str:
        kind, rows = csvfiles.read_report_text(text)
        return store.import_rows(self.con, username, kind, rows, **kw)

    def one(self, sql: str, *args):
        return self.con.execute(sql, args).fetchone()

    def row(self, tables: dict, key: str) -> dict:
        header, *rows = tables["content"]
        return dict(zip(header, next(r for r in rows if r[-1] == key)))

    def tables(self, users) -> dict:
        """build_tables, with content_history read out into a list."""
        return {tab: list(rows) for tab, rows in store.build_tables(self.con, users).items()}


class Storing(StoreCase):
    def test_import_and_tables(self):
        for text in (b.POSTS_CSV, b.COMMENTS_CSV, b.ACCOUNT_CSV):
            self.assertEqual(self.load(text), "acme_jane")
        self.load(b.POSTS_CSV)  # the same file again changes nothing
        self.assertEqual(self.one("SELECT COUNT(*) FROM pro_items")[0], 5)
        # the same title + subreddit + day twice: two posts, numbered in file order
        keys = [r[0] for r in self.con.execute("SELECT item_key FROM pro_items WHERE title='Same title' ORDER BY item_key")]
        self.assertEqual(keys, [keys[0], keys[0] + "-2"])
        self.assertEqual(keys[0], store.post_key("acme_jane", "r/test", "Same title", "2026-10-01"))
        # a profile comment with a blank Subreddit gets "u/" from its link; Reply Count counts the comment itself
        self.assertEqual(self.one("SELECT subreddit, item_type FROM pro_items WHERE item_key='t1_c1'"), ("u/acme_jane", "text"))
        self.assertEqual(self.one("SELECT replies FROM pro_snapshots WHERE item_key='t1_c1'")[0], 2)
        self.assertEqual(self.one("SELECT item_type FROM pro_items WHERE title='Other'")[0], "Image")  # as given
        # a later download of the comments, on day 7 after c1 was posted
        self.load(b.COMMENTS_CSV.replace("2026-10-08", "2026-10-12").replace(",1738,", ",1800,"))
        tables = self.tables({"acme_jane"})
        self.assertEqual(list(tables), ["content", "account_daily", "content_history"])
        self.assertEqual(tables["content"][0], CONTENT)
        self.assertEqual(tables["content_history"][0], HISTORY)
        self.assertEqual(tables["account_daily"], [["account", "date", "views", "upvotes", "followers"],
                                                   ["acme_jane", "2026-10-07", 120, 5, 10],
                                                   ["acme_jane", "2026-10-08", 130, 6, 11]])
        self.assertEqual(len(tables["content"]), 6)
        c1 = self.row(tables, "t1_c1")
        self.assertEqual((c1["views"], c1["as_of"], c1["views_7d"], c1["posted_month"], c1["text"]),
                         (1800, "2026-10-12", 1800, "2026-10", None))
        self.assertEqual([r[2] for r in tables["content"][1:]], sorted((r[2] for r in tables["content"][1:]), reverse=True))
        self.assertEqual(len(tables["content_history"]), 1 + 3 + 2 * 2)
        self.assertEqual(self.tables({"someone_else"})["content"][1:], [])
        # an item from before Reddit tracked views: its 0 views are blank, its other zeros stay
        store.upsert_item(self.con, "t1_old", "comment", "acme_jane", "r/test", "Old", "text", "2025-01-01", "https://x/old/")
        store.snapshot_values(self.con, "2026-10-08", "t1_old", 0, 1, 0, 0, 0)
        old = self.row(self.tables({"acme_jane"}), "t1_old")
        self.assertEqual((old["views"], old["replies"]), (None, 0))
        history = [r for r in self.tables({"acme_jane"})["content_history"] if r[1] == "t1_old"]
        self.assertEqual([(r[0], r[5], r[6], r[8]) for r in history], [("2026-10-08", 645, None, 0)])
        # an older download saved after newer ones still comes in day order
        self.load(b.COMMENTS_CSV.replace("2026-10-08", "2026-10-07").replace(",1738,", ",1500,"))
        tables = self.tables({"acme_jane"})
        self.assertEqual([(r[0], r[6]) for r in tables["content_history"] if r[1] == "t1_c1"],
                         [("2026-10-07", 1500), ("2026-10-08", 1738), ("2026-10-12", 1800)])
        self.assertEqual(self.row(tables, "t1_c1")["as_of"], "2026-10-12")

    def test_a_failure_stores_nothing(self):
        real, calls = store.snapshot_values, []

        def fails_second_time(*a):
            calls.append(a)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            real(*a)
        store.record_upload(self.con, "acme_jane", "comment", "2026-10-08", 2)  # same transaction as the rows
        with mock.patch.object(store, "snapshot_values", fails_second_time), self.assertRaises(RuntimeError):
            self.load(b.COMMENTS_CSV)
        for table in ("pro_items", "pro_snapshots", "uploads"):
            self.assertEqual(self.one(f"SELECT COUNT(*) FROM {table}")[0], 0, table)

    def test_same_day_keeps_the_higher_numbers(self):
        self.load(b.COMMENTS_CSV)
        self.load(b.COMMENTS_CSV.replace(",10,1,1,0,0", ",55,1,1,0,0"))
        self.load(b.COMMENTS_CSV)  # fewer views: neither the views nor the upvote rate go back
        self.assertEqual(self.one("SELECT views, upvote_rate FROM pro_snapshots WHERE item_key='t1_c2'"), (55, 1))
        self.load(b.COMMENTS_CSV.replace(",10,1,1,0,0", ",,0.5,1,0,0"))  # blank views never replace a number
        self.assertEqual(self.one("SELECT views, upvote_rate FROM pro_snapshots WHERE item_key='t1_c2'"), (55, 0.5))
        # the evening's download first, then the morning's: the evening's numbers stay
        self.load(b.POSTS_CSV.replace("100,0.9", "120,0.97"))
        self.load(b.POSTS_CSV.replace("100,0.9", "100,1"))
        self.assertEqual(self.one("SELECT s.views, s.upvote_rate FROM pro_snapshots s JOIN pro_items i USING (item_key) "
                                  "WHERE i.title='Same title' ORDER BY s.views DESC LIMIT 1"), (120, 0.97))

    def test_one_spelling_per_username(self):
        self.load(b.ACCOUNT_CSV)
        self.assertEqual(self.load(b.ACCOUNT_CSV.replace("120,", "125,"), "ACME_JANE"), "acme_jane")
        self.assertEqual(self.con.execute("SELECT account, views FROM account_daily ORDER BY date").fetchall(),
                         [("acme_jane", 125), ("acme_jane", 130)])  # the newer file wins every day it has
        self.load(b.POSTS_CSV, "Acme_Jane")
        self.assertEqual({r[0] for r in self.con.execute("SELECT DISTINCT account FROM pro_items")}, {"acme_jane"})
        self.assertEqual(store.canonical_user(self.con, "ACME_jane", ["Acme_Jane"]), "acme_jane")  # the database first
        self.assertEqual(store.canonical_user(self.con, "BOLT_bikes", ["Acme_Jane", "Bolt_Bikes"]), "Bolt_Bikes")
        self.assertEqual(store.canonical_user(self.con, "Cedar_Lane"), "Cedar_Lane")

    def test_older_account_file_only_fills_gaps(self):
        self.load(b.ACCOUNT_CSV)
        self.load("Date,Views,Upvotes,Followers\n2026-10-05,90,1,9\n2026-10-06,95,1,9\n2026-10-07,50,2,10\n",
                  fill_only=True)
        self.assertEqual(dict(self.con.execute("SELECT date, views FROM account_daily")),
                         {"2026-10-05": 90, "2026-10-06": 95, "2026-10-07": 120, "2026-10-08": 130})

    def test_post_links(self):
        self.load(b.POSTS_CSV + "Another,r/test,Text,2026-10-05,2026-10-08,5,1,1,0,0\n")
        self.load(b.COMMENTS_CSV)
        # the link comes from the account's own comment in the post's thread
        self.assertEqual(self.one("SELECT url FROM pro_items WHERE kind='post' AND title='Another'")[0],
                         "https://www.reddit.com/r/test/comments/def/slug/")
        # two posts with the same title in a subreddit: either could be meant, so no link
        self.assertEqual({r[0] for r in self.con.execute("SELECT url FROM pro_items WHERE title='Same title'")}, {None})
        # a preview stores no links
        self.con.execute("UPDATE pro_items SET url=NULL WHERE kind='post'")
        self.load(b.COMMENTS_CSV, links=False)
        self.assertIsNone(self.one("SELECT url FROM pro_items WHERE kind='post' AND title='Another'")[0])


class Milestones(unittest.TestCase):
    def test_value_at_age(self):
        v = store.value_at_age
        self.assertIsNone(v([], 7))
        self.assertEqual(v([(4, 100), (9, 200)], 7), 160)
        self.assertEqual(v([(6, 50), (8, 70)], 7), 60)
        self.assertEqual(v([(7, 90)], 7), 90)
        self.assertEqual(v([(8, 95)], 7), 95)          # within the 2-day slack
        self.assertIsNone(v([(10, 95)], 7))            # too late to stand in for day 7
        self.assertIsNone(v([(1, 20), (40, 400)], 7))  # gap too wide to guess
        self.assertEqual(v([(30, 500), (44, 520)], 30), 500)
        self.assertEqual(v([(0, 10), (9, 100)], 7), 100)  # gap too wide, but the later one is within the slack
        self.assertEqual(v([(3, 0), (11, 8)], 7), 4)      # a gap of exactly 8 still interpolates
        self.assertEqual(v([(6, 1), (8, 2)], 7), 2)       # Python's round(1.5) == 2
        self.assertEqual(v([(6, 2), (8, 3)], 7), 2)       # round(2.5) == 2 (half to even)
        self.assertIsNone(v([(3, 50)], 7))                # not old enough yet


class Evidence(StoreCase):
    def test_match_counts(self):
        for text in (b.POSTS_CSV, b.COMMENTS_CSV):
            self.load(text)
        posts = csvfiles.read_report_text(b.POSTS_CSV)[1]
        self.assertEqual(store.match_counts(self.con, "post", posts), {"acme_jane": 3})  # a profile post counts once
        comments = csvfiles.read_report_text(b.COMMENTS_CSV)[1]
        self.assertEqual(store.match_counts(self.con, "comment", comments), {"acme_jane": 2})
        many = csvfiles.read_report_text(b.report("comment", b.comment_rows(1200, prefix="m")))[1]
        self.assertEqual(store.match_counts(self.con, "comment", many), {})  # asked in parts of 500

    def test_account_agreement(self):
        days = b.day_rows("2026-09-01", 20, views=lambda i: 0 if i in (4, 5) else 10 + i)
        self.load(b.report("account", days))
        same = csvfiles.read_report_text(b.report("account", days))[1]
        # the newest 3 days and the 2 days with no views on either side are left out
        self.assertEqual(store.account_agreement(self.con, same), {"acme_jane": (15, 15)})
        self.assertEqual(store.account_agreement(self.con, same, {"bolt_bikes"}), {})
        busy = b.day_rows("2026-09-01", 20, views=lambda i: 10 + i)  # views where the saved days have none
        rows = csvfiles.read_report_text(b.report("account", busy))[1]
        self.assertEqual(store.account_agreement(self.con, rows), {"acme_jane": (15, 17)})
        self.assertEqual(store.match_counts(self.con, "account", rows), {"acme_jane": 15})


class Tables(StoreCase):
    def fill(self):
        self.load(b.POSTS_CSV)
        self.load(b.COMMENTS_CSV)
        self.load(b.ACCOUNT_CSV)
        self.load(b.report("post", b.post_rows(3, prefix="B")), "bolt_bikes")
        self.load(b.report("comment", b.comment_rows(4, prefix="b", newest="2026-10-01")), "bolt_bikes")
        self.load(b.report("account", b.day_rows("2026-10-01", 4)), "bolt_bikes")
        self.load(b.report("comment", b.comment_rows(2, prefix="z")), "cedar_lane")

    def test_each_clients_rows_are_the_same_alone_or_with_others(self):
        self.fill()
        both = self.tables({"acme_jane", "bolt_bikes"})
        for user in ("acme_jane", "bolt_bikes"):
            alone = self.tables({user})
            for tab, rows in both.items():
                at = rows[0].index("account")
                self.assertEqual(alone[tab], rows[:1] + [r for r in rows[1:] if r[at] == user], tab)
                self.assertGreater(len(alone[tab]), 1, tab)
        self.assertEqual({tab: rows[1:] for tab, rows in self.tables(set()).items()},
                         {"content": [], "account_daily": [], "content_history": []})
        self.assertEqual(self.tables(None), self.tables({"acme_jane", "bolt_bikes", "cedar_lane"}))

    def test_content_history_is_read_from_the_database_as_it_is_used(self):
        self.fill()
        history = store.build_tables(self.con, {"acme_jane", "bolt_bikes"})["content_history"]
        self.assertIsInstance(history, store.History)  # never a list of every snapshot
        rows = list(history)
        self.assertEqual((rows[0], len(history), list(history)), (store.HISTORY, len(rows), rows))  # again: same
        # only the tab asked for is read (the CSV download): Account days never read the snapshots
        asked = []
        self.con.set_trace_callback(asked.append)
        self.assertEqual(list(store.build_tables(self.con, {"bolt_bikes"}, "account_daily")), ["account_daily"])
        self.assertEqual([sql for sql in asked if "pro_snapshots" in sql], [])
        only = store.build_tables(self.con, {"bolt_bikes"}, "content_history")
        self.assertEqual([sql for sql in asked if "ORDER BY i.rowid" in sql], [])  # nothing walked before it's read
        self.con.set_trace_callback(None)
        self.assertEqual((list(only), list(only["content_history"])),
                         (["content_history"], self.tables({"bolt_bikes"})["content_history"]))

    def test_snapshots_are_read_an_item_at_a_time_through_the_index(self):
        self.fill()
        asked = []
        self.con.set_trace_callback(asked.append)
        tables = store.build_tables(self.con, {"acme_jane", "bolt_bikes"})
        list(tables["content_history"])
        self.con.set_trace_callback(None)
        walks = [sql for sql in asked if "ORDER BY i.rowid" in sql]
        self.assertEqual(len(walks), 2)  # the content tab, then content_history
        for sql in asked:
            plan = " ".join(str(r[-1]) for r in self.con.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertNotIn("TEMP B-TREE", plan, sql)  # never a sort of every snapshot
            if sql in walks:
                self.assertIn("pro_snapshots_item", plan)

    def test_a_long_history_is_never_all_in_memory(self):
        days = [b.shift("2025-01-01", d) for d in range(500)]
        for n in range(60):
            store.upsert_item(self.con, f"t1_{n}", "comment", "acme_jane", "r/test", f"T{n}", "text", days[0], None)
        self.con.executemany("INSERT INTO pro_snapshots VALUES (?,?,?,?,?,?,?)",
                             [(day, f"t1_{n}", float(d), 0.9, 1.0, 0.0, 0.0) for n in range(60)
                              for d, day in enumerate(days)])
        self.con.commit()
        self.con.execute("BEGIN")  # as the Sheets and the CSV download read them
        tracemalloc.start()
        try:
            whole = list(store.build_tables(self.con, {"acme_jane"})["content_history"])
            held = tracemalloc.get_traced_memory()[0]  # what a list of every snapshot costs
            del whole
            tracemalloc.stop()  # (reset_peak is Python 3.10+)
            tracemalloc.start()
            start = tracemalloc.get_traced_memory()[0]
            tables = store.build_tables(self.con, {"acme_jane"})
            rows = sum(1 for _ in tables["content_history"])
            csv_text = sum(len(part) for part in store.export_csv(tables["content_history"]))
            peak = tracemalloc.get_traced_memory()[1] - start
        finally:
            tracemalloc.stop()
            self.con.rollback()
        self.assertEqual((rows, len(tables["content_history"]), len(tables["content"])), (30_001, 30_001, 61))
        self.assertGreater(csv_text, 1_000_000)
        self.assertLess(peak, held / 5)

    def test_sheet_targets(self):
        self.fill()
        acme = b.client("c-1", "Acme Jane", "Acme_Jane", sheet_id="SHEET_A")
        bolt = b.client("c-2", "Bolt Bikes", "bolt_bikes", sheet_id="SHEET_B")
        cedar = b.client("c-3", "Cedar Lane", "cedar_lane", paused=True, sheet_id="SHEET_C")
        data = {"clients": [acme, bolt, cedar], "all_sheet": {"id": "SHEET_ALL", "title": "All"}}
        targets = store.sheet_targets(self.con, data)  # their tables are read later, one Sheet at a time
        self.assertEqual(targets, [
            # the paused client's 2 comments may leave the All clients Sheet
            {"key": "all", "name": "All clients", "sheet_id": "SHEET_ALL", "users": {"acme_jane", "bolt_bikes"},
             "allow_drop": 2},
            {"key": "c-1", "name": "Acme Jane", "sheet_id": "SHEET_A", "users": {"acme_jane"}, "allow_drop": 0},
            {"key": "c-2", "name": "Bolt Bikes", "sheet_id": "SHEET_B", "users": {"bolt_bikes"}, "allow_drop": 0}])
        self.assertEqual({r[1] for r in self.tables(targets[0]["users"])["content"][1:]}, {"acme_jane", "bolt_bikes"})
        # a Sheet shared with another client (even a paused one), or the All clients Sheet itself, is left out
        shared = {"clients": [acme, bolt, {**cedar, "sheet_id": "SHEET_B"}], "all_sheet": {"id": "SHEET_A"}}
        self.assertEqual([t["key"] for t in store.sheet_targets(self.con, shared)], ["all"])
        # no All clients Sheet: only the clients' own; a client without a Sheet has no target
        alone = {"clients": [acme, {**bolt, "sheet_id": None}], "all_sheet": None}
        self.assertEqual([t["key"] for t in store.sheet_targets(self.con, alone)], ["c-1"])
        # no Sheet chosen yet: nothing is read (the Sheets job can say "nothing to do" at once)
        asked = []
        self.con.set_trace_callback(asked.append)
        no_sheet = {"clients": [{**acme, "sheet_id": None}, {**bolt, "sheet_id": None}], "all_sheet": None}
        self.assertEqual(store.sheet_targets(self.con, no_sheet), [])
        self.con.set_trace_callback(None)
        self.assertEqual(asked, [])
        # nobody active: nothing is written (the All clients Sheet keeps its rows)
        paused = {"clients": [{**acme, "paused": True}, cedar], "all_sheet": {"id": "SHEET_ALL"}}
        self.assertEqual(store.sheet_targets(self.con, paused), [])
        self.assertEqual(store.sheet_targets(self.con, {"clients": [], "all_sheet": {"id": "SHEET_ALL"}}), [])

    def test_export_csv_never_makes_formulas(self):
        table = [["title", "views", "age"], ["=HYPERLINK(\"x\")", 1800.0, -3], ["+1", None, 0], ["-x", 2, 1],
                 ["@x", 3, 1], ["\tx", 4, 1], ["\rx", 5, 1], ["Plain, with comma", 6, 1], ["", 7, 1]]
        back = list(csv.reader(io.StringIO("".join(store.export_csv(table)), newline="")))
        self.assertEqual(back, [["title", "views", "age"], ["'=HYPERLINK(\"x\")", "1800.0", "-3"], ["'+1", "", "0"],
                                ["'-x", "2", "1"], ["'@x", "3", "1"], ["'\tx", "4", "1"], ["'\rx", "5", "1"],
                                ["Plain, with comma", "6", "1"], ["", "7", "1"]])


class Database(StoreCase):
    def test_schema_and_foreign_databases(self):
        self.assertEqual(self.one("SELECT value FROM meta WHERE key='schema_version'")[0], "1")
        self.assertEqual(self.one("PRAGMA journal_mode")[0], "wal")
        self.con.execute("DROP TABLE uploads")  # a missing table comes back
        self.con.execute("DROP INDEX pro_snapshots_item")  # and an index added in an update is made
        self.con.commit()
        store.connect(self.db).close()
        self.assertFalse(store.ever_imported(self.con))
        self.assertEqual(self.one("SELECT sql FROM sqlite_master WHERE name='pro_snapshots_item'")[0],
                         "CREATE INDEX pro_snapshots_item ON pro_snapshots (item_key)")
        for setup in ("CREATE TABLE pro_items (item_key TEXT)",
                      "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"):
            other = self.dir / f"other{len(setup)}.db"
            con = sqlite3.connect(str(other))
            con.execute(setup)
            con.commit()
            con.close()
            with self.assertRaises(store.ForeignDatabase) as cm:
                store.connect(other)
            self.assertIn("holds a database from another program", str(cm.exception))
            con = sqlite3.connect(str(other))
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "delete")  # left as it was
            con.close()

    def test_open_read_and_memory_copy(self):
        with self.assertRaises(sqlite3.OperationalError):
            store.open_read(self.dir / "missing.db")
        self.assertFalse((self.dir / "missing.db").exists())
        self.load(b.ACCOUNT_CSV)
        reader = store.open_read(self.db)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM account_daily").fetchone()[0], 2)
        mem = store.memory_copy(reader)
        self.addCleanup(mem.close)
        for table in store.TABLES:  # every table as it is, but not the Sheets' index (a quarter of the copy)
            query = f"SELECT * FROM {table} ORDER BY 1, 2"
            self.assertEqual(mem.execute(query).fetchall(), reader.execute(query).fetchall())
        names = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn("pro_snapshots_item", names)
        self.assertEqual(mem.execute("PRAGMA database_list").fetchall(), [(0, "main", "")])  # nothing left attached
        mem.execute("DELETE FROM account_daily")
        mem.commit()
        self.assertEqual(self.one("SELECT COUNT(*) FROM account_daily")[0], 2)
        again = store.memory_copy(mem)  # a copy of a copy (in memory itself)
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT COUNT(*) FROM account_daily").fetchone()[0], 0)

    def test_backups_keep_the_newest_five(self):
        self.load(b.ACCOUNT_CSV)
        folder = self.dir / "backups"
        made = [store.backup_db(self.con, folder) for _ in range(7)]
        self.assertEqual([p.name for p in made], [f"reddit-before-import.{n}.db" for n in range(1, 8)])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [f"reddit-before-import.{n}.db" for n in range(3, 8)])
        copy = sqlite3.connect(str(made[-1]))
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM account_daily").fetchone()[0], 2)


class Helpers(StoreCase):
    def test_engine_helpers(self):
        me, nobody = {"acme_jane"}, set()
        self.assertIsNone(store.newest_stored(self.con, "post", me))
        empty = store.fingerprint(self.con, "comment", me, "2026-10-08")
        for text in (b.POSTS_CSV, b.COMMENTS_CSV, b.ACCOUNT_CSV):
            self.load(text)
        self.assertEqual(store.newest_stored(self.con, "comment", me), "2026-10-08")
        self.assertEqual(store.newest_stored(self.con, "account", me), "2026-10-08")
        self.assertIsNone(store.newest_stored(self.con, "account", nobody))
        self.assertNotEqual(store.fingerprint(self.con, "comment", me, "2026-10-08"), empty)
        self.assertEqual(store.fingerprint(self.con, "account", nobody, "2026-10-08")[0], 0)
        self.assertEqual((store.stored_count(self.con, "post", me), store.stored_count(self.con, "comment", nobody)),
                         (3, 0))
        self.assertEqual(store.snapshot_counts(self.con, me), {"post": 3, "comment": 2, "account_last": "2026-10-08"})
        self.assertEqual(store.snapshot_counts(self.con, nobody), {"post": 0, "comment": 0, "account_last": None})
        self.assertEqual(store.items_for_usernames(self.con, me), 5 + 2)
        self.assertEqual(store.items_for_usernames(self.con, {"bolt_bikes"}), 0)
        # an upload note is only kept when the import that follows commits it
        self.assertFalse(store.ever_imported(self.con))
        store.record_upload(self.con, "acme_jane", "post", "2026-10-08", 3)
        self.assertTrue(store.upload_recorded(self.con, "post", me, "2026-10-08"))
        self.con.rollback()
        self.assertFalse(store.upload_recorded(self.con, "post", me, "2026-10-08"))
        store.record_upload(self.con, "Acme_Jane", "post", "2026-10-08", 3)
        self.load(b.POSTS_CSV)
        self.assertTrue(store.upload_recorded(self.con, "post", me, "2026-10-08"))
        self.assertFalse(store.upload_recorded(self.con, "comment", me, "2026-10-08"))
        self.assertTrue(store.ever_imported(self.con))

    def test_row_keys(self):
        posts = csvfiles.read_report_text(b.POSTS_CSV)[1]
        self.assertEqual(store.row_keys("post", posts), {("r/test", "Same title", "2026-10-01", 0),
                                                         ("r/test", "Same title", "2026-10-01", 1),
                                                         ("u/acme_jane", "Other", "2026-09-01", 0)})
        self.assertEqual(store.row_keys("comment", csvfiles.read_report_text(b.COMMENTS_CSV)[1]), {"c1", "c2"})
        self.assertEqual(store.row_keys("account", csvfiles.read_report_text(b.ACCOUNT_CSV)[1]),
                         {"2026-10-07", "2026-10-08"})

    def test_client_activity(self):
        me = {"acme_jane"}
        self.assertEqual(store.client_activity(self.con, me),
                         {"post": None, "comment": None, "account": None, "items": 0, "comment_reach": None})
        # an empty Posts download counts as uploaded; the comments' day is their newest export. As the
        # engine does, each upload is noted in the same transaction as its rows
        store.record_upload(self.con, "acme_jane", "post", "2026-10-12", 0)
        self.load(b.report("post"))
        store.record_upload(self.con, "Acme_Jane", "comment", "2026-10-08", 2)
        self.load(b.COMMENTS_CSV)
        store.record_upload(self.con, "acme_jane", "account", "2026-10-08", 2)
        self.load(b.ACCOUNT_CSV)
        self.assertEqual(store.client_activity(self.con, me),
                         {"post": "2026-10-12", "comment": "2026-10-08", "account": "2026-10-08", "items": 2,
                          "comment_reach": None})
        # a busy account: the newest 100 comments only reach back 9 days
        store.record_upload(self.con, "acme_jane", "comment", "2026-10-15", 100)
        self.load(b.report("comment", b.comment_rows(100, day="2026-10-15", newest="2026-10-14", per_day=10,
                                                     prefix="busy")))
        activity = store.client_activity(self.con, me)
        self.assertEqual((activity["comment"], activity["comment_reach"], activity["items"]), ("2026-10-15", 9, 102))
        self.assertEqual(store.client_activity(self.con, {"bolt_bikes"})["items"], 0)
        # a poll never reads the snapshots (they grow every day): only the uploads and the items
        asked = []
        self.con.set_trace_callback(asked.append)
        store.client_activity(self.con, me)
        store.items_for_usernames(self.con, me)
        self.con.set_trace_callback(None)
        self.assertEqual([sql for sql in asked if "pro_snapshots" in sql], [])


if __name__ == "__main__":
    unittest.main()
