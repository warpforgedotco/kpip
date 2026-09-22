"""Colour on the help path, and what it costs to decide there is none."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import pytest
from kpip.cli.parser import HelpFormatter, colour_is_possible

# argparse only colours help from 3.14; earlier versions never call the
# hook at all, so there is nothing there to keep cheap.
colours_help = pytest.mark.skipif(
    not hasattr(argparse.HelpFormatter, "_set_color"),
    reason="argparse colours help only from Python 3.14",
)


@pytest.fixture(autouse=True)
def clear_colour_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PYTHON_COLORS", "FORCE_COLOR", "NO_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)


class _Stdout:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_a_pipe_cannot_be_shown_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=False))

    assert colour_is_possible() is False


def test_a_terminal_can(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=True))

    assert colour_is_possible() is True


@pytest.mark.parametrize(
    "name, value, expected",
    [
        ("NO_COLOR", "1", False),
        ("TERM", "dumb", False),
        ("FORCE_COLOR", "1", True),
        ("PYTHON_COLORS", "0", True),
    ],
)
def test_the_environment_is_read_the_way_cpython_reads_it(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    expected: bool,
) -> None:
    """``PYTHON_COLORS=0`` still defers: saying no is _colorize's to say."""
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=not expected))
    monkeypatch.setenv(name, value)

    assert colour_is_possible() is expected


def test_stdout_that_cannot_answer_is_not_shown_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Closed:
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdout", Closed())

    assert colour_is_possible() is False


@colours_help
def test_a_pipe_gets_a_formatter_that_never_asked_about_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the override: no ``_colorize``, and no colour either."""
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=False))

    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("a pipe must not cost the colour machinery an import")

    monkeypatch.setattr(sys.modules["argparse"].HelpFormatter, "_set_color", fail)

    formatter = HelpFormatter("kpip")

    assert formatter._decolor("plain") == "plain"
    # Whatever colour argparse asks the theme for, it is given nothing.
    assert formatter._theme.heading == ""
    assert formatter._theme.summary_long_option == ""


@colours_help
def test_a_terminal_reaches_the_real_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=True))

    reached: list[bool] = []

    def record(self: Any, color: bool) -> None:
        reached.append(color)
        self._theme = None
        self._decolor = str

    monkeypatch.setattr(sys.modules["argparse"].HelpFormatter, "_set_color", record)

    HelpFormatter("kpip")

    assert reached == [True]
