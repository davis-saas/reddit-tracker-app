"""Reading and checking Reddit Pro downloads (rt/csvfiles.py) and the test builders."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE.parent), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import builders as b  # noqa: E402
from rt import csvfiles  # noqa: E402
from rt.csvfiles import AS_EXPORTED, ReportError, read_report_text  # noqa: E402


class ReportChecks(unittest.TestCase):
    def refused(self, text: str, words: str, code: str) -> ReportError:
        with self.assertRaises(ReportError) as cm:
            read_report_text(text)
        self.assertIn(words, str(cm.exception))
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def test_excel_dates_name_the_date_column_first(self):
        e = self.refused(b.COMMENTS_CSV.replace("2026-10-06,2026-10-08", "10/6/26,10/8/26"),
                         "row 3: the date '10/8/26' isn't like 2026-10-28", "excel")
        self.assertEqual(e.message, f"row 3: the date '10/8/26' isn't like 2026-10-28. {AS_EXPORTED}")
        self.assertEqual(e.detail, "row 3: the date '10/8/26' isn't like 2026-10-28")
        self.refused(b.report("account", b.day_rows("2026-10-01", 3), excel="dates"), "isn't like 2026-10-28", "excel")
        self.refused(b.ACCOUNT_CSV.replace("2026-10-07", "2026-02-30"), "the date '2026-02-30'", "excel")
        self.refused(b.ACCOUNT_CSV.replace("2026-10-07", ""), "row 2: a date is missing", "excel")

    def test_numbers_must_be_plain(self):
        self.refused(b.COMMENTS_CSV.replace(",1738,", ',"1,738",'),
                     "row 2: '1,738' in the Total Views column isn't a plain number", "excel")
        self.refused(b.report("post", [b.post_row(views=1738)], excel="numbers"), "'1,738' in the Total Views", "excel")
        self.refused(b.ACCOUNT_CSV.replace("130,6,11", "1.7K,6,11"), "'1.7K' in the Views column", "excel")
        self.refused(b.POSTS_CSV.replace("100,0.9", "100,95%"), "'95%' in the Upvote Rate column", "excel")
        # Python reads these as numbers too, but a Sheet can't hold an endless or missing number
        self.refused(b.ACCOUNT_CSV.replace("130,6,11", "inf,6,11"), "'inf' in the Views column", "excel")
        self.refused(b.POSTS_CSV.replace("100,0.9", "100,NaN"), "'NaN' in the Upvote Rate column", "excel")

    def test_damaged_rows(self):
        self.refused(b.COMMENTS_CSV + "Short,r/test,Text,2026-10-07\n",
                     "row 4: it doesn't have the same columns as the header", "bad_row")
        self.refused(b.ACCOUNT_CSV + "2026-10-09,1,2,3,extra\n", "row 4: it doesn't have the same columns", "bad_row")
        self.refused(b.COMMENTS_CSV.replace("https://www.reddit.com/r/test/comments/def/slug/c2/",
                                            "https://www.reddit.com/"), "isn't a link to a Reddit comment", "bad_row")
        # an empty extra cell (a trailing comma) is harmless
        self.assertEqual(len(read_report_text(b.ACCOUNT_CSV + "2026-10-09,1,2,3,\n")[1]), 3)

    def test_wrong_or_incomplete_files(self):
        e = self.refused("Post Title,Comment Published Date,Date\nA,2026-10-05,2026-10-08\n",
                         "not a complete Reddit Pro comment report: no Subreddit, Comment Type", "missing_column")
        self.assertNotIn("Use the file exactly", e.detail)
        self.refused("a,b\n1,2\n", "not a Reddit Pro report (its columns: a, b)", "not_report")
        self.refused("", "none, the file is empty", "not_report")
        self.refused(b.report("account", b.day_rows("2026-10-01", 2), excel="semicolons"),
                     "its columns are separated by ';'", "excel")
        self.refused('Date,"x\0y"\n', "not a plain CSV file", "not_csv")

    def test_not_utf8_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "utf16.csv"
            path.write_bytes("Date,Views\n".encode("utf-16"))
            with self.assertRaises(ReportError) as cm:
                csvfiles.read_report(path)
            self.assertEqual(cm.exception.code, "not_csv")
            self.assertIn("not a plain CSV file", str(cm.exception))
            bom = b.write(Path(d), "acme_jane Account Performance Report.csv", "﻿" + b.ACCOUNT_CSV)
            self.assertEqual(csvfiles.read_report(bom)[0], "account")

    def test_good_files(self):
        self.assertEqual(len(read_report_text(b.COMMENTS_CSV + ",,,,,,,,,,\n,,,,,,,,,,\n")[1]), 2)  # lines of commas
        kind, rows = read_report_text("﻿" + b.ACCOUNT_CSV)
        self.assertEqual((kind, [r["Date"] for r in rows]), ("account", ["2026-10-07", "2026-10-08"]))
        self.assertEqual(read_report_text(b.report("post")), ("post", []))  # a header-only download
        self.assertEqual(read_report_text(b.report("comment", b.comment_rows(3), crlf=True, bom=True))[0], "comment")
        # column order doesn't matter; dates are kept as plain days
        kind, rows = read_report_text("Followers,Upvotes,Views,Date\n10,5,120, 2026-10-07 \n")
        self.assertEqual((kind, rows[0]["Date"]), ("account", "2026-10-07"))

    def test_kind_comes_from_the_header(self):
        self.assertEqual(csvfiles.report_kind(["Comment Published Date", "Post Published Date"]), "post")
        self.assertEqual(csvfiles.report_kind(["Comment Published Date", "Followers"]), "comment")
        self.assertEqual(csvfiles.report_kind(["Date", "Followers"]), "account")
        self.assertIsNone(csvfiles.report_kind(["Date", "Views"]))

    def test_num_and_iso_date(self):
        self.assertEqual(csvfiles.num(" 12 "), 12.0)
        self.assertIsNone(csvfiles.num(""))
        self.assertIsNone(csvfiles.num(None))
        self.assertEqual((csvfiles.num("-3"), csvfiles.num("0.95"), csvfiles.num("1e3")), (-3.0, 0.95, 1000.0))
        for bad in ("1,234", "1.7K", "95%", "inf", "-Infinity", "nan", "1_000"):
            with self.assertRaises(ReportError) as cm:
                csvfiles.num(bad)
            self.assertEqual(cm.exception.code, "excel")
        self.assertEqual(csvfiles.iso_date(" 2026-10-08 "), "2026-10-08")
        for bad in ("10/8/26", "2026-1-8", ""):
            with self.assertRaises(ReportError):
                csvfiles.iso_date(bad)


class Names(unittest.TestCase):
    def test_username_from_reddit_file_names(self):
        for name in ("acme_jane Post Insights Report.csv", "2026-11-03_acme_jane Post Insights Report.csv",
                     "2026-11-03_091502_123456_acme_jane Post Insights Report.csv",
                     "acme_jane Comment Insights Report 2026-11-03_091502_123456.csv",
                     "acme_jane Account Performance Report (1).csv", "ACME_JANE account performance report.csv"):
            self.assertEqual(csvfiles.account_from_filename(name).lower(), "acme_jane", name)
        self.assertEqual(csvfiles.account_from_filename("Copy of Acme Jane Post Insights Report.csv"),
                         "Copy of Acme Jane")
        self.assertIsNone(csvfiles.account_from_filename("posts.csv"))

    def test_username_from_what_people_type(self):
        for text in ("Acme_Jane", "  Acme_Jane ", "u/Acme_Jane", "/u/Acme_Jane/", "https://www.reddit.com/user/Acme_Jane/",
                     "https://old.reddit.com/u/Acme_Jane?utm_source=share", "reddit.com/user/Acme_Jane/performance/",
                     "https://sh.reddit.com/user/Acme_Jane#top", "Acme_Jane Post Insights Report.csv",
                     "2026-11-02_Acme_Jane Post Insights Report.csv",
                     "2026-11-02_091502_123456_Acme_Jane Comment Insights Report.csv"):
            self.assertEqual(csvfiles.username_from(text), ("Acme_Jane", None), text)
        name, why = csvfiles.username_from("Acme Jane")
        self.assertIsNone(name)
        self.assertIn("Did you mean Acme_Jane or Acme-Jane?", why)
        for bad in ("ab", "@Acme_Jane", "a" * 21, "Acme.Jane"):
            name, why = csvfiles.username_from(bad)
            self.assertIsNone(name, bad)
            self.assertIn("3–20 letters, numbers, _ or -", why)
        self.assertEqual(csvfiles.username_from("   "), (None, "Enter the client's Reddit username."))

    def test_default_name(self):
        self.assertEqual(csvfiles.default_name("Acme_Jane"), "Acme Jane")
        self.assertEqual(csvfiles.default_name("bolt--bikes_co"), "bolt bikes co")


class Builders(unittest.TestCase):
    def test_builders_make_files_reddit_would(self):
        self.assertEqual(read_report_text(b.report("post", b.post_rows(4)))[0], "post")
        rows = read_report_text(b.report("comment", b.comment_rows(120, sort="top")))[1]
        posted = [r["Comment Published Date"] for r in rows]
        self.assertGreater(sum(y > x for x, y in zip(posted, posted[1:])), 5)  # looks sorted by Top
        new = [r["Comment Published Date"] for r in read_report_text(b.report("comment", b.comment_rows(120)))[1]]
        self.assertEqual(new, sorted(new, reverse=True))  # Reddit's "New": newest first
        days = read_report_text(b.report("account", b.day_rows("2026-10-01", 5, views=lambda i: i * 10, skip={2})))[1]
        self.assertEqual([(r["Date"], r["Views"]) for r in days],
                         [("2026-10-01", "0"), ("2026-10-02", "10"), ("2026-10-04", "30"), ("2026-10-05", "40")])
        self.assertTrue(b.report("account", [], bom=True).startswith("﻿Date,Views"))
        with self.assertRaises(ValueError):
            b.report("post", [b.post_row(views=5)], excel="numbers")  # nothing to damage


if __name__ == "__main__":
    unittest.main()
