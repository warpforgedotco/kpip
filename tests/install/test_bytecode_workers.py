"""Byte-compilation worker processes.

The pool is an optimization with a fallback behind it, so most of what
matters is what happens when it does not work: a worker that will not start,
one that stops answering, a path the protocol cannot carry. Every one of
those has to come back as "these jobs are yours to compile", never as a
failed install.
"""

from __future__ import annotations

import importlib.util
import marshal
import sys
from pathlib import Path

import pytest
from kpip.install import bytecode


@pytest.fixture(autouse=True)
def _isolated_pool() -> object:
    """Never share the module-level pool with other tests."""
    bytecode.shutdown()
    yield
    bytecode.shutdown()


def _job(tmp_path: Path, name: str, source: str) -> bytecode.CompileJob:
    module = tmp_path / f"{name}.py"
    module.write_text(source)
    return (
        str(module),
        str(tmp_path / "out" / f"{name}.pyc"),
        f"pkg/{name}.py",
    )


def test_workers_compile_and_bake_the_display_name(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    job = _job(tmp_path, "mod", "def f():\n    return 7\n")

    assert bytecode.compile_jobs([job]) == []

    body = Path(job[1]).read_bytes()

    assert body[:4] == importlib.util.MAGIC_NUMBER

    code = marshal.loads(body[16:])

    assert code.co_filename == "pkg/mod.py"


def test_a_module_that_will_not_compile_is_reported_as_done(tmp_path: Path) -> None:
    """The worker acknowledges it either way; only the .pyc is missing."""
    (tmp_path / "out").mkdir()
    good = _job(tmp_path, "good", "OK = 1\n")
    bad = _job(tmp_path, "bad", "print 'python 2'\n")

    assert bytecode.compile_jobs([good, bad]) == []
    assert Path(good[1]).is_file()
    assert not Path(bad[1]).exists()


def test_a_batch_larger_than_the_pool_completes(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    jobs = [_job(tmp_path, f"m{index:03d}", f"V = {index}\n") for index in range(60)]

    assert bytecode.compile_jobs(jobs) == []
    assert all(Path(job[1]).is_file() for job in jobs)


def test_the_pool_is_reused_across_batches(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    first = _job(tmp_path, "one", "A = 1\n")

    bytecode.compile_jobs([first])
    started = bytecode._POOL

    bytecode.compile_jobs([_job(tmp_path, "two", "B = 2\n")])

    assert bytecode._POOL is started, "a second batch restarted the workers"


def test_untransmittable_paths_come_back_to_the_caller(tmp_path: Path) -> None:
    """A tab or newline in a path would be misparsed by the line protocol."""
    (tmp_path / "out").mkdir()
    fine = _job(tmp_path, "fine", "C = 3\n")
    awkward = (str(tmp_path / "we\tird.py"), str(tmp_path / "out" / "x.pyc"), "x.py")

    returned = bytecode.compile_jobs([fine, awkward])

    assert returned == [awkward]
    assert Path(fine[1]).is_file()


def test_jobs_come_back_when_no_worker_can_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(bytecode, "_spawn", lambda: None)

    jobs = [_job(tmp_path, "orphan", "D = 4\n")]

    assert bytecode.compile_jobs(jobs) == jobs
    assert not Path(jobs[0][1]).exists()


def test_jobs_come_back_when_a_worker_stops_answering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that breaks the echo protocol is not trusted again."""
    (tmp_path / "out").mkdir()

    monkeypatch.setattr(bytecode._Worker, "compile", lambda self, job: False)

    jobs = [_job(tmp_path, f"lost{index}", f"E = {index}\n") for index in range(5)]

    returned = bytecode.compile_jobs(jobs)

    assert sorted(returned) == sorted(jobs)


def test_empty_batch_starts_nothing(tmp_path: Path) -> None:
    assert bytecode.compile_jobs([]) == []
    assert bytecode._POOL is None


def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    bytecode.compile_jobs([_job(tmp_path, "sd", "F = 6\n")])

    bytecode.shutdown()
    bytecode.shutdown()

    assert bytecode._POOL is None


def test_worker_script_runs_under_the_current_interpreter() -> None:
    """The script must stay runnable standalone -- it is executed, not imported."""
    import subprocess

    script = Path(bytecode._worker_script())

    assert script.is_file()

    result = subprocess.run(
        [sys.executable, str(script)],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.stdout.splitlines()[:1] == ["Ready"]
    assert result.returncode == 0


def test_kpip_itself_is_a_worker_given_the_worker_argument(tmp_path) -> None:
    """How a compiled kpip, which has no worker script to run, starts one:
    ``kpip/__main__.py`` hands the process to the worker loop."""
    import subprocess
    import sys

    from kpip.install._compile_worker import WORKER_ARGUMENT

    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    destination = tmp_path / "module.pyc"
    worker = subprocess.Popen(
        [sys.executable, "-m", "kpip", WORKER_ARGUMENT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert worker.stdout is not None
        assert worker.stdin is not None
        assert worker.stdout.readline().strip() == "Ready"
        worker.stdin.write(f"{source}\t{destination}\tmodule.py\n")
        worker.stdin.flush()
        assert worker.stdout.readline().strip() == str(source)
    finally:
        worker.stdin.close()
        worker.wait(timeout=30)

    assert destination.is_file()


def test_a_compiled_kpip_starts_itself_as_the_worker(monkeypatch) -> None:
    from kpip.core import interpreter
    from kpip.install import bytecode
    from kpip.install._compile_worker import WORKER_ARGUMENT

    monkeypatch.setattr(interpreter, "is_compiled", lambda: True)
    monkeypatch.setattr(interpreter, "own_command", lambda: ["/opt/kpip"])

    assert bytecode._worker_command() == ["/opt/kpip", WORKER_ARGUMENT]
