"""Run every test:  python3 tests/run_tests.py  [-v]  [settings google ...]

Names pick test files (settings -> tests/test_settings.py). The exit code is 0 only when all pass.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    names = [a for a in argv if a != "-v"]
    sys.dont_write_bytecode = True  # keep the app folder free of __pycache__
    for folder in (ROOT, HERE):  # `import rt` and the tests' own helpers
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for pattern in [f"test_{n}.py" for n in names] or ["test_*.py"]:
        suite.addTests(loader.discover(str(HERE), pattern=pattern, top_level_dir=str(HERE)))
    with tempfile.TemporaryDirectory() as data:
        os.environ["RT_DATA_DIR"] = data  # a test that forgets --data never touches the real data folder
        started = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    seconds = time.monotonic() - started
    bad = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    if result.testsRun == 0:
        print("No tests found.")
        return 1
    if bad:
        print(f"\n{bad} of {result.testsRun} tests failed ({seconds:.1f} s).")
        return 1
    skipped = f", {len(result.skipped)} skipped" if result.skipped else ""
    print(f"\nAll {result.testsRun} tests passed{skipped} ({seconds:.1f} s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
