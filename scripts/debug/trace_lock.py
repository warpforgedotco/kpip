"""Temporary diagnostic: run a cached lock and report every exception kpip raised.

Usage: python trace_lock.py CACHE_DIR OUTPUT REQUIREMENT
"""

import collections
import os
import sys
import traceback

counts: collections.Counter[tuple[str, str, int, str]] = collections.Counter()
first: dict[tuple[str, str, int, str], str] = {}

mon = sys.monitoring
TOOL = mon.DEBUGGER_ID
mon.use_tool_id(TOOL, "trace_lock")


def on_raise(code, offset, exc):
    filename = code.co_filename
    if "kpip" not in filename or "_vendor" in filename and "nab_resolver" not in filename:
        return
    key = (type(exc).__name__, os.path.relpath(filename).replace("\\", "/")[-70:], code.co_firstlineno, code.co_name)
    counts[key] += 1
    if key not in first:
        first[key] = f"{exc!r}"[:300]


mon.register_callback(TOOL, mon.events.RAISE, on_raise)
mon.set_events(TOOL, mon.events.RAISE)


def report():
    mon.set_events(TOOL, 0)
    print(f"\n=== exceptions raised in kpip ({sum(counts.values())} total)", file=sys.stderr)
    for key, n in counts.most_common(60):
        print(f"{n:6d} {key[0]:22s} {key[1]}:{key[2]} {key[3]}\n       {first[key]}", file=sys.stderr)


import atexit

atexit.register(report)

from kpip.cli.entrypoint import console_main

cache_dir, output, requirement = sys.argv[1:4]
sys.argv = ["kpip", "lock", "--cache-dir", cache_dir, "--output", output, requirement]
try:
    console_main()
except SystemExit as exc:
    print("exit", exc.code, file=sys.stderr)
    raise
