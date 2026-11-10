"""Checks over the app's own code that keep it running on every Python from 3.9 and on Windows:
explicit text encodings, standard library only, no modules that newer Pythons removed, code that
compiles, and start files that work (Windows line endings; plain advice when Python is too old)."""
from __future__ import annotations

import ast
import http.server
import json
import os
import py_compile
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOVED = {"cgi", "imp", "distutils", "asyncore", "asynchat", "smtpd", "telnetlib", "pipes", "crypt", "nntplib"}
MODE_CHARS = set("rwxabt+U")
# Standard-library modules the app may import (sys.stdlib_module_names is 3.10+). Add one here when the app
# needs it, but never one that Debian / Raspberry Pi OS ships separately (tkinter, venv, ensurepip, lib2to3).
STDLIB = set("""__future__ argparse base64 binascii bisect calendar codecs collections contextlib copy csv ctypes
dataclasses datetime email enum errno fnmatch functools glob gzip hashlib heapq hmac html http io ipaddress
itertools json locale logging math mimetypes operator os pathlib platform queue random re secrets select
selectors shlex shutil signal socket socketserver sqlite3 ssl stat string struct subprocess sys tempfile
textwrap threading time traceback typing unicodedata urllib uuid warnings weakref webbrowser zipfile zlib""".split())
BASH = shutil.which("bash") if os.name == "posix" else None
SCRIPTS = ("start-mac.command", "start-linux.sh", "install-raspberry-pi.sh", "make-release.sh")


def app_files() -> list[Path]:
    files = sorted((ROOT / "rt").glob("*.py"))
    entry = ROOT / "reddit_tracker.py"
    return files + [entry] if entry.exists() else files


def _imports(tree) -> list[tuple[int, str]]:
    """(line, top-level module) for every absolute import."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            found.append((node.lineno, (node.module or "").split(".")[0]))
    return found


def _const(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _missing_encoding(call: ast.Call) -> str | None:
    """How a call opens a text file without encoding= ("open(..., 'w')"), or None if it doesn't."""
    keywords = {k.arg for k in call.keywords}
    if None in keywords or "encoding" in keywords:  # **kwargs can't be checked
        return None
    func, args = call.func, call.args
    if isinstance(func, ast.Name):
        name, owner = func.id, None
    elif isinstance(func, ast.Attribute):
        name, owner = func.attr, (func.value.id if isinstance(func.value, ast.Name) else "")
    else:
        return None
    if name == "read_text":
        return None if args else "read_text()"
    if name == "write_text":
        return None if len(args) > 1 else "write_text()"
    if name == "open" and owner in ("os", "webbrowser"):
        return None
    if name == "open" and owner not in (None, "io"):
        mode_at = 0  # Path.open(mode)
    elif name in ("open", "fdopen"):
        mode_at = 1  # open(file, mode), io.open(file, mode), os.fdopen(fd, mode)
    else:
        return None
    node = next((k.value for k in call.keywords if k.arg == "mode"), args[mode_at] if len(args) > mode_at else None)
    if node is None:
        return f"{name}()"
    mode = _const(node)
    if mode is None or not set(mode) <= MODE_CHARS or "b" in mode:
        return None  # binary, or not a file mode at all (a URL, a request object, a variable)
    return f"{name}(..., {mode!r})"


