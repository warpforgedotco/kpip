"""A lock runs with garbage collection off, and gets it back afterwards.

Collecting a lock's heap reclaims nothing -- it is the catalog and the clause
set, alive until the command ends -- so it only costs time.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest
from kpip.cli import entrypoint, fast


@pytest.fixture(autouse=True)
def restore_collection() -> Iterator[None]:
    enabled = gc.isenabled()
    try:
        yield
    finally:
        if enabled:
            gc.enable()
        else:
            gc.disable()


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Whether collection was on while each command ran."""
    monkeypatch.delenv("KPIP_GC", raising=False)
    monkeypatch.setattr(fast, "run_before_startup", lambda argv: (None, True))
    monkeypatch.setattr(fast, "suppresses_logging", lambda argv, log_file: True)
    states: list[bool] = []
    monkeypatch.setattr(
        entrypoint,
        "run_command",
        lambda argv, spec: states.append(gc.isenabled()) or 0,
    )
    return states


def test_a_lock_runs_uncollected_and_gets_collection_back(seen: list[bool]) -> None:
    gc.enable()

    assert entrypoint.main(["lock"]) == 0

    assert seen == [False]
    assert gc.isenabled(), "main is importable; collection is interpreter-wide"


def test_a_process_about_to_exit_keeps_collection_off(seen: list[bool]) -> None:
    """Turning it back on would collect the whole heap just before the exit."""
    gc.enable()

    assert entrypoint.main(["lock"], keep_collection_paused=True) == 0

    assert seen == [False]
    assert not gc.isenabled()


def test_other_commands_stay_collected(seen: list[bool]) -> None:
    gc.enable()

    assert entrypoint.main(["check"]) == 0

    assert seen == [True]


def test_the_escape_hatch_keeps_a_lock_collected(
    monkeypatch: pytest.MonkeyPatch, seen: list[bool]
) -> None:
    monkeypatch.setenv("KPIP_GC", "default")
    gc.enable()

    assert entrypoint.main(["lock"]) == 0

    assert seen == [True]


def test_collection_a_caller_turned_off_stays_off(seen: list[bool]) -> None:
    gc.disable()

    assert entrypoint.main(["lock"]) == 0

    assert seen == [False]
    assert not gc.isenabled()


def test_collection_comes_back_when_a_lock_raises(
    monkeypatch: pytest.MonkeyPatch, seen: list[bool]
) -> None:
    def boom(argv: list[str], spec: object) -> int:
        raise RuntimeError("command failed")

    monkeypatch.setattr(entrypoint, "run_command", boom)
    gc.enable()

    with pytest.raises(RuntimeError):
        entrypoint.main(["lock"])

    assert gc.isenabled()
