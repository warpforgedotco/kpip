"""A command collects less often than CPython's defaults ask for.

A resolve keeps the index catalog and the resolver's clause set alive and
allocates millions of short-lived tuples through them, so the stock
thresholds spend their collections traversing a large live heap and reclaim
almost nothing.  Every generation collects less often, none is disabled.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest
from kpip.cli import entrypoint


@pytest.fixture(autouse=True)
def restore_thresholds() -> Iterator[None]:
    saved = gc.get_threshold()
    try:
        yield
    finally:
        gc.set_threshold(*saved)


def test_every_generation_collects_less_often(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPIP_GC", raising=False)
    gc.set_threshold(700, 10, 10)

    previous = entrypoint.collect_less_often()

    assert previous == (700, 10, 10), "the caller is handed what to restore"
    young, first, second = gc.get_threshold()
    assert young == entrypoint._YOUNG_GENERATION_THRESHOLD
    assert (first, second) == (
        entrypoint._OLD_GENERATION_RATIO,
        entrypoint._OLD_GENERATION_RATIO,
    )
    assert first > 10, "a full traversal must become rarer, not more frequent"


@pytest.mark.parametrize("tuned", [50_000, 0])
def test_a_generation_zero_tuned_further_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tuned: int,
) -> None:
    """A higher threshold had a reason, and 0 means collection is off."""
    monkeypatch.delenv("KPIP_GC", raising=False)
    gc.set_threshold(tuned, 10, 10)

    entrypoint.collect_less_often()

    assert gc.get_threshold()[0] == tuned


def test_the_escape_hatch_leaves_cpython_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPIP_GC", "default")
    gc.set_threshold(700, 10, 10)

    assert entrypoint.collect_less_often() is None

    assert gc.get_threshold() == (700, 10, 10)


def test_a_command_is_tuned_before_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collection is never left at CPython's settings for real work.

    ``check`` has no fast path to return through, so reaching it means the
    dispatch itself is covered rather than one command's shortcut.
    """
    events: list[str] = []
    monkeypatch.setattr(
        entrypoint,
        "collect_less_often",
        lambda: events.append("tuned"),
    )
    monkeypatch.setattr(
        entrypoint,
        "run_command",
        lambda argv, spec: events.append("ran") or 0,
    )

    assert entrypoint.main(["check"]) == 0

    assert events == ["tuned", "ran"]


def test_a_finished_command_leaves_the_thresholds_as_it_found_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` is importable, and thresholds are interpreter-wide."""
    monkeypatch.delenv("KPIP_GC", raising=False)
    gc.set_threshold(700, 10, 10)
    seen: list[tuple[int, ...]] = []
    monkeypatch.setattr(
        entrypoint,
        "run_command",
        lambda argv, spec: seen.append(gc.get_threshold()) or 0,
    )

    assert entrypoint.main(["check"]) == 0

    assert seen == [
        (
            entrypoint._YOUNG_GENERATION_THRESHOLD,
            entrypoint._OLD_GENERATION_RATIO,
            entrypoint._OLD_GENERATION_RATIO,
        )
    ]
    assert gc.get_threshold() == (700, 10, 10)


def test_the_thresholds_are_restored_even_when_a_command_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPIP_GC", raising=False)
    gc.set_threshold(700, 10, 10)

    def boom(argv: list[str], spec: object) -> int:
        raise RuntimeError("command failed")

    monkeypatch.setattr(entrypoint, "run_command", boom)

    with pytest.raises(RuntimeError):
        entrypoint.main(["check"])

    assert gc.get_threshold() == (700, 10, 10)
