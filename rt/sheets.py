"""Writing the three tabs to a Google Sheet, and the background job that keeps every Sheet up to date."""
from __future__ import annotations

import hashlib
import itertools
import json
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from rt import VERSION
from rt.google import MESSAGES, GoogleError, problem, utc_stamp, write_private

TABS = ("content", "account_daily", "content_history")
PART_BYTES = 2_000_000      # Google advises at most 2 MB per request; bigger tabs go in parts
CELL_LIMIT = 10_000_000     # cells in one spreadsheet (Google's limit)
CELL_CHARS = 49_000         # a cell holds at most 50,000 characters
RETRY_SECONDS = 300
ANSWER_SECONDS = 0.3        # start() waits at most this long to learn whether there's anything to write
AFTER_CRASH = "Not updated yet: Reddit Tracker will try again in a few minutes."
# Problems only a person can fix: a pass that ends with them still counts as complete
LASTING = frozenset({"not_found", "read_only", "trashed", "protected", "api_off", "shrink", "not_a_sheet",
                     "excel_file", "shared", "too_big", "too_much", "drive_full"})
# The sign-in (or google_app.json) is gone: stop, keep it pending, no timed retry
STOP_PASS = ("rejected", "signed_out", "app_refused")
META_FIELDS = ("properties.title,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount,"
               "frozenRowCount)),charts(chartId))")


