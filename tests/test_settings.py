"""Settings (clients, Sheets, options, backups) and the data-folder paths."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rt import paths, settings  # noqa: E402
from rt.settings import ALL, Settings, SettingsError  # noqa: E402

SHEET = "1Sheet" + "a" * 24
OTHER = "1Other" + "b" * 24


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path, self.backups = self.dir / "settings.json", self.dir / "backups"
        self.s = Settings(self.path, self.backups)

    def fails(self, code, fn, *args, **kwargs) -> SettingsError:
        before = self.s.rev()
        with self.assertRaises(SettingsError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        self.assertEqual(self.s.rev(), before, "a refused change must leave the file as it was")
        return caught.exception

    def add(self, username, name=None, **kwargs) -> dict:
        return self.s.add_client(self.s.rev(), username, name, **kwargs)

    def write_by_hand(self, data) -> None:
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def numbered(self) -> list[int]:
        return sorted(int(m.group(1)) for m in (re.fullmatch(r"settings\.(\d+)\.json", p.name)
                                                for p in self.backups.iterdir()) if m)


class PathsTest(unittest.TestCase):
    def test_data_dir_override_env_and_mode(self):
        with tempfile.TemporaryDirectory() as d:
            chosen, from_env = Path(d) / "chosen", Path(d) / "from env"
            with mock.patch.dict(os.environ, {"RT_DATA_DIR": str(from_env)}):
                self.assertEqual(paths.data_dir(chosen), chosen.resolve())
                self.assertEqual(paths.data_dir(), from_env.resolve())
            self.assertTrue(chosen.is_dir() and from_env.is_dir())
            if os.name == "posix":
                self.assertEqual(chosen.stat().st_mode & 0o777, 0o700)

    def test_default_data_dir_per_system(self):
        home = Path("/home/acme")
        cases = (("darwin", {}, home / "Library" / "Application Support" / "Reddit Tracker"),
                 ("win32", {"APPDATA": "/appdata"}, Path("/appdata") / "Reddit Tracker"),
                 ("linux", {"XDG_DATA_HOME": "/xdg"}, Path("/xdg") / "reddit-tracker"),
                 ("linux", {"XDG_DATA_HOME": "relative"}, home / ".local" / "share" / "reddit-tracker"),
                 ("linux", {}, home / ".local" / "share" / "reddit-tracker"))
        for platform, env, want in cases:
            clean = {k: v for k, v in os.environ.items() if k not in ("APPDATA", "XDG_DATA_HOME")}
            with mock.patch.object(sys, "platform", platform), mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                    mock.patch.object(Path, "home", return_value=home):
                self.assertEqual(paths.default_data_dir(), want, platform)

    def test_find_google_app_file_order(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app, data, env_file = d / "app", d / "data", d / "elsewhere.json"
            app.mkdir()
            data.mkdir()
            with mock.patch.object(paths, "APP_DIR", app), mock.patch.dict(os.environ, {"RT_GOOGLE_APP_FILE": ""}):
                self.assertIsNone(paths.find_google_app_file(data))
                for folder in (data, app):
                    (folder / "google_app.json").write_text("{}", encoding="utf-8")
                    self.assertEqual(paths.find_google_app_file(data), folder / "google_app.json")
                env_file.write_text("{}", encoding="utf-8")
                os.environ["RT_GOOGLE_APP_FILE"] = str(env_file)
                self.assertEqual(paths.find_google_app_file(data), env_file)

    def test_now_utc(self):
        stamp = paths.now_utc()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        self.assertLess(abs((dt.datetime.now(dt.timezone.utc) - when).total_seconds()), 5)


class FileTest(Base):
    def test_missing_file_is_an_empty_list(self):
        self.assertEqual(self.s.rev(), "")
        self.assertEqual(self.s.data(), settings.empty())
        self.assertEqual(self.s.clients(), [])
        self.assertFalse(self.path.exists(), "reading never creates the file")

    def test_saved_layout_modes_and_last_saved_copy(self):
        self.add("Acme_Jane", "Café Acme")
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n")
        self.assertIn("Café Acme", text)
        self.assertEqual(self.numbered(), [], "nothing to back up before the first save")
        self.assertEqual((self.backups / settings.LAST_SAVED).read_text(encoding="utf-8"), text)
        self.add("BoltBikes")
        self.assertEqual((self.backups / "settings.1.json").read_text(encoding="utf-8"), text)
        if os.name == "posix":
            for p in (self.path, self.backups / "settings.1.json", self.backups / settings.LAST_SAVED):
                self.assertEqual(p.stat().st_mode & 0o777, 0o600, p.name)
            self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)

    def test_backups_are_numbered_and_pruned_to_20(self):
        client = self.add("Acme_Jane")
        for i in range(25):
            before = self.path.read_bytes()
            self.s.update_client(self.s.rev(), client["id"], name=f"Acme {i}")
        self.assertEqual(self.numbered(), list(range(6, 26)))
        self.assertEqual((self.backups / "settings.25.json").read_bytes(), before)
        self.assertEqual((self.backups / settings.LAST_SAVED).read_bytes(), self.path.read_bytes())

    def test_failed_write_leaves_the_file_and_no_temp_file(self):
        self.add("Acme_Jane")
        before = self.path.read_bytes()

        def boom(*args):
            raise OSError("disk full")
        with mock.patch.object(os, "replace", boom):
            with self.assertRaises(OSError):
                settings.atomic_write(self.path, b"broken")
            e = self.fails("write_failed", self.add, "BoltBikes")
        self.assertEqual(e.status, 500)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.rglob("*.tmp")], [])

    def test_a_save_windows_blocks_for_a_moment_is_retried(self):
        tried = []

        def replace(src, dst):  # on Windows a status poll reading settings.json blocks the swap for a moment
            tried.append(Path(dst).name)
            if len(tried) == 1:
                raise PermissionError(13, "The process cannot access the file")
            os.replace(src, dst)
        with mock.patch.object(paths, "os", types.SimpleNamespace(name="nt", replace=replace)):
            self.add("Acme_Jane")
        self.assertEqual(tried[:2], ["settings.json", "settings.json"])
        self.assertEqual([c["name"] for c in self.s.clients()], ["Acme Jane"])

    def test_a_bom_from_notepad_or_powershell_reads_fine(self):
        self.add("Acme_Jane")
        self.path.write_bytes(b"\xef\xbb\xbf" + self.path.read_bytes())
        self.assertEqual([c["name"] for c in self.s.clients()], ["Acme Jane"])
        self.add("BoltBikes")
        self.assertEqual([c["name"] for c in self.s.clients()], ["Acme Jane", "BoltBikes"])
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_rev_refuses_an_older_page(self):
        old = self.s.rev()
        self.add("Acme_Jane")
        self.assertNotEqual(self.s.rev(), old)
        self.assertEqual(len(self.s.rev()), 12)
        e = self.fails("stale", self.s.add_client, old, "BoltBikes")
        self.assertEqual(e.status, 409)
        self.fails("stale", self.s.add_client, None, "BoltBikes")

    def test_hand_edit_is_built_on_and_unknown_keys_kept(self):
        self.add("Acme_Jane")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["clients"].append({"id": "c-hand0001", "name": "By Hand", "reddit_username": "by_hand", "note": "keep me"})
        data["extra"] = {"kept": True}
        self.write_by_hand(data)
        self.add("BoltBikes")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([c["name"] for c in saved["clients"]], ["Acme Jane", "By Hand", "BoltBikes"])
        self.assertEqual(saved["clients"][1]["note"], "keep me")
        self.assertIs(saved["clients"][1]["paused"], False)
        self.assertEqual(saved["extra"], {"kept": True})

    def test_damaged_file_is_explained_and_never_overwritten(self):
        self.path.write_text('{\n  "clients": [\n    {"id": "c-1"}\n    {"id": "c-2"}\n  ]\n}\n', encoding="utf-8")
        for fn in (self.s.data, self.s.clients, lambda: self.s.option("access_key"),
                   lambda: self.s.set_sheet(ALL, SHEET, "All"), lambda: self.add("Acme_Jane")):
            e = self.fails("broken_file", fn)
            self.assertEqual(e.status, 409)
            self.assertIn("line 4, column 5: a comma is missing", e.message)
            self.assertIn("no earlier copy", e.message)
            self.assertIsNone(e.backup)
        for bad, words in (({"clients": [{"id": "c-1", "name": "A", "reddit_username": "aaa", "paused": "false"}]},
                            '"paused" should be true or false, without quotes'),
                           ({"clients": [{"id": "c-1", "name": "A", "reddit_username": 1234}]},
                            "client 1 in the list has no id, name or Reddit username"),
                           ([], "isn't laid out like Reddit Tracker's settings")):
            self.write_by_hand(bad)
            self.assertIn(words, self.fails("broken_file", self.s.data).message)
        self.write_by_hand({"clients": [{"id": "c-1", "name": "A", "reddit_username": "aaa", "paused": None}]})
        self.assertIs(self.s.clients()[0]["paused"], False, "null reads as not paused")

    def test_restore_last_saved(self):
        self.add("Acme_Jane")
        self.add("BoltBikes")
        good = self.path.read_bytes()
        self.assertEqual(self.s.restore_last_saved(), {"restored": None, "kept": None}, "a good file is left alone")
        self.path.write_text('{"clients": [}', encoding="utf-8")
        e = self.fails("broken_file", self.s.data)
        self.assertEqual(e.backup, str(self.backups / settings.LAST_SAVED))
        self.assertIn(e.backup, e.message)
        result = self.s.restore_last_saved()
        self.assertEqual(self.path.read_bytes(), good)
        self.assertEqual(Path(result["kept"]).name, "settings.broken.1.json")
        self.assertEqual(Path(result["kept"]).read_text(encoding="utf-8"), '{"clients": [}')
        self.assertEqual([c["name"] for c in self.s.clients()], ["Acme Jane", "BoltBikes"])
        # a damaged last-saved copy: the newest numbered backup that reads fine is used instead
        (self.backups / settings.LAST_SAVED).write_text("oops", encoding="utf-8")
        self.path.write_text("oops", encoding="utf-8")
        result = self.s.restore_last_saved()
        self.assertEqual(Path(result["restored"]).name, "settings.1.json")
        self.assertEqual(Path(result["kept"]).name, "settings.broken.2.json")
        self.assertEqual([c["name"] for c in self.s.clients()], ["Acme Jane"])
        self.assertEqual((self.backups / settings.LAST_SAVED).read_bytes(), self.path.read_bytes())

    def test_restore_without_any_backup_starts_empty_and_keeps_the_file(self):
        self.path.write_text("not json", encoding="utf-8")
        result = self.s.restore_last_saved()
        self.assertIsNone(result["restored"])
        self.assertEqual(Path(result["kept"]).read_text(encoding="utf-8"), "not json")
        self.assertEqual(self.s.data(), settings.empty())

    def test_two_pages_with_the_same_rev_one_wins(self):
        rev, results = self.s.rev(), []
        gate = threading.Barrier(2)

        def attempt(username):
            gate.wait()
            try:
                self.s.add_client(rev, username)
                results.append("ok")
            except SettingsError as e:
                results.append(e.code)
        threads = [threading.Thread(target=attempt, args=(u,)) for u in ("Acme_Jane", "BoltBikes")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["ok", "stale"])
        self.assertEqual(len(self.s.clients()), 1)


class ClientsTest(Base):
    def test_a_username_of_only_dashes_and_underscores_names_the_client_after_it(self):
        self.assertEqual(self.add("___")["name"], "___")
        c = self.add("-_-", "Bolt Bikes")
        self.assertEqual(self.s.update_client(self.s.rev(), c["id"], name="  ")["name"], "-_-")
        self.assertEqual([c["name"] for c in self.s.clients()], ["___", "-_-"], "the file still reads fine")
        with self.assertRaises(SettingsError) as caught:
            Settings._name(settings.empty(), "")
        self.assertEqual((caught.exception.code, caught.exception.field), ("name_missing", "name"))

    def test_add_from_link_or_file_name(self):
        c = self.add("https://www.reddit.com/user/Acme_Jane/performance/?x=1")
        self.assertRegex(c["id"], r"^c-[0-9a-f]{8}$")
        self.assertEqual(c, {"id": c["id"], "name": "Acme Jane", "reddit_username": "Acme_Jane",
                             "sheet_id": None, "sheet_title": None, "paused": False})
        self.assertEqual(self.s.client(c["id"]), c)
        b = self.add("2026-11-02_Bolt-Bikes Post Insights Report.csv")
        self.assertEqual((b["name"], b["reddit_username"]), ("Bolt Bikes", "Bolt-Bikes"))
        self.assertEqual(self.add("u/Cog_Co", "  Cog \t Company ")["name"], "Cog Company")
        self.assertEqual(len({c["id"] for c in self.s.clients()}), 3)

    def test_add_refusals(self):
        self.add("Acme_Jane")
        old = self.add("Old_One")
        self.s.set_paused(self.s.rev(), old["id"], True)
        e = self.fails("bad_username", self.add, "Acme Jane")
        self.assertEqual((e.field, e.status), ("username", 422))
        self.assertIn("Did you mean Acme_Jane", e.message)
        self.fails("bad_username", self.add, "ab")
        e = self.fails("username_taken", self.add, "ACME_JANE")
        self.assertIn("already set up as Acme Jane", e.message)
        e = self.fails("username_taken", self.add, "u/old_one")
        self.assertEqual(e.message, "u/old_one belongs to Old One, which is paused. Resume it instead of adding it again.")
        for reserved in ("all accounts", "ALL CLIENTS", " All  clients "):
            self.assertEqual(self.fails("name_reserved", self.add, "BoltBikes", reserved).field, "name")
        self.fails("name_reserved", self.add, "All_clients")  # the default name would be "All clients"
        self.assertIn("called Acme Jane", self.fails("name_taken", self.add, "BoltBikes", "acme jane").message)
        self.fails("name_too_long", self.add, "BoltBikes", "x" * 61)
        self.fails("bad_request", self.add, "BoltBikes", 12345)
        self.assertEqual(self.add("BoltBikes", "x" * 60)["name"], "x" * 60)

    def test_username_takes_the_database_spelling(self):
        stored = {"acme_jane": "Acme_Jane"}
        c = self.add("acme_jane", canonical=lambda u: stored.get(u.lower(), u))
        self.assertEqual((c["reddit_username"], c["name"]), ("Acme_Jane", "Acme Jane"))

    def test_update_client(self):
        c = self.add("Acme_Jane")
        rival = self.add("BoltBikes")
        rev = self.s.rev()
        self.assertEqual(self.s.update_client(rev, c["id"]), c)
        self.assertEqual(self.s.rev(), rev, "nothing changed, nothing written")
        renamed = self.s.update_client(rev, c["id"], name="Acme J")
        self.assertEqual((renamed["id"], renamed["name"]), (c["id"], "Acme J"))
        self.assertEqual(self.s.update_client(self.s.rev(), c["id"], name="acme j")["name"], "acme j")
        self.fails("name_taken", self.s.update_client, self.s.rev(), c["id"], name="BOLTBIKES")
        self.fails("username_taken", self.s.update_client, self.s.rev(), c["id"], username_text="boltbikes",
                   items_count=0)
        self.fails("bad_request", self.s.update_client, self.s.rev(), c["id"], username_text="Acme_Jane2")
        e = self.fails("username_locked", self.s.update_client, self.s.rev(), c["id"], username_text="Acme_Jane2",
                       items_count=3)
        self.assertEqual(e.field, "username")
        self.assertIn("u/Acme_Jane already has saved numbers", e.message)
        # a case-only change isn't a change: no count needed, and the database spelling wins
        same = self.s.update_client(self.s.rev(), c["id"], username_text="ACME_JANE",
                                    canonical=lambda u: "Acme_Jane")
        self.assertEqual(same["reddit_username"], "Acme_Jane")
        moved = self.s.update_client(self.s.rev(), c["id"], username_text="u/Acme_Jane2", name="", items_count=0)
        self.assertEqual((moved["reddit_username"], moved["name"]), ("Acme_Jane2", "Acme Jane2"))
        self.assertEqual(self.s.client(rival["id"]), rival)
        e = self.fails("no_such_client", self.s.update_client, self.s.rev(), "c-00000000", name="X")
        self.assertEqual(e.status, 404)

    def test_a_username_shared_by_mistake_unlocks_both(self):
        self.write_by_hand({"version": 1, "clients": [
            {"id": "c-aaaaaaaa", "name": "Acme Jane", "reddit_username": "Acme_Jane"},
            {"id": "c-bbbbbbbb", "name": "Dup Hand", "reddit_username": "acme_jane"}]})
        self.s.update_client(self.s.rev(), "c-aaaaaaaa", name="Acme J")  # the old mix-up never blocks other edits
        self.s.update_client(self.s.rev(), "c-bbbbbbbb", username_text="dup_hand", items_count=7)
        self.fails("username_locked", self.s.update_client, self.s.rev(), "c-aaaaaaaa", username_text="other_one",
                   items_count=7)

    def test_pause_resume_and_remove(self):
        a = self.add("Acme_Jane")
        b = self.add("BoltBikes")
        self.assertIs(self.s.set_paused(self.s.rev(), a["id"], True)["paused"], True)
        self.assertEqual([c["id"] for c in self.s.clients(active_only=True)], [b["id"]])
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["clients"][0]["paused"], True)
        rev = self.s.rev()
        self.s.set_paused(rev, a["id"], True)
        self.assertEqual(self.s.rev(), rev)
        self.fails("bad_request", self.s.set_paused, rev, a["id"], "false")
        self.assertIs(self.s.set_paused(rev, a["id"], False)["paused"], False)
        self.fails("stale", self.s.remove_client, rev, b["id"])
        self.s.remove_client(self.s.rev(), b["id"])
        self.assertEqual([c["id"] for c in self.s.clients()], [a["id"]])
        self.fails("no_such_client", self.s.remove_client, self.s.rev(), b["id"])
        self.assertEqual(self.add("BoltBikes")["name"], "BoltBikes", "a removed client can be added again")


class SheetsTest(Base):
    def test_one_sheet_never_serves_two_targets(self):
        a = self.add("Acme_Jane")
        b = self.add("BoltBikes")
        page_rev = self.s.rev()
        self.s.set_sheet(a["id"], SHEET, "Reddit Tracker – Acme Jane")
        self.assertEqual(self.s.client(a["id"])["sheet_id"], SHEET)
        self.s.set_sheet(a["id"], SHEET, "Renamed")  # the same Sheet again just refreshes its title
        self.assertEqual(self.s.client(a["id"])["sheet_title"], "Renamed")
        self.assertIn("Acme Jane already uses this Sheet", self.fails("sheet_in_use", self.s.set_sheet,
                                                                      b["id"], SHEET, "x").message)
        self.assertIn("All clients Sheet needs a Sheet of its own",
                      self.fails("sheet_in_use", self.s.set_sheet, ALL, SHEET, "x").message)
        self.s.set_sheet(ALL, OTHER, "Reddit Tracker – All clients")
        self.assertEqual(self.s.data()["all_sheet"], {"id": OTHER, "title": "Reddit Tracker – All clients"})
        self.assertIn("already your All clients Sheet",
                      self.fails("sheet_in_use", self.s.set_sheet, b["id"], OTHER, "x").message)
        self.s.set_paused(self.s.rev(), a["id"], True)
        self.fails("sheet_in_use", self.s.set_sheet, b["id"], SHEET, "x")  # paused clients keep their Sheet
        self.fails("no_such_client", self.s.set_sheet, "c-00000000", "1Free" + "c" * 25, "x")
        self.fails("bad_request", self.s.set_sheet, b["id"], "  ", "x")
        self.s.clear_sheet(ALL)
        self.s.clear_sheet(a["id"])
        self.assertIsNone(self.s.data()["all_sheet"])
        self.assertEqual((self.s.client(a["id"])["sheet_id"], self.s.client(a["id"])["sheet_title"]), (None, None))
        self.s.set_sheet(b["id"], OTHER, "Bolt")
        self.assertNotEqual(self.s.rev(), page_rev, "Sheet changes need no page rev but still change it")


class OptionsTest(Base):
    def test_options_and_access_key(self):
        self.assertIsNone(self.s.option("access_key"))
        self.assertEqual(self.s.option("missing", 5), 5)
        key = self.s.new_access_key()
        self.assertRegex(key, r"^[A-Za-z0-9_-]{12}$")
        self.assertEqual(self.s.option("access_key"), key)
        self.assertNotEqual(self.s.new_access_key(), key)
        self.s.set_option("color", "blue")
        self.assertEqual(self.s.data()["options"]["color"], "blue")


if __name__ == "__main__":
    unittest.main()
