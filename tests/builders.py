"""Synthetic Reddit Pro downloads and clients for the tests (made-up names only).

Rows are dicts keyed by Reddit's own column names; report() turns them into a file's text,
optionally damaged the way Excel or Numbers damage it:

    report("post", [post_row("Hello", views=1200)])
    report("comment", comment_rows(120, day="2026-10-08", sort="top"))     # >= 100 rows, sorted by Top
    report("account", day_rows("2026-08-01", 60, views=lambda i: 1 + i % 3, skip={10, 11}))
    report("account", day_rows("2026-10-01", 3), excel="dates")           # 10/1/26: refused as Excel
    report("post")                                                         # header only (empty download)

Options of report(): bom=True (leading BOM), blank_lines=N (lines of commas at the end), crlf=True,
excel="dates" | "numbers" (numbers of 1,000 or more get a separator) | "semicolons".

Small classic files (account acme_jane): POSTS_CSV (two "Same title" posts on the same day, and a
profile post on u/acme_jane), COMMENTS_CSV (c1 on acme_jane's profile with a blank Subreddit and
Reply Count 3; c2 in r/test), ACCOUNT_CSV (2026-10-07 and 2026-10-08).

client(...) builds a settings client and dropped(...) a file as the page sends it.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Callable, Iterable, Union

HEADERS = {
    "post": ("Post Title", "Subreddit", "Post Type", "Post Published Date", "Date", "Total Views", "Upvote Rate",
             "Total Comments", "Total Shares", "Total Awards"),
    "comment": ("Post Title", "Subreddit", "Comment Type", "Comment Published Date", "Date", "Permalink",
                "Total Views", "Upvote Rate", "Reply Count", "Total Shares", "Total Awards"),
    "account": ("Date", "Views", "Upvotes", "Followers"),
}
_DATES = ("Date", "Post Published Date", "Comment Published Date")
_NUMBERS = ("Total Views", "Upvote Rate", "Total Comments", "Reply Count", "Total Shares", "Total Awards",
            "Views", "Upvotes", "Followers")

POSTS_CSV = """Post Title,Subreddit,Post Type,Post Published Date,Date,Total Views,Upvote Rate,Total Comments,Total Shares,Total Awards
Same title,r/test,Text,2026-10-01,2026-10-08,100,0.9,3,1,0
Same title,r/test,Text,2026-10-01,2026-10-08,50,0.8,1,0,0
Other,u/acme_jane,Image,2026-09-01,2026-10-08,999,1,0,0,0
"""
COMMENTS_CSV = """Post Title,Subreddit,Comment Type,Comment Published Date,Date,Permalink,Total Views,Upvote Rate,Reply Count,Total Shares,Total Awards
A post,,Text,2026-10-05,2026-10-08,https://www.reddit.com/user/acme_jane/comments/abc/slug/c1/,1738,0.95,3,0,0
Another,r/test,Text,2026-10-06,2026-10-08,https://www.reddit.com/r/test/comments/def/slug/c2/,10,1,1,0,0
"""
ACCOUNT_CSV = """Date,Views,Upvotes,Followers
2026-10-07,120,5,10
2026-10-08,130,6,11
"""

Number = Union[int, float]


def post_row(title: str = "A post", sub: str = "r/test", posted: str = "2026-10-01", day: str = "2026-10-08",
             views: Number | None = 100, rate: Number | None = 0.9, comments: Number | None = 3,
             shares: Number | None = 1, awards: Number | None = 0, type: str = "Text") -> dict:
    """One row of a Posts download (lifetime totals as of `day`)."""
    return {"Post Title": title, "Subreddit": sub, "Post Type": type, "Post Published Date": posted, "Date": day,
            "Total Views": views, "Upvote Rate": rate, "Total Comments": comments, "Total Shares": shares,
            "Total Awards": awards}


def comment_row(cid: str = "c1", title: str = "A post", sub: str = "r/test", posted: str = "2026-10-05",
                day: str = "2026-10-08", views: Number | None = 10, rate: Number | None = 1,
                replies: Number | None = 1, shares: Number | None = 0, awards: Number | None = 0,
                type: str = "Text", link: str | None = None) -> dict:
    """One row of a Comments download. Reply Count counts the comment itself, as Reddit's does."""
    where = "user/" + sub[2:] if sub.startswith("u/") else (sub or "r/test")
    return {"Post Title": title, "Subreddit": sub, "Comment Type": type, "Comment Published Date": posted,
            "Date": day, "Permalink": link or f"https://www.reddit.com/{where}/comments/p{cid}/slug/{cid}/",
            "Total Views": views, "Upvote Rate": rate, "Reply Count": replies, "Total Shares": shares,
            "Total Awards": awards}


def day_row(date: str, views: Number | None = 1, upvotes: Number | None = 0, followers: Number | None = 1) -> dict:
    """One day of an Account download."""
    return {"Date": date, "Views": views, "Upvotes": upvotes, "Followers": followers}


