"""Training for the profile-guided build of MonolithPy, from kpip's own work.

CPython's profile-guided build runs ``PROFILE_TASK`` with the interpreter it
has just built, instrumented, and optimizes for what that run touched;
MonolithPy's task is a slice of CPython's test suite. This runs that slice
too, then kpip from source over the training steps of
:mod:`kpip_compile.pgo`, with fewer locks: the instrumented interpreter is
several times slower than the one it becomes.

``kpip-compile profile-task SCRIPT`` writes a launcher for it, which
MonolithPy's ``build.sh`` and ``build.mac.sh`` run in place of their own task
when ``MONOLITHPY_PROFILE_TASK`` names it.

A compiled kpip spends its time in the runtime its C calls -- objects,
dicts, strings, imports -- rather than in the bytecode loop a source run
exercises most. This trains both; only comparing a build trained on it with
one trained on the test suite alone says what it is worth.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kpip_compile.pgo import WEB_REQUIREMENTS, training_steps
from kpip_compile.vendor import REPO_ROOT

PROFILE_RESOLVES: tuple[tuple[str, str | None], ...] = (
    ("black.in", None),
    ("boto3.in", None),
    ("jupyter.in", None),
    ("trio.in", "3.12"),
    ("backtracking/starlette-fastapi.in", "3.12"),
)
"""Small and large graphs, a lock for another Python, and backtracking."""

CPYTHON_TASK = ("-m", "test", "--pgo", "-x", "test_json")
"""MonolithPy's own ``PROFILE_TASK``, kept in the mix."""

LAUNCHER = """\
import sys

sys.path.insert(0, {source!r})

from kpip_compile.interpreter_pgo import main

sys.exit(main())
"""


def target_python() -> str | None:
    """A CPython on ``PATH`` for the training to install for.

    Run from source, kpip installs for the Python running it: here the
    MonolithPy being built, whose ``monolithpy`` tags no published wheel
    carries, so its installs would build every sdist instead. A compiled
    kpip installs for a CPython, as this one then does.
    """
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found is None:
            continue
        tag = subprocess.run(
            [found, "-c", "import sys; print(sys.implementation.cache_tag)"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if (tag or "").startswith("cpython-"):
            return found
    return None


def train(python: str) -> int:
    """Run the CPython slice, then kpip from source, with ``python``.

    A step that fails still left a profile behind, and the build ignores
    the task's status, so failures are counted and named, not raised.
    """
    print(f"interpreter pgo: {' '.join(CPYTHON_TASK)}", flush=True)
    subprocess.run([python, *CPYTHON_TASK], check=False)

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    target = target_python()
    if target is None:
        print(
            "interpreter pgo: no CPython on PATH; installs are for this "
            "interpreter, and build sdists instead",
            flush=True,
        )
    else:
        print(f"interpreter pgo: installing for {target}", flush=True)
        environment["KPIP_PYTHON"] = target
    with tempfile.TemporaryDirectory(prefix="kpip-interpreter-pgo-") as directory:
        work = Path(directory)
        cache = work / "cache"
        (work / "web.txt").write_text(WEB_REQUIREMENTS, encoding="utf-8")

        steps = training_steps(work, cache, PROFILE_RESOLVES)
        failed = 0
        for number, step in enumerate(steps, start=1):
            shutil.rmtree(cache / "v1" / "lock-replay-v1", ignore_errors=True)
            print(
                f"interpreter pgo [{number}/{len(steps)}]: "
                f"kpip {' '.join(step.arguments[:2])}",
                flush=True,
            )
            result = subprocess.run(
                [python, "-m", "kpip", *step.arguments],
                env=environment,
                cwd=work,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                failed += 1
                lines = (result.stderr or "").strip().splitlines()
                print(
                    f"interpreter pgo [{number}/{len(steps)}] failed "
                    f"({result.returncode}): {lines[-1] if lines else ''}",
                    flush=True,
                )
    print(f"interpreter pgo: {len(steps)} kpip steps, {failed} failed", flush=True)
    return 0


def write_launcher(script: Path) -> Path:
    """Write the script ``MONOLITHPY_PROFILE_TASK`` names; returns its path.

    The build runs it with the interpreter it has just built, from its own
    source tree, so the launcher carries where this package is.
    """
    script = script.resolve()
    script.parent.mkdir(parents=True, exist_ok=True)
    source = str(Path(__file__).resolve().parents[1])
    script.write_text(LAUNCHER.format(source=source), encoding="utf-8")
    return script


def main() -> int:
    return train(sys.executable)
