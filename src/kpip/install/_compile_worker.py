"""The byte-compilation worker loop that :mod:`kpip.install.bytecode` runs.

Compiling holds the GIL, so threads cannot make it parallel; this runs in a
child process instead. The process is reused for every module in a session,
so the interpreter is started once rather than once per file.

Protocol, one line each way, over stdin/stdout:

* the worker prints ``Ready`` once it can accept work;
* the parent writes ``<source>\\t<destination>\\t<display>``;
* the worker compiles and echoes ``<source>`` back.

The echo is both the acknowledgement and a sanity check -- getting the path
back is what tells the parent this really is a Python interpreter that ran
this script, and it keeps exactly one file in flight per worker so the parent
can attribute a hang or a crash to the file that caused it.

The loop is :data:`SOURCE`, text rather than code, because the interpreter
that runs it is the one kpip installs for, which compiles its own bytecode.
It is handed the loop with ``-c`` -- a compiled kpip has no file to point it
at -- so it is written for any Python kpip installs for.
Modules are compiled unoptimized, whatever flags run the worker, to match
the ``.pyc`` names kpip gives them.

Compilation failures are reported as an acknowledgement like any other. A
wheel that ships a module this interpreter cannot compile -- vendored Python
2 is the usual reason -- still installs; it simply has no bytecode for that
module, which is what pip does too.
"""

SOURCE = r"""
import sys

# -c put the working directory first on sys.path: a py_compile.py there
# must not stand in for the standard library's. Only that entry: under
# PYTHONSAFEPATH the first is the standard library's.
if sys.path and sys.path[0] == "":
    del sys.path[0]

# kpip writes and reads UTF-8; a pipe otherwise speaks the locale's code
# page, which on Windows mangles a path like C:\Users\José.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

import py_compile
import warnings

# A module that warns at compile time (SyntaxWarning, most often) is not
# this install's problem to report, and anything written to stderr risks
# being read as protocol noise.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore")

    print("Ready", flush=True)

    for line in sys.stdin:
        record = line.rstrip("\n")

        if not record:
            continue

        source, _, rest = record.partition("\t")
        destination, _, display = rest.partition("\t")

        try:
            try:
                py_compile.compile(
                    source,
                    cfile=destination,
                    dfile=display or None,
                    doraise=False,
                    optimize=0,
                    quiet=2,
                )

            except TypeError:
                # 3.7, before quiet.
                py_compile.compile(
                    source,
                    cfile=destination,
                    dfile=display or None,
                    doraise=False,
                    optimize=0,
                )

        except (OSError, ValueError, RecursionError, MemoryError, SyntaxError):
            pass

        print(source, flush=True)
"""
