"""A command hands the interpreter lock over less often than CPython asks.

A resolve runs dozens of fetch workers against one interpreter lock while
the main thread does the catalog and resolver work, and the default 5 ms
handover only multiplies system calls that buy nothing when the waiting
threads are blocked on sockets.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
from kpip.cli import entrypoint


@pytest.fixture(autouse=True)
def restore_interval() -> Iterator[None]:
    saved = sys.getswitchinterval()
    try:
        yield
    finally:
        sys.setswitchinterval(saved)


def test_the_interval_is_raised_and_the_old_one_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPIP_SWITCH_INTERVAL", raising=False)
    sys.setswitchinterval(0.005)

    previous = entrypoint.switch_threads_less_often()

    assert previous == pytest.approx(0.005)
    assert sys.getswitchinterval() == pytest.approx(entrypoint._SWITCH_INTERVAL_SECONDS)
    assert entrypoint._SWITCH_INTERVAL_SECONDS > 0.005, "handovers must get rarer"


def test_the_escape_hatch_leaves_cpython_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPIP_SWITCH_INTERVAL", "default")
    sys.setswitchinterval(0.005)

    assert entrypoint.switch_threads_less_often() is None

    assert sys.getswitchinterval() == pytest.approx(0.005)


def test_a_finished_command_leaves_the_interval_as_it_found_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` is importable, and the interval is interpreter-wide."""
    monkeypatch.delenv("KPIP_SWITCH_INTERVAL", raising=False)
    monkeypatch.delenv("KPIP_GC", raising=False)
    sys.setswitchinterval(0.005)
    seen: list[float] = []
    monkeypatch.setattr(
        entrypoint,
        "run_command",
        lambda argv, spec: seen.append(sys.getswitchinterval()) or 0,
    )

    assert entrypoint.main(["check"]) == 0

    assert seen == [pytest.approx(entrypoint._SWITCH_INTERVAL_SECONDS)]
    assert sys.getswitchinterval() == pytest.approx(0.005)


def test_the_interval_is_restored_even_when_a_command_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPIP_SWITCH_INTERVAL", raising=False)
    sys.setswitchinterval(0.005)

    def boom(argv: list[str], spec: object) -> int:
        raise RuntimeError("command failed")

    monkeypatch.setattr(entrypoint, "run_command", boom)

    with pytest.raises(RuntimeError):
        entrypoint.main(["check"])

    assert sys.getswitchinterval() == pytest.approx(0.005)
