"""Preview and import of dropped Reddit Pro files: whose file each one is, whether it is safe to
store, and what it adds. One engine for both: the preview runs it on an in-memory copy of the
database, so it shows exactly what Import will do.

Clients are settings dicts {"id", "name", "reddit_username", "paused", "sheet_id"}, paused ones
included; files are {"id", "name", "text", "modified" (ms or None), "client", "expect"}.
"""
from __future__ import annotations

import datetime as dt
import errno
import hashlib
import re
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from . import csvfiles, store
from .csvfiles import COMMENT_LIMIT, KINDS, NOUNS, REPORT_TITLES, USERNAME

NEEDS_YOU = ("needs_client", "name_mismatch", "doesnt_match", "new_client", "paused", "cant_use", "changed", "failed")
NOT_NEEDED = ("already", "older_copy", "same_file")
CANT_USE = {
    "excel": "This file was opened and saved in Excel or Numbers, which changed its dates or numbers. Download "
             "it again from Reddit Pro and drop the new file without opening it.",
    "missing_column": "This file is missing some of Reddit's columns ({detail}). If nobody edited it, Reddit may "
                      "have changed its report.",
    "not_report": "Not a Reddit Pro report, so it was left out.",
    "not_csv": "This isn't a plain CSV file (it may be a spreadsheet). Download the CSV again from Reddit Pro.",
    "bad_row": "Part of this file is damaged ({detail}). Download it again from Reddit Pro.",
}
UNWRITABLE = "Reddit Tracker can't save to its data folder (the disk is full or can't be written to)"
# read-only SD card, disk full, no permission (EDQUOT: over a disk quota, not on every system)
UNWRITABLE_ERRNOS = {errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM, getattr(errno, "EDQUOT", errno.ENOSPC)}
UNWRITABLE_SQLITE = ("disk is full", "readonly database", "disk i/o error", "unable to open database")


def abs_day(day: str | None) -> str:
    """'Thu 8 Oct' for a YYYY-MM-DD day."""
    if not day:
        return "never"
    d = dt.date.fromisoformat(day)
    return f"{d:%a} {d.day} {d:%b}"


def day_label(day: str | None, today: dt.date) -> str:
    """'today', 'yesterday' or 'Thu 8 Oct'."""
    if day and dt.date.fromisoformat(day) == today:
        return "today"
    if day and dt.date.fromisoformat(day) == today - dt.timedelta(days=1):
        return "yesterday"
    return abs_day(day)


def plural(n: int, kind: str) -> str:
    """'1 post', '3 comments', '2 days'."""
    return f"{n} {NOUNS[kind][0 if n == 1 else 1]}"


def name_route(filename: str, clients: list[dict]) -> tuple[str, object]:
    """What a file's name says: ("client", c), ("paused", c), ("label_user", c), ("new_client", username)
    or ("unknown", None). A username in Reddit's own file name decides alone, so "Acme_Jane2" is never
    Acme_Jane's. One that is also a one-word client name may be either ("label_user"). A renamed file
    is matched by a client name, or a whole username or client name inside it, longest first."""
    active = [c for c in clients if not c.get("paused")]
    paused = [c for c in clients if c.get("paused")]
    users = {c["reddit_username"].lower(): c for c in active if c.get("reddit_username")}
    labels = {c["name"].casefold(): c for c in active}
    paused_users = {c["reddit_username"].lower(): c for c in paused if c.get("reddit_username")}
    paused_labels = {c["name"].casefold(): c for c in paused}
    named = csvfiles.account_from_filename(filename)
    if named and USERNAME.fullmatch(named):
        if named.lower() in users:
            return "client", users[named.lower()]
        if named.lower() in paused_users:
            return "paused", paused_users[named.lower()]
        if named.casefold() in labels:
            return "label_user", labels[named.casefold()]
        if named.casefold() not in paused_labels:
            return "new_client", named
    if named and named.casefold() in labels:
        return "client", labels[named.casefold()]
    if named and named.casefold() in paused_labels:
        return "paused", paused_labels[named.casefold()]
    low = filename.lower()
    whole = [(u, "client", c) for u, c in users.items()] + [(u, "paused", c) for u, c in paused_users.items()]
    for u, how, c in sorted(whole, key=lambda x: len(x[0]), reverse=True):
        if re.search(rf"(?<![a-z0-9_-]){re.escape(u)}(?![a-z0-9_-])", low):
            return how, c
    text = (named or Path(filename).stem).casefold()  # without the report's title: "Copy of Acme Jane"
    names = [(n, "client", c) for n, c in labels.items()] + [(n, "paused", c) for n, c in paused_labels.items()]
    for n, how, c in sorted(names, key=lambda x: len(x[0]), reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text):
            return how, c
    return "unknown", None