class LintTest(unittest.TestCase):
    def test_text_files_name_their_encoding(self):
        problems = []
        for path in app_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    what = _missing_encoding(node)
                    if what:
                        problems.append(f"{path.name}:{node.lineno}: {what} without encoding=")
        self.assertEqual(problems, [])

    def test_no_removed_modules_or_old_habits(self):
        problems = []
        for path in app_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, str(path))
            problems += [f"{path.name}:{line}: imports {n}" for line, n in _imports(tree) if n in REMOVED]
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                    problems.append(f"{path.name}:{node.lineno}: utcnow() (use datetime.now(timezone.utc))")
            # reddit_tracker.py may do without it when it has no annotations: then its "needs Python 3.9"
            # message can show even on a Python too old to know the future import
            annotated = any(isinstance(n, ast.AnnAssign) or getattr(n, "returns", None) is not None
                            or isinstance(n, ast.arg) and n.annotation is not None for n in ast.walk(tree))
            needed = path.parent.name == "rt" or annotated
            if source.strip() and needed and "from __future__ import annotations" not in source:
                problems.append(f"{path.name}: no 'from __future__ import annotations'")
        self.assertEqual(problems, [])

    def test_standard_library_only(self):
        problems = [f"{path.name}:{line}: imports {name}, which isn't in the standard-library list"
                    for path in app_files() for line, name in _imports(ast.parse(path.read_text(encoding="utf-8")))
                    if name not in STDLIB | {"rt"}]
        self.assertEqual(problems, [])
        self.assertFalse(STDLIB & REMOVED)
        if hasattr(sys, "stdlib_module_names"):  # Python 3.10+ can check the list itself
            self.assertEqual(STDLIB - set(sys.stdlib_module_names), set())

    def test_windows_start_file_has_windows_line_endings(self):
        raw = (ROOT / "start-windows.bat").read_bytes()
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "every line ends with CRLF")
        self.assertEqual(raw.count(b"\r"), raw.count(b"\r\n"))

    def test_everything_compiles(self):
        self.assertTrue(app_files())
        with tempfile.TemporaryDirectory() as out:
            for i, path in enumerate(app_files()):
                py_compile.compile(str(path), cfile=str(Path(out) / f"{i}.pyc"), doraise=True)

    def test_the_lint_itself(self):
        cases = {"open(p)": "open()", "open(p, 'w')": "open(..., 'w')", "open(p, 'rb')": None,
                 "open(p, encoding='utf-8')": None, "p.read_text()": "read_text()", "p.read_text('utf-8')": None,
                 "p.write_text(s)": "write_text()", "p.write_text(s, encoding='utf-8')": None,
                 "p.open('a')": "open(..., 'a')", "p.open('rb')": None, "os.fdopen(fd, 'w')": "fdopen(..., 'w')",
                 "os.fdopen(fd, 'wb')": None, "os.open(p, os.O_RDONLY)": None, "webbrowser.open(url)": None,
                 "opener.open(request, timeout=60)": None, "open(p, mode)": None, "open(p, **kw)": None}
        for code, want in cases.items():
            self.assertEqual(_missing_encoding(ast.parse(code).body[0].value), want, code)


