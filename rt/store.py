"""The local database: dated snapshots of every post and comment, Account days, and uploads.

Each Reddit Pro download is stored as snapshots of its export day, so the Sheets can show each
item's newest numbers and its views at 7, 14 and 30 days. `users` arguments are sets of
lower-case Reddit usernames (an empty set matches nobody).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import itertools
import operator
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from .csvfiles import COMMENT_LIMIT, num

SCHEMA_VERSION = "1"
MILESTONES = (7, 14, 30)  # views at 7/14/30 days after posting
MILESTONE_SLACK_DAYS = 2  # a snapshot this many days after a milestone may stand in for it
MAX_GAP_DAYS = {7: 8, 14: 10, 30: 16}  # interpolate only between snapshots this close; wider gaps stay blank
# The thread a comment permalink belongs to: .../comments/<post id>/ plus the title slug if any
THREAD = re.compile(r"(https?://(?:www\.)?reddit\.com/(?:r|u|user)/[^/]+/comments/[^/]+/)([^/]+/)?")
TABLES = ("pro_items", "pro_snapshots", "account_daily", "uploads", "meta")
INDEXES = ("pro_snapshots_item",)
# pro_snapshots_item: each item's snapshots, so the Sheets read them one item at a time (never all at once)
SCHEMA = """
CREATE TABLE IF NOT EXISTS pro_items (
    item_key TEXT PRIMARY KEY, kind TEXT, account TEXT, subreddit TEXT, title TEXT,
    item_type TEXT, published TEXT, url TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS pro_snapshots (
    export_date TEXT, item_key TEXT, views REAL, upvote_rate REAL,
    replies REAL, shares REAL, awards REAL,
    PRIMARY KEY (export_date, item_key)
);
CREATE TABLE IF NOT EXISTS account_daily (
    account TEXT, date TEXT, views REAL, upvotes REAL, followers REAL,
    PRIMARY KEY (account, date)
);
CREATE TABLE IF NOT EXISTS uploads (
    account TEXT, kind TEXT, export_date TEXT, rows INTEGER, PRIMARY KEY (account, kind, export_date)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
SCHEMA_INDEXES = "CREATE INDEX IF NOT EXISTS pro_snapshots_item ON pro_snapshots (item_key);"
SCHEMA += SCHEMA_INDEXES
FOREIGN = ("This data folder holds a database from another program. Move it away or start Reddit Tracker "
           "with another --data folder.")
BACKUP_PREFIX = "reddit-before-import."


class ForeignDatabase(Exception):
    """reddit.db exists but wasn't made by this app (e.g. the old tracker's database)."""


# ---------------------------------------------------------------- Connections

def connect(path: Path | str) -> sqlite3.Connection:
    """The database for imports and Sheets, with its tables and indexes created if missing (an index added
    in an update is made at the next start)."""
    con = sqlite3.connect(str(path), timeout=15)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        if names and not ("meta" in names and con.execute(
                "SELECT 1 FROM meta WHERE key='schema_version'").fetchone()):
            raise ForeignDatabase(FOREIGN)
        con.execute("PRAGMA journal_mode=WAL")
        if not names.issuperset(TABLES + INDEXES):
            con.executescript(f"BEGIN; {SCHEMA} INSERT OR IGNORE INTO meta VALUES ('schema_version', "
                              f"'{SCHEMA_VERSION}'); COMMIT;")
    except BaseException:
        con.close()
        raise
    return con


def open_read(path: Path | str) -> sqlite3.Connection:
    """A connection for reading only. Not mode=ro: next to an import in the same process, those
    make the import fail with 'disk I/O error' on macOS."""
    if not Path(path).exists():
        raise sqlite3.OperationalError(f"the database {path} is missing")
    return sqlite3.connect(str(path), timeout=10)


def memory_copy(con: sqlite3.Connection) -> sqlite3.Connection:
    """The database's tables copied into memory, for a preview that saves nothing. Not the Sheets' index:
    the preview doesn't need it, and it would make the copy a quarter bigger. Call it with nothing
    left unsaved on con (under db_lock)."""
    path = next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "")
    mem = sqlite3.connect(":memory:", isolation_level=None)
    try:
        if not path:  # con is in memory itself
            con.backup(mem)
        else:
            mem.executescript(SCHEMA.replace(SCHEMA_INDEXES, ""))
            mem.execute("ATTACH DATABASE ? AS f", (path,))
            mem.execute("BEGIN")  # one moment for every table
            for table in TABLES:
                mem.execute(f"INSERT INTO main.{table} SELECT * FROM f.{table}")
            mem.execute("COMMIT")
            mem.execute("DETACH DATABASE f")
        mem.isolation_level = ""  # as sqlite3.connect() makes it
    except BaseException:
        mem.close()
        raise
    return mem


def backup_db(con: sqlite3.Connection, backups_dir: Path, keep: int = 5) -> Path:
    """reddit-before-import.<n>.db with an increasing n (clocks can jump), newest `keep` kept."""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    found = {}
    for p in backups_dir.glob(BACKUP_PREFIX + "*.db"):
        seq = p.name[len(BACKUP_PREFIX):-3]
        if seq.isdigit():
            found[int(seq)] = p
    dest = backups_dir / f"{BACKUP_PREFIX}{max(found, default=0) + 1}.db"
    out = sqlite3.connect(str(dest))
    try:
        con.backup(out)
    finally:
        out.close()
    for seq in sorted(found)[:max(0, len(found) + 1 - keep)]:
        found[seq].unlink()
    return dest


# ---------------------------------------------------------------- Storing a report

def marks(n: int) -> str:
    return ",".join("?" * n) or "''"


def post_key(account: str, subreddit: str, title: str, published: str) -> str:
    """The Posts download has no post ID: subreddit + title + day is unique per account."""
    raw = "|".join([account.lower(), subreddit.lower(), title.strip(), published])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def upsert_item(con, key, kind, account, subreddit, title, item_type, published, url=None) -> None:
    # Profile posts are "u/name" in the Posts download but "user/name" in comment links
    subreddit = re.sub(r"^user/", "u/", subreddit or "")
    con.execute(
        """INSERT INTO pro_items (item_key, kind, account, subreddit, title, item_type, published, url)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(item_key) DO UPDATE SET item_type=COALESCE(excluded.item_type, item_type),
           subreddit=COALESCE(NULLIF(excluded.subreddit, ''), subreddit),
           url=COALESCE(excluded.url, url)""",
        (key, kind, account, subreddit, title, item_type, published, url))


def snapshot_values(con, export_date, key, views, upvote_rate, replies, shares, awards) -> None:
    """A day's numbers. When that day is already stored the higher counts are kept (a blank never
    replaces a number), and an earlier download's upvote rate (fewer views) never replaces a later one's.
    SQLite's max() of a NULL is NULL, hence the COALESCEs; `views` in SET is the OLD row's."""
    con.execute(
        """INSERT INTO pro_snapshots VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(export_date, item_key) DO UPDATE SET
           views=max(COALESCE(excluded.views, views), COALESCE(views, excluded.views)),
           upvote_rate=CASE WHEN excluded.views < views AND upvote_rate IS NOT NULL THEN upvote_rate
                            ELSE COALESCE(excluded.upvote_rate, upvote_rate) END,
           replies=max(COALESCE(excluded.replies, replies), COALESCE(replies, excluded.replies)),
           shares=max(COALESCE(excluded.shares, shares), COALESCE(shares, excluded.shares)),
           awards=max(COALESCE(excluded.awards, awards), COALESCE(awards, excluded.awards))""",
        (export_date, key, views, upvote_rate, replies, shares, awards))


def canonical_user(con: sqlite3.Connection, name: str, configured: Iterable[str] = ()) -> str:
    """The spelling of this username already in use (usernames ignore case): the database's
    first, then the client list's, else as given."""
    for sql in ("SELECT account FROM account_daily WHERE lower(account)=lower(?) LIMIT 1",
                "SELECT account FROM pro_items WHERE lower(account)=lower(?) LIMIT 1"):
        row = con.execute(sql, (name,)).fetchone()
        if row and row[0]:
            return row[0]
    return next((k for k in configured if k and k.lower() == name.lower()), name)


def fill_post_links(con: sqlite3.Connection, account: str) -> int:
    """Give posts without a link their thread's link, taken from the account's own comments in it
    (same subreddit and title, written on or after the post's day). A title the account used for
    more than one post in a subreddit is skipped: either could be meant."""
    missing = con.execute("SELECT item_key, lower(subreddit), title, published FROM pro_items WHERE kind='post' "
                          "AND url IS NULL AND lower(account)=lower(?)", (account,)).fetchall()
    if not missing:
        return 0
    posts: dict[tuple, int] = {}
    for sub, title in con.execute("SELECT lower(subreddit), title FROM pro_items WHERE kind='post' "
                                  "AND lower(account)=lower(?)", (account,)):
        posts[(sub, title)] = posts.get((sub, title), 0) + 1
    comments: dict[tuple, list] = {}
    for sub, title, published, url in con.execute(
            "SELECT lower(subreddit), title, published, url FROM pro_items WHERE kind='comment' "
            "AND lower(account)=lower(?) AND url IS NOT NULL", (account,)):
        comments.setdefault((sub, title), []).append((published, url))
    filled = 0
    for key, sub, title, published in missing:
        if posts.get((sub, title)) != 1:
            continue
        threads: dict[str, str] = {}
        for written, url in comments.get((sub, title), ()):
            m = THREAD.match(url) if written >= published else None
            if m:  # the link with the title slug if any comment link has it (".../comment/<id>/" has none)
                slug = m.group(2) if m.group(2) and m.group(2) != "comment/" else ""
                threads[m.group(1)] = max(threads.get(m.group(1), ""), slug)
        if len(threads) == 1:
            (root, slug), = threads.items()
            con.execute("UPDATE pro_items SET url=? WHERE item_key=?", (root + slug, key))
            filled += 1
    return filled


def import_rows(con: sqlite3.Connection, username: str, kind: str, rows: list[dict], fill_only: bool = False,
                links: bool = True) -> str:
    """Store a checked report (csvfiles.read_report_text) for this username: all of it, or nothing.
    Commits once, together with anything already done in the open transaction (record_upload).
    Returns the username as stored. fill_only: an older Account report only adds missing days.
    links=False skips filling in post links (the preview)."""
    account = canonical_user(con, username)
    try:
        seen: dict[str, int] = {}
        for r in rows:
            if kind == "post":
                key = post_key(account, r["Subreddit"], r["Post Title"], r["Post Published Date"])
                # Same title + subreddit + day can repeat (reposted ads): numbered in export order
                seen[key] = seen.get(key, 0) + 1
                if seen[key] > 1:
                    key = f"{key}-{seen[key]}"
                upsert_item(con, key, "post", account, r["Subreddit"], r["Post Title"],
                            r["Post Type"], r["Post Published Date"])
                snapshot_values(con, r["Date"], key, num(r["Total Views"]), num(r["Upvote Rate"]),
                                num(r["Total Comments"]), num(r["Total Shares"]), num(r["Total Awards"]))
            elif kind == "comment":
                url = r["Permalink"].strip()
                key = "t1_" + url.rstrip("/").rsplit("/", 1)[-1]  # .../comments/<post>/<slug>/<comment id>/
                sub = r["Subreddit"] or (re.search(r"/(r/[^/]+)/", url) or re.search(r"/(user/[^/]+)/", url)
                                         or [None, ""])[1]
                upsert_item(con, key, "comment", account, sub, r["Post Title"],
                            r["Comment Type"].lower(), r["Comment Published Date"], url)
                # Reddit Pro's "Reply Count" counts the comment itself
                replies = num(r["Reply Count"])
                snapshot_values(con, r["Date"], key, num(r["Total Views"]), num(r["Upvote Rate"]),
                                max(replies - 1, 0) if replies is not None else None,
                                num(r["Total Shares"]), num(r["Total Awards"]))
            else:
                con.execute(f"INSERT OR {'IGNORE' if fill_only else 'REPLACE'} INTO account_daily VALUES (?,?,?,?,?)",
                            (account, r["Date"], num(r["Views"]), num(r["Upvotes"]), num(r["Followers"])))
        if kind != "account" and links:
            fill_post_links(con, account)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return account


# ---------------------------------------------------------------- Whose file is it?

def match_counts(con: sqlite3.Connection, kind: str, rows: list[dict]) -> dict[str, int]:
    """{lower-case username: rows of this report it already has}: evidence of whose file it is.
    Comments by ID; posts by subreddit + title + day (and posts on a user's own profile), each row
    counted once per username; Account days only for a username whose saved days mostly agree."""
    counts: dict[str, int] = {}

    def add(user: str, n: int = 1) -> None:
        counts[user] = counts.get(user, 0) + n

    if kind == "comment":
        keys = ["t1_" + r["Permalink"].strip().rstrip("/").rsplit("/", 1)[-1] for r in rows]
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            for user, n in con.execute(f"SELECT lower(account), COUNT(*) FROM pro_items WHERE item_key IN "
                                       f"({marks(len(part))}) GROUP BY 1", part):
                add(user, n)
    elif kind == "post":
        stored: dict[tuple, set] = {}
        for user, sub, title, published in con.execute(
                "SELECT lower(account), lower(subreddit), title, published FROM pro_items WHERE kind='post'"):
            stored.setdefault((sub, title, published), set()).add(user)
        for r in rows:
            sub = re.sub(r"^user/", "u/", r["Subreddit"] or "").lower()
            owners = set(stored.get((sub, r["Post Title"], r["Post Published Date"]), ()))
            if sub.startswith("u/"):
                owners.add(sub[2:])
            for user in owners:
                add(user)
    else:
        for user, (agree, both) in account_agreement(con, rows).items():
            if agree >= 5 and agree >= both / 2:
                add(user, agree)
    return counts


def account_agreement(con: sqlite3.Connection, rows: list[dict], users: set | None = None) -> dict[str, tuple]:
    """{lower-case username: (days whose numbers agree with the Account report, days compared)}.
    The report's newest 3 days (still changing) and days neither side has views on are left out;
    a day only one side has views on is a disagreement."""
    newest = set(sorted({r["Date"] for r in rows})[-3:])
    mine = {r["Date"]: (num(r["Views"]), num(r["Upvotes"]), num(r["Followers"]))
            for r in rows if r["Date"] not in newest}
    out: dict[str, tuple] = {}
    for user, day, *numbers in con.execute("SELECT lower(account), date, views, upvotes, followers FROM account_daily"):
        if day in mine and (users is None or user in users) and (numbers[0] or mine[day][0]):
            agree, both = out.get(user, (0, 0))
            out[user] = (agree + (tuple(numbers) == mine[day]), both + 1)
    return out


# ---------------------------------------------------------------- Helpers for the import engine

def newest_stored(con, kind: str, users: set) -> str | None:
    """The newest export day of this report (Account: the newest day) stored for these usernames."""
    u = sorted(users)
    if kind == "account":
        return con.execute(f"SELECT MAX(date) FROM account_daily WHERE lower(account) IN ({marks(len(u))})",
                           u).fetchone()[0]
    return con.execute(f"SELECT MAX(s.export_date) FROM pro_snapshots s JOIN pro_items i ON i.item_key=s.item_key "
                       f"WHERE i.kind=? AND lower(i.account) IN ({marks(len(u))})", [kind, *u]).fetchone()[0]


def fingerprint(con, kind: str, users: set, day: str) -> tuple:
    """A summary of what's stored, to tell whether a file added anything (Posts/Comments: the items
    and that export day's numbers; Account: every day)."""
    u = sorted(users)
    if kind == "account":
        return con.execute(f"SELECT COUNT(*), SUM(views), SUM(upvotes), SUM(followers), MAX(date) FROM account_daily "
                           f"WHERE lower(account) IN ({marks(len(u))})", u).fetchone()
    items = con.execute(f"SELECT COUNT(*) FROM pro_items WHERE kind=? AND lower(account) IN ({marks(len(u))})",
                        [kind, *u]).fetchone()
    return items + con.execute(
        f"SELECT COUNT(*), SUM(s.views), SUM(s.replies), SUM(s.shares), SUM(s.upvote_rate) FROM pro_snapshots s "
        f"JOIN pro_items i ON i.item_key=s.item_key WHERE s.export_date=? AND i.kind=? "
        f"AND lower(i.account) IN ({marks(len(u))})", [day, kind, *u]).fetchone()


def upload_recorded(con, kind: str, users: set, day: str) -> bool:
    u = sorted(users)
    return bool(con.execute(f"SELECT 1 FROM uploads WHERE kind=? AND export_date=? AND lower(account) IN "
                            f"({marks(len(u))})", [kind, day, *u]).fetchone())


def record_upload(con, username: str, kind: str, day: str, rows: int) -> None:
    """Note an upload (empty ones too). Never commits: import_rows commits it with the rows."""
    con.execute("INSERT OR IGNORE INTO uploads VALUES (?,?,?,?)", (username, kind, day, rows))


def stored_count(con, kind: str, users: set) -> int:
    u = sorted(users)
    return con.execute(f"SELECT COUNT(*) FROM pro_items WHERE kind=? AND lower(account) IN ({marks(len(u))})",
                       [kind, *u]).fetchone()[0]


def snapshot_counts(con, users: set) -> dict:
    """{"post": items, "comment": items, "account_last": newest Account day} for the receipt."""
    u = sorted(users)
    counts = {"post": 0, "comment": 0}
    counts.update(con.execute(f"SELECT kind, COUNT(*) FROM pro_items WHERE lower(account) IN ({marks(len(u))}) "
                              f"GROUP BY kind", u))
    counts["account_last"] = newest_stored(con, "account", users)
    return counts


def row_keys(kind: str, rows: list[dict]) -> set:
    """What tells a report's rows apart: comment IDs, posts (repeats numbered) or Account days."""
    if kind == "comment":
        return {r["Permalink"].strip().rstrip("/").rsplit("/", 1)[-1] for r in rows}
    if kind == "account":
        return {r["Date"] for r in rows}
    seen: dict[tuple, int] = {}
    for r in rows:
        key = ((r["Subreddit"] or "").lower(), r["Post Title"], r["Post Published Date"])
        seen[key] = seen.get(key, 0) + 1
    return {(*key, n) for key, count in seen.items() for n in range(count)}


def items_for_usernames(con, users: set) -> int:
    """Posts, comments and Account days saved for these usernames (a username with any is locked)."""
    u = sorted(users)
    return sum(con.execute(f"SELECT COUNT(*) FROM {table} WHERE lower(account) IN ({marks(len(u))})", u).fetchone()[0]
               for table in ("pro_items", "account_daily"))


def ever_imported(con) -> bool:
    return bool(con.execute("SELECT 1 FROM uploads LIMIT 1").fetchone())


def client_activity(con, users: set) -> dict:
    """The newest day of each report uploaded for these usernames, how many posts and comments are
    saved, and how many days the newest 100 comments reach back (None below 100 comments). Asked on
    every status poll, so it never reads the snapshots: every import notes its upload (its file's
    newest day) together with its rows."""
    u = sorted(users)
    uploaded = dict(con.execute(f"SELECT kind, MAX(export_date) FROM uploads WHERE lower(account) IN "
                                f"({marks(len(u))}) GROUP BY kind", u))
    out = {k: uploaded.get(k) for k in ("post", "comment")}
    out["account"] = newest_stored(con, "account", users)
    out["items"] = con.execute(f"SELECT COUNT(*) FROM pro_items WHERE lower(account) IN ({marks(len(u))})",
                               u).fetchone()[0]
    published = [r[0] for r in con.execute(
        f"SELECT published FROM pro_items WHERE kind='comment' AND lower(account) IN ({marks(len(u))}) "
        f"ORDER BY published DESC LIMIT {COMMENT_LIMIT}", u)]
    out["comment_reach"] = ((dt.date.fromisoformat(published[0]) - dt.date.fromisoformat(published[-1])).days
                            if len(published) >= COMMENT_LIMIT and out["comment"] else None)
    return out


# ---------------------------------------------------------------- Tables for the Sheets

def value_at_age(points: list[tuple[int, float]], target: int) -> float | None:
    """points = [(age_days, views)] sorted by age: the value at `target` days, interpolated between
    close snapshots, or a snapshot up to 2 days late; else None."""
    before = [p for p in points if p[0] <= target]
    after = [p for p in points if p[0] >= target]
    if before and after:
        (a0, v0), (a1, v1) = before[-1], after[0]
        if a1 == a0:
            return v0
        if a1 - a0 <= MAX_GAP_DAYS.get(target, max(8, target // 2)):
            return round(v0 + (v1 - v0) * (target - a0) / (a1 - a0))
    if after and after[0][0] - target <= MILESTONE_SLACK_DAYS:
        return after[0][1]
    return None


CONTENT = ["kind", "account", "posted_date", "posted_month", "subreddit", "title", "text", "item_type", "as_of",
           "views", "upvote_rate", "replies", "shares", "awards", *[f"views_{d}d" for d in MILESTONES], "url",
           "item_key"]
HISTORY = ["export_date", "item_key", "kind", "account", "posted_date", "age_days", "views", "upvote_rate",
           "replies", "shares", "awards"]
ITEM = "item_key, kind, account, published, subreddit, title, item_type, url, text"  # what the content tab shows
HISTORY_ITEM = "item_key, kind, account, published"  # all content_history needs: less to hold while it's read


def _who(users: set | None, column: str = "account") -> tuple[str, list]:
    """SQL that keeps these usernames' rows (None: everyone), with its arguments."""
    if users is None:
        return "1", []
    return (f"lower({column}) IN ({marks(len(users))})", sorted(users)) if users else ("0", [])


def _views_since(con: sqlite3.Connection, who: tuple) -> dict:
    """Reddit has no views for items from before it started tracking an account (it says 0): per account
    and kind, the posting day of its first item with views. 0 views on anything older are left blank."""
    since: dict[tuple, str] = {}
    for account, kind, published in con.execute(
            f"SELECT account, kind, published FROM pro_items i WHERE {who[0]} AND EXISTS "
            f"(SELECT 1 FROM pro_snapshots s WHERE s.item_key=i.item_key AND s.views > 0)", who[1]):
        k = ((account or "").lower(), kind)
        since[k] = min(since.get(k, published), published)
    return since


def _walk(con: sqlite3.Connection, who: tuple, since: dict, columns: str = ITEM) -> Iterator[tuple]:
    """(item, its snapshots by day) for each of these usernames' items that has any, in the order the items
    were saved: one item's snapshots at a time, read through the pro_snapshots_item index (only each item's
    few snapshots are sorted, never the whole table). `columns`: the item's to read, as ITEM starts."""
    items = {r[0]: r for r in con.execute(f"SELECT {columns} FROM pro_items WHERE {who[0]}", who[1])}
    rows = con.execute(f"SELECT s.* FROM pro_items i CROSS JOIN pro_snapshots s ON s.item_key=i.item_key "
                       f"WHERE {who[0]} ORDER BY i.rowid", who[1])
    for key, group in itertools.groupby(rows, operator.itemgetter(1)):
        item, s = items[key], sorted(group)  # by export_date, the first column (one per day)
        if item[3] < since.get(((item[2] or "").lower(), item[1]), "0000"):
            s = [(*r[:2], None if r[2] == 0 else r[2], *r[3:]) for r in s]
        yield item, s


def _content(walk: Iterator[tuple]) -> list[list]:
    """The content tab: each item's newest numbers and its views at 7/14/30 days, newest posting day first."""
    rows = []
    for (key, kind, account, published, sub, title, itype, url, text), s in walk:
        pub = dt.date.fromisoformat(published)
        points = [((dt.date.fromisoformat(r[0]) - pub).days, r[2]) for r in s if r[2] is not None]
        newest = [next((r[i] for r in reversed(s) if r[i] is not None), None) for i in range(2, 7)]
        rows.append([kind, account, published, published[:7], sub, title, text, itype, s[-1][0],
                     *newest, *[value_at_age(points, d) for d in MILESTONES], url, key])
    return [list(CONTENT)] + sorted(rows, key=lambda r: r[2], reverse=True)


class History:
    """The content_history tab, a row per snapshot: read from the database each time it's iterated (header
    first), so its rows are never all in memory at once. len() counts the header, like a list's. Use it
    inside the read transaction it was made in: its rows are then the same every time."""

    def __init__(self, con: sqlite3.Connection, who: tuple, since: dict):
        self._con, self._who, self._since = con, who, since
        self._len = 1 + con.execute(f"SELECT COUNT(*) FROM pro_items i CROSS JOIN pro_snapshots s ON "
                                    f"s.item_key=i.item_key WHERE {who[0]}", who[1]).fetchone()[0]

    def __len__(self) -> int:
        return self._len

    def __iter__(self) -> Iterator[list]:
        yield list(HISTORY)
        for (key, kind, account, published), s in _walk(self._con, self._who, self._since, HISTORY_ITEM):
            pub = dt.date.fromisoformat(published)
            for r in s:
                yield [r[0], key, kind, account, published, (dt.date.fromisoformat(r[0]) - pub).days, *r[2:]]


def build_tables(con: sqlite3.Connection, users: set | None = None, tab: str | None = None) -> dict:
    """{"content", "account_daily", "content_history"} (or only `tab`) for these usernames (None: everyone):
    content and account_daily as [header, *rows], content_history as History. Call it inside a read
    transaction (con.execute("BEGIN")) kept until the tables are used up, so they all show the same moment."""
    who = _who(users)
    since = _views_since(con, who) if tab != "account_daily" else {}
    tables = {}
    if tab in (None, "content"):
        tables["content"] = _content(_walk(con, who, since))
    if tab in (None, "account_daily"):
        cur = con.execute(f"SELECT * FROM account_daily WHERE {who[0]} ORDER BY account, date", who[1])
        tables["account_daily"] = [[d[0] for d in cur.description]] + [list(r) for r in cur]
    if tab in (None, "content_history"):
        tables["content_history"] = History(con, who, since)
    return tables


class SheetLayoutError(Exception):
    """A Sheet whose tabs aren't the ones Reddit Tracker writes (their headers differ)."""


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def import_sheet_tables(con: sqlite3.Connection, users: dict, content: list, history: list, account: list) -> dict:
    """Take back the numbers a Reddit Tracker Sheet holds (its three tabs as lists of rows, header first) for
    these usernames ({lower-case: spelling to store}). What this computer already has wins: nothing saved
    is replaced or lowered. Rows of other accounts are left out. Returns {"items", "snapshots", "days"}.
    Never commits."""
    for tab, rows, header in (("content", content, CONTENT), ("content_history", history, HISTORY),
                              ("account_daily", account, ["account", "date", "views", "upvotes", "followers"])):
        if rows and [str(h).strip() for h in rows[0][:len(header)]] != header:
            raise SheetLayoutError(tab)
    pad = lambda r, n: [*(r[:n]), *([""] * (n - len(r)))]  # noqa: E731  (Google drops trailing blanks)
    mine = lambda name: users.get(str(name or "").lower())  # noqa: E731
    seen = {"items": 0, "snapshots": 0, "days": 0}
    newest: dict = {}
    for r in content[1:]:
        (kind, acct, posted, _, sub, title, text, itype, as_of, views, rate, replies, shares, awards,
         _7, _14, _30, url, key) = pad(r, len(CONTENT))
        who = mine(acct)
        if not who or not key or kind not in ("post", "comment") or not posted:
            continue
        upsert_item(con, key, kind, who, sub, title, itype or None, posted, url or None)
        if text:
            con.execute("UPDATE pro_items SET text=? WHERE item_key=? AND COALESCE(text, '')=''", (text, key))
        seen["items"] += 1
        if as_of:
            snapshot_values(con, as_of, key, *map(_num, (views, rate, replies, shares, awards)))
            seen["snapshots"] += 1
            newest[(who, kind)] = max(newest.get((who, kind), ""), as_of)
    known = {r[0] for r in con.execute("SELECT item_key FROM pro_items")}
    for r in history[1:]:
        day, key, kind, acct, _, _, views, rate, replies, shares, awards = pad(r, len(HISTORY))
        who = mine(acct)
        if not who or key not in known or not day:
            continue
        snapshot_values(con, day, key, *map(_num, (views, rate, replies, shares, awards)))
        seen["snapshots"] += 1
        newest[(who, kind)] = max(newest.get((who, kind), ""), day)
    for r in account[1:]:
        acct, day, views, upvotes, followers = pad(r, 5)
        who = mine(acct)
        if not who or not day:
            continue
        con.execute("INSERT OR IGNORE INTO account_daily VALUES (?,?,?,?,?)",
                    (who, day, _num(views), _num(upvotes), _num(followers)))
        seen["days"] += 1
    for (who, kind), day in newest.items():  # so each client's page shows when its numbers are from
        record_upload(con, who, kind, day, 0)
    return seen


def sheet_targets(con: sqlite3.Connection, settings_data: dict) -> list[dict]:
    """The Sheets to write: the All clients Sheet first (if chosen), then each active client's own, as
    {"key", "name", "sheet_id", "users", "allow_drop"}. Their tables aren't read here: build_tables(con,
    t["users"]) makes one target's at a time. A client whose Sheet is also another client's or the All
    clients Sheet is left out (nothing gets mixed up). Paused clients are never written, and their rows
    leave the All clients Sheet."""
    clients = settings_data.get("clients") or []
    active = [c for c in clients if not c.get("paused")]
    if not active:
        return []
    all_sheet = (settings_data.get("all_sheet") or {}).get("id")
    used: dict[str, int] = {}
    for c in clients:
        if c.get("sheet_id"):
            used[c["sheet_id"]] = used.get(c["sheet_id"], 0) + 1
    everyone: set = set()
    targets = []
    for c in active:
        users = {c["reddit_username"].lower()} if c.get("reddit_username") else set()
        everyone |= users
        sheet = c.get("sheet_id")
        if sheet and users and used[sheet] == 1 and sheet != all_sheet:
            targets.append({"key": c["id"], "name": c["name"], "sheet_id": sheet, "users": users, "allow_drop": 0})
    if all_sheet:
        # content rows that may leave it: the items (with numbers) of anyone not among the active clients
        where, args = _who(everyone, "COALESCE(account, '')")
        gone = con.execute(f"SELECT COUNT(*) FROM pro_items i WHERE NOT ({where}) AND EXISTS "
                           f"(SELECT 1 FROM pro_snapshots s WHERE s.item_key=i.item_key)", args).fetchone()[0]
        targets.insert(0, {"key": "all", "name": "All clients", "sheet_id": all_sheet, "users": everyone,
                           "allow_drop": gone})
    return targets


def export_csv(table_rows: Iterable[list]) -> Iterator[str]:
    """A table as CSV text for a download, a part at a time. Text starting with = + - @ (or a tab/CR) gets a
    leading ' so a spreadsheet never runs it as a formula; numbers are left as they are."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    out = io.StringIO()
    writer = csv.writer(out)
    for i, row in enumerate(table_rows, 1):
        writer.writerow([cell(v) for v in row])
        if i % 1000 == 0:
            yield out.getvalue()
            out.seek(0)
            out.truncate()
    yield out.getvalue()
