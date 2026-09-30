"""Byte-compilation worker, run as a script by :mod:`kpip.install.bytecode`.

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

Compilation failures are reported as an acknowledgement like any other. A
wheel that ships a module this interpreter cannot compile -- vendored Python
2 is the usual reason -- still installs; it simply has no bytecode for that
module, which is what pip does too.
"""

import sys

WORKER_ARGUMENT = "--kpip-compile-worker"
"""What a compiled kpip is started with to be a worker: it has no script to
run, so ``kpip/__main__.py`` runs :func:`main` when this comes first."""


def main() -> None:
    import py_compile
    import warnings

    with warnings.catch_warnings():
        # A module that warns at compile time (SyntaxWarning, most often) is
        # not this install's problem to report, and anything written to
        # stderr risks being read as protocol noise.
        warnings.filterwarnings("ignore")

        print("Ready", flush=True)

        for line in sys.stdin:
            record = line.rstrip("\n")

            if not record:
                continue

            source, _, rest = record.partition("\t")
            destination, _, display = rest.partition("\t")

            try:
                py_compile.compile(
                    source,
                    cfile=destination,
                    dfile=display or None,
                    doraise=False,
                    quiet=2,
                )

            except (OSError, ValueError, RecursionError, MemoryError):
                pass

            print(source, flush=True)


if __name__ == "__main__":
    main()