def shift(day: str, days: int) -> str:
    """'2026-10-08' moved by a number of days."""
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def _order(rows: list[dict], sort: str, views: str) -> list[dict]:
    """Rows come newest-posted first (Reddit's "New"); "old" reverses them; "top" mixes the posted
    dates and gives the views in falling order, like Reddit's "Top"."""
    if sort == "old":
        return rows[::-1]
    if sort == "top":
        n = len(rows)
        rows = [rows[i] for i in sorted(range(n), key=lambda i: (i * 37) % n)]
        for place, r in enumerate(rows):
            r[views] = (n - place) * 10
    return rows


def post_rows(n: int, day: str = "2026-10-08", newest: str = "2026-10-07", per_day: int = 1, sort: str = "new",
              prefix: str = "P", sub: str = "r/test") -> list[dict]:
    """n posts downloaded on `day`, posted counting back from `newest`, `per_day` a day."""
    rows = [post_row(f"{prefix}{i}", sub=sub, posted=shift(newest, -(i // per_day)), day=day, views=i + 1)
            for i in range(n)]
    return _order(rows, sort, "Total Views")


def comment_rows(n: int | None = None, day: str = "2026-10-08", posted: list[str] | None = None,
                 newest: str = "2026-10-07", per_day: int = 1, sort: str = "new", prefix: str = "c",
                 sub: str = "r/test") -> list[dict]:
    """Comments downloaded on `day`, with IDs prefix0, prefix1… Posted dates: the `posted` list as
    given, else n of them counting back from `newest`, `per_day` a day."""
    if posted is None:
        posted = [shift(newest, -(i // per_day)) for i in range(n or 0)]
    rows = [comment_row(f"{prefix}{i}", title=f"T{i}", sub=sub, posted=p, day=day, views=i)
            for i, p in enumerate(posted)]
    return _order(rows, sort, "Total Views")


def day_rows(start: str, count: int, views: Number | Callable[[int], Number] = 1,
             upvotes: Number | Callable[[int], Number] = 0, followers: Number | Callable[[int], Number] = 1,
             skip: Iterable[int] = ()) -> list[dict]:
    """`count` Account days from `start`; each number may be a function of the day's index. Days
    whose index is in `skip` are left out (a gap)."""
    def value(v, i):
        return v(i) if callable(v) else v
    skipped = set(skip)
    return [day_row(shift(start, i), value(views, i), value(upvotes, i), value(followers, i))
            for i in range(count) if i not in skipped]


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _excel_date(s: str) -> str:
    d = dt.date.fromisoformat(s)
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


def _excel_number(s: str) -> str:
    try:
        v = float(s)
    except ValueError:
        return s
    return f"{v:,.0f}" if v >= 1000 and v.is_integer() else s


def report(kind: str, rows: Iterable[dict] = (), bom: bool = False, blank_lines: int = 0, crlf: bool = False,
           excel: str | None = None) -> str:
    """The text of a Reddit Pro download of this kind ("post", "comment" or "account")."""
    header = HEADERS[kind]
    lines = [[_text(r.get(c)) for c in header] for r in rows]
    if excel in ("dates", "numbers"):
        damage, columns = (_excel_date, _DATES) if excel == "dates" else (_excel_number, _NUMBERS)
        damaged = [[damage(v) if c in columns and v else v for c, v in zip(header, line)] for line in lines]
        if damaged == lines:
            raise ValueError(f"excel={excel!r} changed nothing (numbers need to be 1,000 or more)")
        lines = damaged
    elif excel not in (None, "semicolons"):
        raise ValueError(f"unknown excel damage {excel!r}")
    end = "\r\n" if crlf else "\n"
    out = io.StringIO()
    csv.writer(out, delimiter=";" if excel == "semicolons" else ",", lineterminator=end).writerows([header, *lines])
    return ("﻿" if bom else "") + out.getvalue() + ("," * (len(header) - 1) + end) * blank_lines


def client(cid: str, name: str, username: str, paused: bool = False, sheet_id: str | None = None) -> dict:
    """A client as settings.clients() returns it."""
    return {"id": cid, "name": name, "reddit_username": username, "paused": paused, "sheet_id": sheet_id,
            "sheet_title": None}


def dropped(fid: str, name: str, text: str, modified: int | None = None, client: str | None = None,
            expect: str | None = None) -> dict:
    """A dropped file as the page sends it to preview/import (`modified` in milliseconds)."""
    return {"id": fid, "name": name, "text": text, "modified": modified, "client": client, "expect": expect}


def modified_ms(day: str, hour: int = 9) -> int:
    """A browser lastModified (local time, milliseconds) on that day."""
    return int(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hour)).timestamp() * 1000)


def write(folder: Path, name: str, text: str) -> Path:
    """Save a download into a (temporary) folder, as UTF-8."""
    path = Path(folder) / name
    path.write_bytes(text.encode("utf-8"))
    return path