class ShrinkError(GoogleError):
    """A Sheet would lose more than half its rows: refused, so an old or missing database never wipes it."""

    def __init__(self, before: int, after: int):
        super().__init__("shrink", f"This Sheet has {before} rows for these clients but Reddit Tracker on this "
                         f"computer has only {after} (it may have been filled by an older tracker). Press Keep the "
                         "Sheet's numbers to copy them onto this computer, or Use this computer's numbers to "
                         "overwrite the Sheet.")
        self.before, self.after = before, after


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _column(n: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


def _cell(v):
    return "" if v is None else (v[:CELL_CHARS] if isinstance(v, str) else v)


def _header(rows: Iterable[list]) -> list:
    """A tab's first row, whether its rows are a list or read as they're used (store.History)."""
    return next(iter(rows))


def _parts(rows: Iterable[list]) -> Iterator[tuple[int, list[list]]]:
    """(first row number, rows) a part at a time, each part under PART_BYTES as JSON."""
    part, first, size = [], 1, 0
    for i, r in enumerate(rows, 1):
        n = len(json.dumps(r)) + 1
        if part and size + n > PART_BYTES:
            yield first, part
            part, first, size = [], i, 0
        part.append(r)
        size += n
    yield first, part


def _resize(tab_id: int, rows: int, cols: int) -> dict:
    return {"updateSheetProperties": {
        "properties": {"sheetId": tab_id, "gridProperties": {"rowCount": rows, "columnCount": cols}},
        "fields": "gridProperties/rowCount,gridProperties/columnCount"}}


def _hashed(rows: Iterable[list], h) -> Iterator[list]:
    """The rows as they go by, hashed into h 5000 at a time: never a whole tab in memory."""
    rows = iter(rows)
    for chunk in iter(lambda: list(itertools.islice(rows, 5000)), []):
        h.update(repr(chunk).encode("utf-8"))
        yield from chunk


def _digest(tab_hashes: dict) -> str:
    h = hashlib.sha256(VERSION.encode("utf-8"))  # with the version: an update rewrites each Sheet once
    for name in sorted(tab_hashes):
        h.update(name.encode("utf-8") + b"\0" + tab_hashes[name].digest())
    return h.hexdigest()


def tables_digest(tables: dict) -> str:
    """A fingerprint of the tables, so a Sheet that already shows them is left alone. write_tables makes
    the same one from the rows it sends (a Sheet that's written anyway isn't read twice)."""
    hashes = {name: hashlib.sha256() for name in tables}
    for name, rows in tables.items():
        for _ in _hashed(rows, hashes[name]):
            pass
    return _digest(hashes)


def _tabs(meta: dict) -> dict[str, dict]:
    """{lower-case title: tab}: Google tab names are unique whatever their case ("Content" is ours)."""
    out = {}
    for s in meta.get("sheets") or []:
        p, grid = s["properties"], s["properties"].get("gridProperties") or {}
        out[p["title"].lower()] = {"id": p["sheetId"], "title": p["title"], "rows": grid.get("rowCount", 0),
                                   "cols": grid.get("columnCount", 0), "frozen": grid.get("frozenRowCount", 0),
                                   "charts": bool(s.get("charts"))}
    return out


def _shrink_guard(google, sheet_id: str, content_tab: dict, header: list, rows_out: int, allow_drop: int,
                  users: set[str] | None) -> None:
    """Refuse when the content tab would lose more than half of this Sheet's own rows (grid rows count)."""
    before = have = content_tab["rows"] - 1

    def too_small(may_go: int) -> bool:
        return before >= 100 and rows_out < before / 2 and rows_out < (before - may_go) * 0.99
    if too_small(allow_drop) and users is not None and "account" in header:
        # Only this Sheet's own accounts count: other accounts' rows (a copied Sheet) and empty rows may go
        col = _column(header.index("account") + 1)
        got = google.values_get(sheet_id, f"{_quote(content_tab['title'])}!{col}1:{col}",
                                {"majorDimension": "COLUMNS"})
        accounts = (got.get("values") or [[]])[0][1:]
        have = sum(str(a or "").lower() in users for a in accounts)
        allow_drop = max(allow_drop, before - have)
    if too_small(allow_drop):
        raise ShrinkError(have, rows_out)


def _add_tabs(google, sheet_id: str, names: list[str], size: dict, tabs: dict) -> None:
    def add(missing: list[str]) -> None:
        reply = google.batch_update(sheet_id, [{"addSheet": {"properties": {
            "title": n, "gridProperties": {"rowCount": size[n][0], "columnCount": size[n][1]}}}} for n in missing])
        for r in reply.get("replies") or []:
            p, grid = r["addSheet"]["properties"], r["addSheet"]["properties"]["gridProperties"]
            tabs[p["title"].lower()] = {"id": p["sheetId"], "title": p["title"], "rows": grid["rowCount"],
                                        "cols": grid["columnCount"], "frozen": 0, "charts": False}
    try:
        add(names)
    except GoogleError:
        # A retried request may have added them already: look again before adding what's still missing
        tabs.update(_tabs(google.get_meta(sheet_id, META_FIELDS)))
        still = [n for n in names if n not in tabs]
        if still:
            add(still)


def write_tables(google, sheet_id: str, tables: dict, allow_drop: int = 0, users: set[str] | None = None,
                 allow_shrink: bool = False) -> dict:
    """Write {tab: [header, *rows]} to a Sheet: {"title", "rows", "digest", "tabs"} (content rows;
    tables_digest of what was sent; {tab: its numeric id}, which a Data Studio link names). A tab's rows may also come from a source with len() that can be iterated more than once
    (store.History): sent a part at a time. Overwrites in place and trims, so a tab is never empty while
    Looker Studio reads it. allow_drop: content rows that may leave on purpose; users: the lower-case
    usernames this Sheet is for (other accounts' rows already in it may go too)."""
    rows_out = len(tables["content"]) - 1 if "content" in tables else 0
    meta = google.get_meta(sheet_id, META_FIELDS)
    before = _tabs(meta)
    if "content" in before and "content" in tables and not allow_shrink:
        _shrink_guard(google, sheet_id, before["content"], _header(tables["content"]), rows_out, allow_drop, users)
    # at least 2 rows, and one more than the rows someone froze (Google keeps a row below them)
    size = {name: (max(len(rows), 2, (before.get(name) or {}).get("frozen", 0) + 1), len(_header(rows)))
            for name, rows in tables.items()}
    others = sum(t["rows"] * t["cols"] for title, t in before.items() if title not in tables)
    own = sum(r * c for r, c in size.values())
    if own > CELL_LIMIT:
        raise GoogleError("too_much")  # no Sheet can hold it, so a new one won't help
    if own + others > CELL_LIMIT:
        raise GoogleError("too_big")
    tabs = dict(before)
    missing = [name for name in tables if name not in tabs]
    if missing:
        _add_tabs(google, sheet_id, missing, size, tabs)
    # Tabs that grow get their room first (one request), so every part lands inside the grid; each tab
    # is trimmed right after it's written, so a failure on a later tab never leaves old rows below new ones
    had = {name: (tabs[name]["rows"], tabs[name]["cols"]) for name in tables}
    room = {name: (max(had[name][0], size[name][0]), max(had[name][1], size[name][1])) for name in tables}
    grow = [_resize(tabs[name]["id"], *room[name]) for name in tables if room[name] != had[name]]
    if grow:
        google.batch_update(sheet_id, grow)
    hashes = {name: hashlib.sha256() for name in tables}
    for name, rows in tables.items():
        tab = _quote(tabs[name]["title"])
        # each row is cleaned as it joins its part: never a second copy of the whole tab, nor of a part
        for first, part in _parts([_cell(v) for v in r] for r in _hashed(rows, hashes[name])):
            google.values_update(sheet_id, f"{tab}!A{first}", part)
        if size[name][0] > len(rows):  # blank the rows the minimum size keeps below the data
            google.values_batch_clear(sheet_id, [f"{tab}!{len(rows) + 1}:{size[name][0]}"])
        if room[name] != size[name]:
            google.batch_update(sheet_id, [_resize(tabs[name]["id"], *size[name])])
    try:
        _remove_empty_default_tab(google, sheet_id, before)
    except GoogleError:
        pass  # only tidying (a lost reply makes its retry fail with "No grid with id"); tried again next time
    return {"title": (meta.get("properties") or {}).get("title"), "rows": rows_out, "digest": _digest(hashes),
            "tabs": {name: tabs[name]["id"] for name in tables}}


def _remove_empty_default_tab(google, sheet_id: str, before: dict) -> None:
    """A new spreadsheet's only tab (any locale's "Sheet1") goes once ours exist, if it holds nothing."""
    if len(before) != 1:
        return
    (key, tab), = before.items()
    if key in TABS or tab["charts"]:
        return
    got = google.values_get(sheet_id, _quote(tab["title"]))
    if any(str(v).strip() for row in got.get("values") or [] for v in row):
        return
    google.batch_update(sheet_id, [{"deleteSheet": {"sheetId": tab["id"]}}])


class SheetsJob:
    """Writes every Sheet in the background, one pass at a time, and remembers how each went in
    sheets_status.json (its only writer). A change counts as written only when a pass that started
    after it got through every Sheet, or stopped on a problem only a person can fix. A Sheet whose
    last write worked and already shows what a pass would write is left alone, unless forced.
    build_targets() lists the Sheets ({"key", "name", "sheet_id", "users", "allow_drop", "read_tables"});
    `with t["read_tables"]() as tables` builds one Sheet's tables, right before it's written."""

    def __init__(self, status_path: Path, db_lock, build_targets: Callable[[], list], google,
                 log: Callable[[str], None], clock: Callable[[], float] = time.time, timer=threading.Timer):
        self.path = Path(status_path)
        self.db_lock, self.build_targets, self.google, self.log = db_lock, build_targets, google, log
        self.clock, self.timer = clock, timer
        self._lock = threading.RLock()
        self.running = self.again = self._force = False
        self.targets: list[dict] = []
        self.started_at = self.finished_at = self.next_retry_at = None
        self._retry_timer = None

    # ---------------------------------------------------------------- sheets_status.json

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _change(self, change: Callable[[dict], None]) -> None:
        with self._lock:
            data = self._load()
            change(data)
            write_private(self.path, json.dumps(data, indent=1).encode("utf-8"))

    def bump(self) -> None:
        """Note a change the Sheets must show. Call it while holding db_lock, then start()."""
        def change(d):
            d["wanted"] = int(d.get("wanted") or 0) + 1
        try:
            self._change(change)
        except OSError as e:  # the pass start() runs still takes the change; only a restart could miss it
            self.log(f"Couldn't note the change for the Sheets: {e}")

    def pending(self) -> bool:
        d = self._load()
        return int(d.get("wanted") or 0) > int(d.get("written") or 0)

    def sheet_status(self, sheet_id: str) -> dict | None:
        entry = (self._load().get("sheets") or {}).get(sheet_id)
        return dict(entry) if isinstance(entry, dict) else None

    def allow_shrink_once(self, sheet_id: str) -> None:
        """Let the next write replace this Sheet even if it shrinks a lot ("Replace anyway")."""
        def change(d):
            d.setdefault("sheets", {}).setdefault(sheet_id, {})["allow_shrink"] = True
        self._change(change)

    def _drop_hashes(self, sheet_ids: list) -> None:
        """Until a write has finished, a Sheet may hold anything: never skip it as unchanged."""
        def change(d):
            for sheet_id in sheet_ids:
                entry = (d.get("sheets") or {}).get(sheet_id)
                if isinstance(entry, dict):
                    entry.pop("hash", None)
        try:
            self._change(change)
        except OSError as e:
            self.log(f"Couldn't save how the Sheets went: {e}")

    def rewrite(self, sheet_id: str) -> None:
        """Write this Sheet at the next pass even if its numbers haven't changed (the others stay skipped).
        "asked" counts these, so a pass that read the Sheet's status before this can't record it as done."""
        def change(d):
            entry = (d.get("sheets") or {}).get(sheet_id)
            if isinstance(entry, dict):
                entry.pop("hash", None)
                entry["asked"] = int(entry.get("asked") or 0) + 1
        try:
            self._change(change)
        except OSError as e:
            self.log(f"Couldn't save how the Sheets went: {e}")

    def set_tabs(self, sheet_id: str, tabs: dict) -> None:
        """Keep a Sheet's {tab: id} found outside a write (a Sheet last written before they were kept)."""
        def change(d):
            entry = d.setdefault("sheets", {}).setdefault(sheet_id, {})
            entry["tabs"] = tabs
        self._change(change)

    def forget_sheet(self, sheet_id: str) -> None:
        def change(d):
            (d.get("sheets") or {}).pop(sheet_id, None)
        self._change(change)

    def set_last_import(self, at: str, receipt: list) -> None:
        def change(d):
            d["last_import"] = {"at": at, "receipt": receipt}
        self._change(change)

    def last_import(self) -> dict | None:
        return self._load().get("last_import")

    def _record(self, sheet_id: str, ok: bool, code=None, message=None, rows=None, title=None,
                digest: str | None = None, asked: int = 0, tabs: dict | None = None) -> None:
        """asked: the Sheet's "asked" count when the pass read its status (see rewrite). tabs: {tab: id}
        as the write found them (kept from before when not given)."""
        now = utc_stamp(self.clock())

        def change(d):
            sheets = d.setdefault("sheets", {})
            prev = sheets.get(sheet_id) or {}
            # hash: what a fully successful write left in the Sheet (a failed one may have left half), unless
            # it was asked to be written anew meanwhile
            fresh = int(prev.get("asked") or 0) == asked
            entry = {"ok": ok, "code": code, "message": message, "rows": rows, "at": now,
                     "ok_at": now if ok else prev.get("ok_at"), "title": title or prev.get("title"),
                     "hash": digest if ok and fresh else None}
            if prev.get("asked"):
                entry["asked"] = prev["asked"]
            if tabs or prev.get("tabs"):
                entry["tabs"] = tabs or prev["tabs"]
            if not ok and prev.get("allow_shrink"):
                entry["allow_shrink"] = True  # used up only by a write that worked
            sheets[sheet_id] = entry
        try:
            self._change(change)
        except OSError as e:
            self.log(f"Couldn't save how the Sheets went: {e}")

    # ---------------------------------------------------------------- running

    def start(self, reason: str = "", force: bool = False) -> str:
        """Run one pass in the background (or once more after the current one): "started", "queued",
        "not_signed_in", "nothing_to_do" (no Sheet to write) or "settings_broken". force: rewrite every
        Sheet, even one that already shows these numbers. Returns within ANSWER_SECONDS."""
        with self._lock:
            if self.running:
                self.again, self._force = True, self._force or force
                return "queued"
        if not self.google.status().get("signed_in"):
            return "not_signed_in"
        answer = queue.SimpleQueue()
        with self._lock:
            self._force = self._force or force
            if self.running:
                self.again = True
                return "queued"
            self.running = True
            self._show(self.targets)  # while the tables are built: the Sheets of last time, waiting
        threading.Thread(target=self._run, args=(answer.put, reason), daemon=True).start()
        try:
            return answer.get(timeout=ANSWER_SECONDS)
        except queue.Empty:  # still building the tables: there is something to write
            return "started"

    def resume(self) -> str | None:
        """At startup: finish an update a shutdown cut off. If the last run died in the middle of a pass (it
        may have run out of memory), it waits for the timed retry instead of starting that pass again at once."""
        if not self.pending():
            return None
        if not self._load().get("pass_running"):
            return self.start("resumed")
        try:
            targets = list(self.build_targets()) if self.google.status().get("signed_in") else []
        except Exception:
            targets = []  # the pass that tries again says what's wrong
        with self._lock:
            if self.running or self.started_at is not None:
                return None  # "Update now" or an import got there first
            self._show(targets)  # until then the page says they're not up to date
            self.started_at = None
            for t in self.targets:
                t.update(state="skipped", message=AFTER_CRASH)
            if self._retry_timer is not None:
                self._retry_timer.cancel()
            self._arm_retry()
        self.log("Reddit Tracker stopped while it was updating the Google Sheets. It will try again in 5 minutes.")
        return "waiting"

    def close(self) -> None:
        """Reddit Tracker is stopping on purpose: a pass cut off now (or waiting after a crash) is
        finished at once at the next start."""
        try:
            if self.running or self._load().get("pass_running"):
                self._change(lambda d: d.pop("pass_running", None))
        except OSError:
            pass  # then the next start waits for the timed retry

    def _show(self, targets: list) -> None:
        self.targets = [{"key": t["key"], "name": t["name"], "sheet_id": t["sheet_id"], "state": "waiting",
                         "rows": None, "code": None, "message": None} for t in targets]
        self.started_at, self.finished_at = utc_stamp(self.clock()), None

    def _mark(self, i: int, state: str, **info) -> None:
        with self._lock:
            self.targets[i].update(state=state, **info)

    def _run(self, tell: Callable[[str], None], reason: str) -> None:
        """The background thread: passes until no more are asked for. Only the first one answers start()."""
        while True:
            outcome = self._one_pass(tell, reason)
            with self._lock:
                last, self.again = not self.again, False
                if last:
                    self.running = False
            tell(outcome)
            if last:
                return
            tell, reason = (lambda outcome: None), ""

    def _one_pass(self, tell: Callable[[str], None], reason: str) -> str:
        # Each Sheet's tables are built in this thread right before it's written, and let go once it's done
        with self._lock:
            force, self._force = self._force, False
        gen = int(self._load().get("wanted") or 0)  # read first: a change counted here is in the tables
        try:  # cleared when the pass ends: still there at the next start means this run died in it (resume)
            self._change(lambda d: d.update(pass_running=utc_stamp(self.clock())))
        except OSError as e:
            self.log(f"Couldn't save how the Sheets went: {e}")
        try:
            targets = list(self.build_targets())
        except Exception as e:
            with self._lock:
                self._force = self._force or force
            broken = getattr(e, "code", None) == "broken_file"  # waits for the settings to be fixed
            if not broken:
                self.log(f"The Google Sheets update stopped: {e}")
            self._end_pass(gen, complete=False, retry=not broken)
            return "settings_broken" if broken else "started"
        with self._lock:
            self._show(targets)
        if not targets:
            self._end_pass(gen, complete=True, retry=False)
            return "nothing_to_do"
        if reason:
            self.log(f"Updating the Google Sheets ({reason}).")
        tell("started")
        try:
            self._pass(gen, targets, force)
        except Exception as e:  # never leave the page saying "Updating…"
            self.log(f"The Google Sheets update stopped: {e}")
            self._end_pass(gen, complete=False, retry=True)
        return "started"

    def _pass(self, gen: int, targets: list, force: bool) -> None:
        t0, stopped = self.clock(), None
        if force:  # stays forced for the Sheets this pass doesn't reach (offline, quota, a restart)
            self._drop_hashes([t["sheet_id"] for t in targets])
        for i in range(len(targets)):
            t, targets[i] = targets[i], None
            stopped = self._write(i, t, force)
            if stopped:
                for j in range(i + 1, len(targets)):
                    self._mark(j, "skipped")
                break
        with self._lock:
            complete = all(t["state"] == "done" or (t["state"] == "failed" and t["code"] in LASTING)
                           for t in self.targets)
        self._end_pass(gen, complete, retry=stopped not in STOP_PASS, seconds=round(self.clock() - t0))

    def _write(self, i: int, t: dict, force: bool) -> str | None:
        """Build one Sheet's tables, write them unless the Sheet already shows them, and let them go (a pass
        never holds two Sheets' tables). Returns the problem that stops the pass, if any."""
        sheet_id = t["sheet_id"]
        saved = self.sheet_status(sheet_id) or {}
        asked = int(saved.get("asked") or 0)
        with t["read_tables"]() as tables:
            # read twice only when it may be unchanged: a Sheet that's written anyway is hashed as it's sent
            if not force and saved.get("ok") and saved.get("hash") and not saved.get("allow_shrink"):
                if saved["hash"] == tables_digest(tables):
                    self._mark(i, "done", rows=saved.get("rows"))  # it already shows exactly this: no Google call
                    self._record(sheet_id, True, rows=saved.get("rows"), title=saved.get("title"),
                                 digest=saved["hash"], asked=asked)
                    return None
            if saved.get("hash"):
                self._drop_hashes([sheet_id])  # a write cut off half-way must not count as done
            self._mark(i, "writing")
            try:
                got = write_tables(self.google, sheet_id, tables, t.get("allow_drop", 0), t.get("users"),
                                   allow_shrink=bool(saved.get("allow_shrink")))
            except Exception as e:
                code, message, offline = problem(e)
                if code == "too_much" and t["key"] != "all":
                    message = MESSAGES["too_much_one"]  # pausing others or another Sheet can't help a client's own
                self.log(f"{t['name']}: the Sheet wasn't updated. {message}")
                self._mark(i, "failed", code=code, message=message)
                self._record(sheet_id, False, code, message)
                # quota: Google still said "slow down" after every wait; the timed retry carries on
                return code if offline or code in STOP_PASS or code == "quota" else None
        self._mark(i, "done", rows=got["rows"])
        self._record(sheet_id, True, rows=got["rows"], title=got["title"], digest=got["digest"], asked=asked,
                     tabs=got.get("tabs"))
        return None

    def _end_pass(self, gen: int, complete: bool, retry: bool, seconds: int | None = None) -> None:
        def change(d):
            d.pop("pass_running", None)
            if complete:
                d["written"] = max(int(d.get("written") or 0), gen)
            if seconds is not None:
                d["seconds"] = seconds
        try:
            self._change(change)
        except OSError as e:
            self.log(f"Couldn't save how the Sheets went: {e}")
        with self._lock:
            self.finished_at = utc_stamp(self.clock())
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = self.next_retry_at = None
            if not complete and retry and self.pending():
                self._arm_retry()

    def _arm_retry(self) -> None:
        """The timed retry, RETRY_SECONDS from now (with self._lock held)."""
        self._retry_timer = self.timer(RETRY_SECONDS, self._retry)
        self._retry_timer.daemon = True
        self._retry_timer.start()
        self.next_retry_at = utc_stamp(self.clock() + RETRY_SECONDS)

    def _retry(self) -> None:
        with self._lock:
            if self.running:
                return  # the pass running now sets a new retry itself if it still needs one
            self._retry_timer = self.next_retry_at = None
        started = None
        try:
            if self.pending():
                started = self.start("trying again by itself")
        except Exception as e:
            self.log(f"The Google Sheets update couldn't start: {e}")
        if started not in ("started", "queued"):
            with self._lock:  # no pass after all (e.g. signed out): don't keep saying one is coming
                if not self.running:
                    self.targets = [t for t in self.targets if t.get("message") != AFTER_CRASH]

    def state(self) -> dict:
        with self._lock:
            targets = [dict(t) for t in self.targets]
            return {"running": self.running, "done": sum(t["state"] not in ("waiting", "writing") for t in targets),
                    "total": len(targets), "pending": self.pending(), "targets": targets,
                    "started_at": self.started_at, "finished_at": self.finished_at,
                    "next_retry_at": self.next_retry_at}
