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
    monkeypatch.setattr(bytecode, "_spawn", lambda command: None)

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


def _target(monkeypatch: pytest.MonkeyPatch, **facts: object) -> None:
    import types

    defaults = {
        "is_own": False,
        "executable": "/opt/python3.12",
        "cache_tag": sys.implementation.cache_tag,
        "magic": importlib.util.MAGIC_NUMBER.hex(),
    }
    other = types.SimpleNamespace(**(defaults | facts))
    monkeypatch.setattr(bytecode, "target_interpreter", lambda **_: other)
    # Not the Python running kpip, which is read from sys.implementation.
    monkeypatch.setattr(bytecode, "is_compiled", lambda: True)


def test_another_python_that_compiles_as_this_one_takes_this_ones_bytecode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target(monkeypatch)

    assert bytecode.compiles_as_this_process()


@pytest.mark.parametrize("facts", [{"cache_tag": "cpython-312"}, {"magic": "00000000"}])
def test_another_version_does_not_compile_as_this_one(
    monkeypatch: pytest.MonkeyPatch, facts: dict[str, str]
) -> None:
    _target(monkeypatch, **facts)

    assert not bytecode.compiles_as_this_process()


def test_the_workers_are_the_target_interpreter_handed_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compiled kpip has no script on disk to point it at."""
    from kpip.install._compile_worker import SOURCE

    _target(monkeypatch)

    assert bytecode._worker_command() == ["/opt/python3.12", "-c", SOURCE]


def test_another_versions_modules_are_not_compiled_here(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What its workers decline goes without bytecode: this process would
    write its own version's under the other's name."""
    (tmp_path / "out").mkdir()
    job = _job(tmp_path, "foreign", "A = 1\n")
    _target(monkeypatch, cache_tag="cpython-312")
    monkeypatch.setattr(bytecode, "_spawn", lambda command: None)

    bytecode.compile_modules([job])

    assert not Path(job[1]).exists()


def test_the_pyc_name_is_the_target_interpreters_unoptimized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target(monkeypatch, cache_tag="cpython-312")

    assert bytecode.pyc_name("mod.py") == "mod.cpython-312.pyc"
    assert bytecode.pyc_name("a.b.py") == "a.b.cpython-312.pyc"


def test_the_worker_loop_runs_given_as_text(tmp_path: Path) -> None:
    """As another interpreter runs it, with ``-c``."""
    import subprocess

    from kpip.install._compile_worker import SOURCE

    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    destination = tmp_path / "module.pyc"

    result = subprocess.run(
        [sys.executable, "-c", SOURCE],
        input=f"{source}\t{destination}\tpkg/module.py\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert result.stdout.splitlines() == ["Ready", str(source)]
    code = marshal.loads(destination.read_bytes()[16:])
    assert code.co_filename == "pkg/module.py"


def test_the_worker_loop_takes_paths_in_utf8_whatever_the_locale(
    tmp_path: Path,
) -> None:
    """A pipe speaks the locale's code page; C:\\Users\\José came out mangled."""
    import os
    import subprocess

    from kpip.install._compile_worker import SOURCE

    directory = tmp_path / "José"
    directory.mkdir()
    source = directory / "module.py"
    source.write_text("VALUE = 1\n")
    destination = directory / "module.pyc"

    result = subprocess.run(
        [sys.executable, "-c", SOURCE],
        input=f"{source}\t{destination}\tmodule.py\n".encode(),
        capture_output=True,
        timeout=30,
        check=True,
        env={**os.environ, "PYTHONIOENCODING": "latin-1", "PYTHONUTF8": "0"},
    )

    assert result.stdout.decode().splitlines() == ["Ready", str(source)]
    assert destination.is_file()