def _fold(username: str) -> str:
    return username.lower().replace("-", "_")


def near_username(username: str, clients: list[dict]) -> dict | None:
    """The one client whose username differs from this one only by - versus _ (an easy slip when typing it):
    a different account to Reddit, so it's only ever offered, never assumed."""
    found = [c for c in clients if c.get("reddit_username") and c["reddit_username"].lower() != username.lower()
             and _fold(c["reddit_username"]) == _fold(username)]
    return found[0] if len(found) == 1 else None


def file_day(modified: int | None, today: dt.date) -> str:
    """The day a file was downloaded (the browser's modified time): an empty report has no dates."""
    if modified and modified > 1e12:
        try:
            return dt.datetime.fromtimestamp(modified / 1000).date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return today.isoformat()


def summarize(kind: str, rows: list[dict], before: str | None, today: dt.date) -> tuple[str, list[dict]]:
    """(one line about what the file holds, warnings). `before`: the newest day stored before it."""
    warns: list[dict] = []
    if not rows:
        return {"post": "No posts in Reddit Pro yet. Nothing to add, and that's fine.",
                "comment": "No comments in Reddit Pro yet. Nothing to add, and that's fine.",
                "account": "No days in this Account file."}[kind], warns
    days = sorted(r["Date"] for r in rows)
    if kind == "account":
        if before and dt.date.fromisoformat(days[0]) > dt.date.fromisoformat(before) + dt.timedelta(days=1):
            gap = (dt.date.fromisoformat(days[0]) - dt.date.fromisoformat(before)).days - 1
            warns.append({"text": f"This Account file starts {day_label(days[0], today)}, but the saved numbers end "
                                  f"{day_label(before, today)}, so {plural(gap, kind)} "
                                  f"{'is' if gap == 1 else 'are'} missing. Choose Past year and download it again.",
                          "level": "redo"})
        return f"{plural(len(rows), kind)}, up to {day_label(days[-1], today)}", warns
    posted = [r[f"{kind.title()} Published Date"] for r in rows]
    if len(rows) >= COMMENT_LIMIT and sum(b > a for a, b in zip(posted, posted[1:])) > 5:
        warns.append({"text": f"This file looks sorted by Top, so new {NOUNS[kind][1]} may be missing. In Reddit Pro, "
                              "set the sort to New and download it again.", "level": "redo"})
    ordered = sorted(posted)
    if kind == "post":
        return f"{plural(len(rows), kind)}, exported {day_label(days[-1], today)}", warns
    if len(rows) >= COMMENT_LIMIT:
        reach = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(ordered[0])).days
        if before and ordered[0] > before:
            warns.append({"text": f"Comments posted between {day_label(before, today)} and "
                                  f"{day_label(ordered[0], today)} weren't in any upload, so they're missing. "
                                  "Upload more often.", "level": "note"})
        if reach < 30:
            back = plural(reach, "account") if reach else "less than a day"  # NOUNS["account"]: day, days
            warns.append({"text": f"Reddit's file only reaches back {back}, so these comments stop updating "
                                  "after that and get no 30-day views.", "level": "note"})
    return f"{plural(len(rows), kind)}, posted {day_label(ordered[0], today)} – {day_label(ordered[-1], today)}", warns


def unwritable(e: BaseException) -> bool:
    """The data folder can't be written: a full disk, a read-only SD card, no permission."""
    return (isinstance(e, OSError) and e.errno in UNWRITABLE_ERRNOS) or (
        isinstance(e, sqlite3.Error) and any(w in str(e).lower() for w in UNWRITABLE_SQLITE))


def plain_error(e: Exception) -> str:
    """Why saving failed, in a few plain words (no full stop: it goes inside a sentence)."""
    if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
        return "Reddit Tracker's database is busy"
    if unwritable(e):
        return UNWRITABLE
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e)[:200] or type(e).__name__


