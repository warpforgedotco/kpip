"""Wheels unpacked on subinterpreters fill the archive cache as threads do."""

from __future__ import annotations

import filecmp
import hashlib
import os
import zipfile
from pathlib import Path

import pytest
from kpip.core.wheel import wheel_candidate
from kpip.install import archive_workers
from kpip.install.wheel_archive_cache import prepare_cached_wheel


def _wheel(directory: Path, name: str, members: int = 80) -> Path:
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(members):
            archive.writestr(f"{name}/mod_{index:03d}.py", f"VALUE = {index}\n" * 20)
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return wheel


def _candidate(wheel: Path) -> object:
    return wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )


@pytest.fixture
def workers():
    started = archive_workers.start_archive_workers()
    if started is None:
        pytest.skip("no subinterpreters on this interpreter")
    yield started
    started.close()


@pytest.mark.parametrize("pycompile", [False, True])
def test_a_wheel_unpacked_in_a_worker_is_the_entry_threads_make(
    tmp_path: Path, workers: archive_workers.ArchiveWorkers, pycompile: bool
) -> None:
    wheel = _wheel(tmp_path, "workerpkg")

    in_worker = workers.prepare(
        _candidate(wheel), str(tmp_path / "workers"), pycompile=pycompile
    )
    in_main = prepare_cached_wheel(
        _candidate(wheel), str(tmp_path / "main"), pycompile=pycompile
    )

    assert in_worker.entries == in_main.entries
    assert in_worker.dist_info == in_main.dist_info
    comparison = filecmp.dircmp(in_worker.tree, in_main.tree)
    assert not comparison.diff_files
    assert not comparison.left_only
    assert not comparison.right_only


def test_a_wheel_a_worker_fails_on_raises_as_the_main_path_does(
    tmp_path: Path, workers: archive_workers.ArchiveWorkers
) -> None:
    wheel = _wheel(tmp_path, "brokenpkg")
    raw = bytearray(wheel.read_bytes())
    # Corrupt the first member's compressed data, leaving the directory intact.
    raw[60:80] = b"\xff" * 20
    wheel.write_bytes(bytes(raw))

    with pytest.raises(Exception) as in_main:
        prepare_cached_wheel(_candidate(wheel), str(tmp_path / "main"), pycompile=False)
    with pytest.raises(Exception) as in_worker:
        workers.prepare(_candidate(wheel), str(tmp_path / "workers"), pycompile=False)

    assert type(in_worker.value) is type(in_main.value)
    assert str(in_worker.value) == str(in_main.value)


def test_workers_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPIP_SUBINTERPRETERS", "0")

    assert archive_workers.start_archive_workers() is None


@pytest.mark.parametrize("frozen", [False, True])
def test_a_compiled_kpip_uses_workers_only_with_their_bytecode(
    monkeypatch: pytest.MonkeyPatch, frozen: bool
) -> None:
    """A compiled kpip's modules are inside the binary, where a new
    interpreter cannot import them; workers start only when the binary also
    carries their bytecode, in its frozen table."""

    monkeypatch.delenv("KPIP_SUBINTERPRETERS", raising=False)
    monkeypatch.setattr(archive_workers, "is_compiled", lambda: True)
    monkeypatch.setattr(
        archive_workers._imp,
        "is_frozen",
        lambda name: frozen and name == "kpip.install.archive_workers",
    )

    started = archive_workers.start_archive_workers()

    assert (started is not None) is frozen
    if started is not None:
        started.close()


def test_nothing_crosses_but_paths_and_a_digest(tmp_path: Path) -> None:
    """The job runs anywhere its module imports: with a path and a digest."""
    wheel = _wheel(tmp_path, "plainpkg", members=3)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

    entry_root = archive_workers.unpack_in_worker(
        os.fspath(wheel), digest, str(tmp_path / "cache"), False
    )

    assert os.path.isdir(os.path.join(entry_root, "tree", "plainpkg"))


@pytest.mark.parametrize("pycompile", [False, True])
def test_the_job_runs_in_a_subinterpreter(
    tmp_path: Path, workers: archive_workers.ArchiveWorkers, pycompile: bool
) -> None:
    """Not the fallback: kpip imports, unpacks and byte-compiles in the
    worker itself."""
    wheel = _wheel(tmp_path, "subpkg", members=3)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

    entry_root = (
        workers._started()
        .submit(
            archive_workers.unpack_in_worker,
            os.fspath(wheel),
            digest,
            str(tmp_path / "cache"),
            pycompile,
        )
        .result()
    )

    assert os.path.isdir(os.path.join(entry_root, "tree", "subpkg"))
    compiled = list(Path(entry_root).rglob("*.pyc"))
    assert len(compiled) == (3 if pycompile else 0)


def test_a_wheel_already_unpacked_starts_no_worker(tmp_path: Path) -> None:
    """A warm install finds every wheel unpacked and starts no subinterpreter."""
    workers = archive_workers.start_archive_workers()
    if workers is None:
        pytest.skip("no subinterpreters on this interpreter")
    wheel = _wheel(tmp_path, "warmpkg", members=3)
    cache = str(tmp_path / "cache")
    unpacked = prepare_cached_wheel(_candidate(wheel), cache, pycompile=False)

    archive = workers.prepare(_candidate(wheel), cache, pycompile=False)

    assert archive.tree == unpacked.tree
    assert workers._executor is None
    workers.close()
