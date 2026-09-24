"""The kpip process ends without tearing the interpreter down.

Teardown frees every object of a resolve's heap one at a time; skipping it
must not skip anything a normal exit does that can be observed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SCRIPT = """
import atexit, concurrent.futures, sys, threading, time
from pathlib import Path
from kpip.cli.entrypoint import exit_without_teardown

log = Path(sys.argv[1])

def write(line):
    with log.open("a") as handle:
        handle.write(line + "\\n")

def thread_work():
    time.sleep(0.2)
    write("thread")

def executor_work():
    time.sleep(0.2)
    write("executor")

threading.Thread(target=thread_work).start()
concurrent.futures.ThreadPoolExecutor(1).submit(executor_work)
atexit.register(write, "atexit")
sys.stdout.write("no newline, still buffered")
exit_without_teardown(3)
"""


def _run(tmp_path: Path, script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), str(tmp_path / "log")],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def test_threads_executors_atexit_and_output_all_finish(tmp_path: Path) -> None:
    result = _run(tmp_path, _SCRIPT)

    assert result.returncode == 3
    assert result.stdout == "no newline, still buffered"
    lines = (tmp_path / "log").read_text().splitlines()
    assert sorted(lines[:2]) == ["executor", "thread"], "workers are waited for"
    assert lines[2:] == ["atexit"], "atexit callbacks run after the workers"


def test_a_stream_that_cannot_be_flushed_exits_with_120(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
        import io, sys
        from kpip.cli.entrypoint import exit_without_teardown

        class Broken(io.StringIO):
            def flush(self):
                raise BrokenPipeError

        sys.stdout = Broken()
        exit_without_teardown(0)
        """,
    )

    assert result.returncode == 120


@pytest.mark.parametrize("status", [0, 3])
def test_the_escape_hatch_exits_normally(tmp_path: Path, status: int) -> None:
    result = _run(
        tmp_path,
        f"""
        import sys
        from kpip.cli.entrypoint import exit_without_teardown

        sys.stdout.write("out")
        exit_without_teardown({status})
        """,
        KPIP_EXIT="full",
    )

    assert (result.returncode, result.stdout) == (status, "out")


def test_the_module_entry_point_uses_it() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "kpip", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("kpip ")