def keep_copy(text: str, username: str, kind: str, uploads_dir: Path) -> str | None:
    """Keep the exact file, named so that dropping it again files it the same way. Returns a
    warning if that failed (the import is saved either way)."""
    try:
        folder = Path(uploads_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{re.sub(r'[^A-Za-z0-9_-]', '_', username) or 'file'} {REPORT_TITLES[kind]}"
        (folder / f"{stem} {dt.datetime.now():%Y-%m-%d_%H%M%S_%f}.csv").write_bytes(text.encode("utf-8"))
        return None
    except OSError as e:
        return f"Saved, but no copy of the file could be kept: {plain_error(e)}."


def _blank(f: dict) -> dict:
    return {"id": f["id"], "name": f["name"], "status": None, "import": False, "client": None, "client_name": None,
            "username": None, "report": None, "export_day": None, "rows": None, "summary": None, "message": None,
            "warnings": [], "evidence": None, "guess": None, "new_username": None, "near_client": None, "paused_client": None,
            "against": None}


def _guess(c: dict, matched: int, rows: list) -> dict:
    return {"client": c["id"], "name": c["name"], "matched": matched, "of": len(rows)}


def plan_files(con: sqlite3.Connection, clients: list[dict], files: list[dict], real: bool, today: dt.date,
               uploads_dir: Path | None = None, on_saved: Callable[[], None] | None = None) -> dict:
    """Check, match and import the files on con, in one pass. real=False on a copy is the preview;
    real=True imports only the files whose `expect` is still the client this finds. A file that
    fails to save stops the batch there; the files before it stay saved. on_saved() is called once,
    right after the first file that changed something was saved."""
    active = [c for c in clients if not c.get("paused")]
    by_id = {c["id"]: c for c in active}
    owned = {c["id"]: {c["reddit_username"].lower()} if c.get("reddit_username") else set() for c in active}
    configured = [c.get("reddit_username") or "" for c in clients]
    out = {f["id"]: _blank(f) for f in files}
    seen: dict[str, str] = {}
    candidates = []
    for f in files:
        r = out[f["id"]]
        try:
            kind, rows = csvfiles.read_report_text(f["text"])
        except csvfiles.ReportError as e:
            r.update(status="cant_use", message=CANT_USE[e.code].format(detail=e.detail))
            continue
        r.update(report=kind, rows=len(rows),
                 export_day=max((x["Date"] for x in rows), default=None) or file_day(f.get("modified"), today))
        if kind == "account" and not rows:
            r.update(status="cant_use", message="This Account file has no days in it. Download it again, with Past "
                                                "year chosen.")
            continue
        how, who = name_route(f["name"], clients)
        if how == "paused":  # never stored for anyone else, whatever was chosen
            r.update(status="paused", paused_client=who["id"],
                     message=f"This file is for {who['name']}, which is paused, so it won't be saved.")
            continue
        if how == "new_client":  # another Reddit account's file: never stored under an existing client
            r.update(status="new_client", new_username=who, message=f"u/{who} isn't one of your clients yet.")
            near = near_username(who, active)
            if near:  # e.g. saved as Acme-Jane, but Reddit's file says Acme_Jane
                r.update(near_client=near["id"],
                         message=f"{near['name']} is saved as u/{near['reddit_username']}, but Reddit's file says "
                                 f"u/{who}. If it's the same account, fix the username.")
            continue
        digest = hashlib.sha1(f["text"].encode("utf-8")).hexdigest()
        if rows and digest in seen:  # the same download twice ("… (1).csv"): only one is used. Empty
            # downloads are alike for every account: two clients' are two files (same client: an older copy)
            r.update(status="same_file", message=f"The same file as “{seen[digest]}”.")
            continue
        noun = NOUNS[kind][1]
        counts = store.match_counts(con, kind, rows)
        matched = {cid: sum(n for u, n in counts.items() if u in own) for cid, own in owned.items()}
        best = max(matched, key=matched.get, default=None)
        total = sum(matched.values())
        enough = min(5, max(len(rows), 1))  # a small file's rows can all be a client's
        choice = by_id.get(f.get("client"))
        target = choice or (who if how == "client" else None)
        if target:
            if best and best != target["id"] and matched[best] >= 5 and matched[best] > matched[target["id"]]:
                said = f"You chose {target['name']}" if choice else f"The name says {target['name']}"
                r.update(status="name_mismatch", guess=_guess(by_id[best], matched[best], rows),
                         message=f"{said}, but these {noun} are {by_id[best]['name']}'s "
                                 f"({matched[best]} of {len(rows)}).")
                continue
        elif best and matched[best] >= enough and matched[best] >= 0.9 * total:
            target = by_id[best]
            r["evidence"] = f"Matched by contents: {matched[best]} of {len(rows)} {noun} are already {target['name']}'s."
        else:
            r.update(status="needs_client",
                     message=f"The name says {who['name']}, but that's also the Reddit username u/{who['name']}, so "
                             f"it may be another account's file. Choose {who['name']} if it's theirs."
                     if how == "label_user" else "We can't tell whose file this is from its name.",
                     guess=_guess(by_id[best], matched[best], rows)
                     if best and matched[best] >= enough and matched[best] > total / 2 else None)
            continue
        mine = {"client": target["id"], "client_name": target["name"]}
        if not owned[target["id"]]:
            r.update(status="cant_use", **mine, message=f"{target['name']} has no Reddit username yet, so its "
                                                        "files can't be stored. Add the username with Edit.")
            continue
        username = store.canonical_user(con, target["reddit_username"], configured)
        if kind == "account":  # never let another account's year of numbers replace this client's
            agreement = store.account_agreement(con, rows, {username.lower()}).values()
            agree, both = sum(a for a, _ in agreement), sum(b for _, b in agreement)
            if both >= 14 and agree < both / 2:
                r.update(status="doesnt_match", against=target["id"],
                         message=f"These Account numbers don't match the ones saved for {target['name']} (only "
                                 f"{agree} of {both} days agree), so they're probably another Reddit account's. "
                                 "Check which account you downloaded it from, or choose the right client.")
                continue
        have = 0 if rows else store.stored_count(con, kind, owned[target["id"]])
        if have:  # an empty download of a report that has rows: Reddit Pro didn't finish it
            r.update(status="cant_use", **mine, message=f"This file is empty, but {plural(have, kind)} are saved "
                                                        f"for {target['name']}, so Reddit Pro probably didn't "
                                                        "finish the download. Download it again.")
            continue
        r.update(mine, username=username)
        seen[digest] = f["name"]
        candidates.append((f, r, kind, rows))

    # The same client's report for the same day more than once: the newest is used (one with rows
    # always beats an empty one; a tie goes to the later file). An Account file with days the newest
    # lacks (Past year, then Past 30 days) is imported too, before it: days merge, the newer numbers win.
    newest: dict[tuple, tuple] = {}
    for f, r, kind, rows in candidates:
        key, rank = (r["client"], kind, r["export_day"]), (bool(rows), f.get("modified") or 0)
        if key not in newest or rank >= newest[key][0]:
            newest[key] = (rank, f, rows)
    for f, r, kind, rows in candidates:
        _, kept, kept_rows = newest[(r["client"], kind, r["export_day"])]
        if kept is f:
            continue
        if kind == "account" and {x["Date"] for x in rows} - {x["Date"] for x in kept_rows}:
            r["fill"] = True  # only the days the newest file lacks, so dropping both again changes nothing
            continue
        r.update(status="older_copy", message=f"An older copy of the same day's report. “{kept['name']}” is used.")

    todo = [c for c in candidates if c[1]["status"] is None]
    todo.sort(key=lambda c: (c[1]["export_day"] or "", c[0].get("modified") or 0))
    before_batch = {cid: store.snapshot_counts(con, own) for cid, own in owned.items()} if real else {}
    keys: dict[tuple, set] = {}
    stopped = notified = False
    for f, r, kind, rows in todo:
        fill = r.pop("fill", False)
        if stopped:
            r.update(status="not_reached", message="Not imported yet: the import stopped at an earlier file. Press "
                                                   "Import again.")
            continue
        if real and f.get("expect") != r["client"]:
            r.update(status="changed", message="The client list changed after this file was checked. It was "
                                               "checked again: look at it, then press Import again.")
            continue
        own = owned[r["client"]] | {r["username"].lower()}
        day = r["export_day"]
        saved, stored = False, None
        try:
            stored = store.newest_stored(con, kind, own)
            before = store.fingerprint(con, kind, own, day)
            recorded = store.upload_recorded(con, kind, own, day)
            store.record_upload(con, r["username"], kind, day, len(rows))  # committed with the rows
            store.import_rows(con, r["username"], kind, rows, fill_only=kind == "account" and (fill or bool(stored)
                              and day < stored), links=real)
            saved = True
            changed = store.fingerprint(con, kind, own, day) != before or (not rows and not recorded)
        except Exception as e:
            if not real:
                raise
            con.rollback()
            stopped = True  # the database is in trouble: the files after this one wait for the next Import
            if not saved:
                r.update(status="failed", message=f"Couldn't be saved: {plain_error(e)}. The files before it were "
                                                  "saved. Press Import again for the rest.")
                continue
            changed = True  # saved; only looking at what it added failed
            r["warnings"].append({"text": f"Saved, but Reddit Tracker couldn't check what it added: "
                                          f"{plain_error(e)}.", "level": "note"})
        r["summary"], found = summarize(kind, rows, stored, today)
        r["warnings"] = found + r["warnings"]
        if not changed:
            r.update(status="already", message="Already imported. Nothing new in it.")
        elif stored and day < stored:
            r.update(status="fills_gap", message="An older download that was never uploaded. It fills a gap in the "
                                                 "history.")
        else:
            r.update(status="ready")
        r["import"] = r["status"] in ("ready", "fills_gap")
        if real and r["import"]:
            if on_saved and not notified:  # saved: from now on the Sheets need this, even after a restart
                on_saved()
                notified = True
            keys.setdefault((r["client"], kind), set()).update(store.row_keys(kind, rows))
            problem = keep_copy(f["text"], r["username"], kind, uploads_dir) if uploads_dir else None
            if problem:
                r["warnings"].append({"text": problem, "level": "note"})

    slots = {c["id"]: {"post": None, "comment": None, "account": None, "older": []} for c in active}
    modified = {f["id"]: f.get("modified") or 0 for f in files}  # in import order: the newest has the slot
    for r in sorted((r for r in out.values() if r["import"]),
                    key=lambda r: (r["export_day"] or "", modified[r["id"]])):
        slot = slots[r["client"]]
        if slot[r["report"]]:
            slot["older"].append(slot[r["report"]])
        slot[r["report"]] = r["id"]
    imported = [r for r in out.values() if r["import"]]
    result = {"files": list(out.values()),
              "counts": {"ready": len(imported),
                         "needs_you": sum(r["status"] in NEEDS_YOU for r in out.values()),
                         "not_needed": sum(r["status"] in NOT_NEEDED for r in out.values())},
              "slots": slots,
              "no_files": [c["id"] for c in active if not any(slots[c["id"]][k] for k in KINDS)]}
    if real:
        result["imported"] = len(imported)
        result["receipt"] = receipt(con, active, owned, before_batch, imported, keys)
    return result


def receipt(con, active: list[dict], owned: dict, before: dict, imported: list[dict], keys: dict) -> list[dict]:
    """Per client, what the import stored: '7 posts', '100 comments (14 new)', 'Account numbers up to …'
    (each post or comment counted once, however many of the client's files list it)."""
    out = []
    for c in active:
        mine = [r for r in imported if r["client"] == c["id"]]
        if not mine:
            continue
        try:
            after = store.snapshot_counts(con, owned[c["id"]])
        except sqlite3.Error:  # the database is busy: what was saved, without the new counts
            after = None
        lines = []
        for kind in ("post", "comment"):
            if any(r["report"] == kind for r in mine):
                n = len(keys.get((c["id"], kind), ()))
                new = after[kind] - before[c["id"]][kind] if after else 0
                lines.append(plural(n, kind) + (f" ({new} new)" if new else "") if n else f"no {NOUNS[kind][1]} yet")
        if any(r["report"] == "account" for r in mine):
            lines.append(f"Account numbers up to {abs_day(after['account_last'])}" if after else "Account numbers saved")
        out.append({"client": c["id"], "name": c["name"], "lines": lines})
    return out


def preview(con: sqlite3.Connection, clients: list[dict], files: list[dict], today: dt.date, lock=None) -> dict:
    """What Import would do, worked out on an in-memory copy (nothing is saved). `lock`, if given,
    is held only while the copy is taken."""
    with lock or nullcontext():
        mem = store.memory_copy(con)
    try:
        return plan_files(mem, clients, files, False, today)
    finally:
        mem.close()


def run_import(con: sqlite3.Connection, clients: list[dict], files: list[dict], today: dt.date, backups_dir: Path,
               uploads_dir: Path, on_saved: Callable[[], None] | None = None) -> dict:
    """Import the files the page showed as ready (each with `expect`), keeping a copy of the database
    first and a copy of each imported file. The plan also has "imported" and "receipt"."""
    if any(f.get("expect") for f in files):
        store.backup_db(con, backups_dir)
    return plan_files(con, clients, files, True, today, uploads_dir, on_saved)