class _Ping(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(self.server.answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(BASH, "needs bash")
class ScriptsTest(unittest.TestCase):
    """The start files and the Raspberry Pi installer, run with fake python3, sudo and systemctl."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.fake("sudo", 'echo "$*" >> "$FAKE_SUDO"; [ "$1" != tee ] || cat > "$FAKE_UNIT"')
        self.fake("systemctl", 'case "$*" in *ActiveState*) echo "$FAKE_STATE";; '
                               '*ExecMainStatus*) echo "$FAKE_STATUS";; esac')
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp), "FAKE_SUDO": str(self.tmp / "sudo.log"),
                    "FAKE_UNIT": str(self.tmp / "unit"), "FAKE_STATE": "active", "FAKE_STATUS": "0", "FAKE_LINK": ""}

    def fake(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def fake_python(self, version: tuple | None) -> Path:
        """A python3 whose version checks see `version` (None: macOS's stand-in, which prints nothing and fails).
        It runs nothing else, only says what it would run."""
        if version is None:
            return self.fake("python3", "exit 1")
        real = shlex.quote(sys.executable)
        return self.fake("python3", f'[ "$1" != -c ] || exec {real} -c "import sys; sys.version_info = {version!r}\n'
                                    '$2"\necho "would run: $*"')

    def run_script(self, script: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(script), *args], env={**self.env, **env}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def test_scripts_parse(self):
        for name in SCRIPTS:
            run = subprocess.run([BASH, "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, f"{name}: {run.stderr}")

    def test_start_files_give_advice_that_works_when_python_is_too_old(self):
        for name, old, words in (("start-linux.sh", (3, 8, 10), "Upgrade to a newer version of your Linux"),
                                 ("start-mac.command", (3, 8, 10), "python.org/downloads")):
            self.fake_python(old)
            run = self.run_script(ROOT / name)
            self.assertEqual(run.returncode, 1, name)
            self.assertIn("it has Python 3.8.10", run.stdout, name)
            self.assertIn(words, run.stdout, name)
            self.assertNotIn("apt install", run.stdout, name)
            self.fake_python((3, 9, 0))
            run = self.run_script(ROOT / name, "--no-browser")
            self.assertEqual((run.returncode, run.stdout.strip()), (0, "would run: reddit_tracker.py --no-browser"))
        self.fake_python(None)
        self.assertIn("Click Install in the box macOS shows", self.run_script(ROOT / "start-mac.command").stdout)
        no_python = self.tmp / "no-python"
        no_python.mkdir()
        os.symlink(shutil.which("dirname"), no_python / "dirname")
        run = self.run_script(ROOT / "start-linux.sh", PATH=str(no_python))
        self.assertEqual(run.returncode, 1)
        self.assertIn("sudo apt install python3", run.stdout)

    @unittest.skipIf(os.geteuid() == 0 if hasattr(os, "geteuid") else False, "the installer refuses to run as root")
    def test_raspberry_pi_installer(self):
        app = self.tmp / "app"
        app.mkdir()
        (app / "reddit_tracker.py").write_text("import os\nprint(os.environ['FAKE_LINK'])\n", encoding="utf-8")
        source = (ROOT / "install-raspberry-pi.sh").read_text(encoding="utf-8")
        self.assertEqual((source.count("\nPY=/usr/bin/python3\n"), source.count("\nPORT=8765\n")), (1, 1))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ping)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def install(python, answer=None, **env):
            script = app / "install-raspberry-pi.sh"
            script.write_text(source.replace("\nPY=/usr/bin/python3\n", f"\nPY={shlex.quote(str(python))}\n")
                              .replace("\nPORT=8765\n", f"\nPORT={port if answer else 1}\n"), encoding="utf-8")
            server.answer = answer
            return self.run_script(script, **env)

        # Python too old: advice that works, and nothing installed
        run = install(self.fake_python((3, 7, 3)))
        self.assertEqual(run.returncode, 1)
        self.assertIn("it has Python 3.7.3", run.stdout)
        self.assertIn("Raspberry Pi Imager", run.stdout)
        self.assertNotIn("full-upgrade", run.stdout)
        self.assertFalse((self.tmp / "sudo.log").exists())

        link = "http://raspberrypi.local:8765/?key=k3yk3yk3yk3y"
        run = install(sys.executable, {"app": "reddit-tracker", "version": "1", "network": True}, FAKE_LINK=link)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn(link, run.stdout)
        self.assertIn("bookmark", run.stdout)
        unit = (self.tmp / "unit").read_text(encoding="utf-8")
        for line in ("After=network-online.target time-sync.target", "Wants=network-online.target",
                     "Restart=always", "RestartSec=5", "RestartPreventExitStatus=3",
                     f'ExecStart={sys.executable} "{app}/reddit_tracker.py" --network --no-browser --port {port}'):
            self.assertIn(line + "\n", unit)

        other_copy = "already open on this Raspberry Pi"
        for answer, env, words in (
                ({"app": "reddit-tracker", "network": False}, {}, other_copy),  # a window opened with start-linux.sh
                (None, {"FAKE_STATE": "failed", "FAKE_STATUS": "3"}, other_copy),  # the service said so, then stopped
                (None, {"FAKE_STATE": "failed", "FAKE_STATUS": "1"}, "journalctl -u reddit-tracker"),
                ({"app": "reddit-tracker"}, {"FAKE_LINK": "Reddit Tracker hasn't started in network mode yet."},
                 "didn't make its link")):  # an older copy that doesn't say whether it's in network mode
            run = install(sys.executable, answer, **env)
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertIn(words, run.stdout)
            self.assertNotIn("bookmark", run.stdout)


if __name__ == "__main__":
    unittest.main()
