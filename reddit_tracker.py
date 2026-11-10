#!/usr/bin/env python3
"""Start Reddit Tracker:  python3 reddit_tracker.py  (or double-click a start file).

Options: --port 8765, --network (other devices may use it with the link), --no-browser,
--data DIR (where it keeps its files), --show-link (print the link for other devices and stop).
"""
# Unlike the rt files, no "from __future__ import annotations", annotations or f-strings here: Python 3.6
# and older would stop at them before the version check below could say what's wrong.
import argparse
import os
import sys

if sys.version_info < (3, 9):
    sys.exit("Reddit Tracker needs Python 3.9 or newer. Get it free from https://www.python.org/downloads/")


def port_number(text):
    """--port: a whole number from 1 to 65535."""
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("use a number from 1 to 65535, for example 8765")
    return port


def parse(argv):
    ap = argparse.ArgumentParser(prog="reddit_tracker.py",
                                 description="Keeps your clients' Reddit Pro numbers up to date in Google Sheets.")
    ap.add_argument("--port", type=port_number, default=None, help="the port to use (8765 unless it's taken)")
    ap.add_argument("--network", action="store_true",
                    help="let other devices on this network use it, with the link it prints")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page in the browser")
    ap.add_argument("--data", default=None, help="the folder for settings, the Google sign-in and the numbers")
    ap.add_argument("--show-link", action="store_true", help="print the link for other devices, then stop")
    return ap.parse_args(argv)


def quick_edit_off():
    """On Windows a click in the console window pauses the app ("QuickEdit"); turn that off."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, (mode.value & ~0x40) | 0x80)
    except Exception:
        pass


if __name__ == "__main__":
    args = parse(sys.argv[1:])
    os.umask(0o077)  # every file and folder it makes is readable by this computer account only
    quick_edit_off()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from rt import server
    sys.exit(server.main(args))
