"""A command collects less often than CPython's defaults ask for.

A resolve keeps the index catalog and the resolver's clause set alive and
allocates millions of short-lived tuples through them, so the stock
thresholds spend a handful of full generation-2 traversals of a large live
heap reclaiming almost nothing.  Only the old-generation multipliers move,
so generation 0 still collects at its usual rate.
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


def test_only_the_old_generation_multipliers_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPIP_GC", raising=False)
    gc.set_threshold(700, 10, 10)

    entrypoint.collect_less_often()

    young, first, second = gc.get_threshold()
    assert young == 700, "generation 0 keeps collecting at its usual rate"
    assert (first, second) == (
        entrypoint._OLD_GENERATION_RATIO,
        entrypoint._OLD_GENERATION_RATIO,
    )
    assert first > 10, "a full traversal must become rarer, not more frequent"


def test_a_tuned_generation_zero_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever set generation 0 had a reason; only the multipliers are ours."""
    monkeypatch.delenv("KPIP_GC", raising=False)
    gc.set_threshold(5_000, 10, 10)

    entrypoint.collect_less_often()

    assert gc.get_threshold()[0] == 5_000


def test_the_escape_hatch_leaves_cpython_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPIP_GC", "default")
    gc.set_threshold(700, 10, 10)

    entrypoint.collect_less_often()

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
