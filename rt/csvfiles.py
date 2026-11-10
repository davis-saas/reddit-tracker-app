"""Reading and checking Reddit Pro's three CSV downloads (Posts, Comments, Account).

The kind of report comes from its header, the Reddit username from its file name. Nothing is
"helpfully" repaired: a file opened and saved in Excel or Numbers (other date format, numbers
like "1,738") is refused with a plain message, before anything is stored.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import re
from pathlib import Path

KINDS = ("post", "comment", "account")
REPORT_TITLES = {"post": "Post Insights Report", "comment": "Comment Insights Report",
                 "account": "Account Performance Report"}
NOUNS = {"post": ("post", "posts"), "comment": ("comment", "comments"), "account": ("day", "days")}
COMMENT_LIMIT = 100  # Reddit Pro's Posts and Comments downloads list only the newest 100
COLUMNS = {
    "post": ("Date", "Post Title", "Subreddit", "Post Type", "Total Views", "Upvote Rate", "Total Comments",
             "Total Shares", "Total Awards", "Post Published Date"),
    "comment": ("Date", "Post Title", "Subreddit", "Comment Type", "Total Views", "Upvote Rate", "Reply Count",
                "Total Shares", "Total Awards", "Comment Published Date", "Permalink"),
    "account": ("Date", "Views", "Upvotes", "Followers"),
}
DATE_COLUMNS = ("Date", "Post Published Date", "Comment Published Date")
NUMBER_COLUMNS = ("Total Views", "Upvote Rate", "Total Comments", "Reply Count", "Total Shares", "Total Awards",
                  "Views", "Upvotes", "Followers")
AS_EXPORTED = "Use the file exactly as Reddit Pro exported it, not opened and saved in Excel or Numbers."
# Reddit names the files "<username> Post Insights Report.csv"; kept copies may start with a date
REPORT_NAME = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}(?:_\d{6})*_)?(.+?)\s+(Post Insights|Comment Insights|Account Performance) Report", re.I)
USERNAME = re.compile(r"[A-Za-z0-9_-]{3,20}")
COMMENT_LINK = re.compile(r"/comments/[^/]+/[^/]+/\w+/?$")


class ReportError(ValueError):
    """A file that can't be used. code: not_csv, not_report, missing_column, excel or bad_row.
    message: the whole sentence; detail: the same without the "Use the file exactly…" advice."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = re.sub(r"\. Use the file exactly.*$", "", message)


def account_from_filename(name: str) -> str | None:
    """The Reddit username in one of Reddit's file names (or a kept copy's), else None."""
    m = REPORT_NAME.match(name)
    return m.group(1).strip() if m else None


def report_kind(header: list[str]) -> str | None:
    if "Post Published Date" in header:
        return "post"
    if "Comment Published Date" in header:
        return "comment"
    if "Followers" in header:
        return "account"
    return None


def num(s: str | None) -> float | None:
    """A plain number as Reddit writes it; blank is None. "1,234", "1.7K", "95%", "inf" or "nan" are refused."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        v = None
    if v is None or not math.isfinite(v) or "_" in s:  # Python also reads "inf", "nan" and "1_000"
        raise ReportError("excel", f"{s!r} isn't a plain number")
    return v


def iso_date(s: str | None) -> str:
    """'2026-10-28' as is; anything else (e.g. '10/28/26' after Excel) is an error."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return dt.date.fromisoformat(s).isoformat()
        except ValueError:
            pass
    raise ReportError("excel", f"the date {s!r} isn't like 2026-10-28" if s else "a date is missing")


def read_report_text(text: str) -> tuple[str, list[dict]]:
    """Check a Reddit Pro download: (kind, rows). Raises ReportError saying what's wrong before
    anything is stored. A header-only file is fine (rows == [])."""
    if "\0" in text:  # Python 3.11+ csv no longer refuses NUL bytes, so check on every version
        raise ReportError("not_csv", f"not a plain CSV file (line contains NUL). {AS_EXPORTED}")
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""))
        header = reader.fieldnames or []
        raw = list(reader)
    except csv.Error as e:
        raise ReportError("not_csv", f"not a plain CSV file ({e}). {AS_EXPORTED}") from None
    kind = report_kind(header)
    if kind is None:
        if len(header) == 1 and ";" in header[0] and "Date" in header[0]:
            raise ReportError("excel", f"its columns are separated by ';', so it was saved by Excel or Numbers. "
                                       f"{AS_EXPORTED}")
        shown = re.sub(r"[^\x20-\x7e]", "?", ", ".join(header))[:120]
        raise ReportError("not_report", f"not a Reddit Pro report (its columns: {shown or 'none, the file is empty'})")
    missing = [c for c in COLUMNS[kind] if c not in header]
    if missing:
        raise ReportError("missing_column", f"not a complete Reddit Pro {kind} report: no {', '.join(missing)} "
                                            f"column. {AS_EXPORTED}")
    rows = []
    for i, r in enumerate(raw, start=2):  # row 1 is the header, as a spreadsheet numbers them
        values = [v for k, v in r.items() if k is not None]
        if not any((v or "").strip() for v in values):
            continue  # an empty line (Excel can leave lines of commas)
        code = "bad_row"
        try:
            if None in values or any((v or "").strip() for v in r.get(None) or []):
                raise ValueError("it doesn't have the same columns as the header")
            code = "excel"
            for c in DATE_COLUMNS:
                if c in r:
                    r[c] = iso_date(r[c])
            for c in NUMBER_COLUMNS:
                if c in r:
                    try:
                        num(r[c])
                    except ValueError:
                        raise ValueError(f"{r[c].strip()!r} in the {c} column isn't a plain number") from None
            code = "bad_row"
            if kind == "comment" and not COMMENT_LINK.search(r["Permalink"].strip()):
                raise ValueError(f"{r['Permalink']!r} isn't a link to a Reddit comment")
        except ValueError as e:
            raise ReportError(code, f"row {i}: {e}. {AS_EXPORTED}") from None
        rows.append(r)
    return kind, rows


def read_report(path: Path | str) -> tuple[str, list[dict]]:
    """read_report_text for a file on disk (UTF-8, with or without a BOM)."""
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ReportError("not_csv", f"not a plain CSV file ({e}). {AS_EXPORTED}") from None
    return read_report_text(text)


def username_from(text: str | None) -> tuple[str | None, str | None]:
    """(Reddit username, None) from 'Name', 'u/Name', '/u/Name/', a reddit.com/user/Name link or one
    of Reddit's file names; otherwise (None, a plain reason)."""
    s = (text or "").strip()
    if not s:
        return None, "Enter the client's Reddit username."
    m = (re.search(r"reddit\.com/(?:user|u)/([^/?#\s]+)", s)
         or re.fullmatch(r"/?u/([^/\s]+)/?", s)
         or REPORT_NAME.match(s))
    name = m.group(1).strip() if m else s
    if USERNAME.fullmatch(name):
        return name, None
    hint = re.sub(r"\s+", "_", name)
    if " " in name and USERNAME.fullmatch(hint):
        return None, (f"That's not a Reddit username: usernames have no spaces. Did you mean {hint} "
                      f"or {hint.replace('_', '-')}?")
    return None, "That's not a Reddit username. Usernames are 3–20 letters, numbers, _ or -, with no spaces."


def default_name(username: str) -> str:
    """A client name made from a username: 'Acme_Jane' -> 'Acme Jane'."""
    return re.sub(r"[_-]+", " ", username).strip()
