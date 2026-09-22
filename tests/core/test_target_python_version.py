"""Resolving for an interpreter other than the one running the resolve."""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
from kpip.core.packaging import (
    default_environment,
    marker_applies,
    normalize_python_version,
    set_target_python_version,
    target_python_version,
)
from kpip.index.candidate_evaluators import CandidateEvaluator

RUNNING = f"{sys.version_info[0]}.{sys.version_info[1]}"


@pytest.fixture(autouse=True)
def restore_target() -> Iterator[None]:
    """No test may leave a target behind: it is process-global state."""
    try:
        yield
    finally:
        set_target_python_version(None)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("3.8", "3.8.0"),
        ("38", "3.8.0"),
        ("3.8.2", "3.8.2"),
        ("2", "2.0.0"),
        ("310", "310"),
    ],
)
def test_normalize_python_version(value: str, expected: str) -> None:
    assert normalize_python_version(value) == expected


def test_no_target_means_the_running_interpreter() -> None:
    assert target_python_version() is None
    assert default_environment()["python_version"] == RUNNING


def test_a_target_moves_both_version_fields() -> None:
    set_target_python_version("3.8.0")

    environment = default_environment()

    assert environment["python_version"] == "3.8"
    assert environment["python_full_version"] == "3.8.0"

    if sys.implementation.name == "cpython":
        assert environment["implementation_version"] == "3.8.0"


def test_a_target_leaves_the_platform_alone() -> None:
    """`--python-version` is not `--platform`: the machine does not move."""
    before = default_environment()

    set_target_python_version("3.8.0")

    after = default_environment()

    for field in ("os_name", "sys_platform", "platform_machine", "platform_system"):
        assert after[field] == before[field]


def test_markers_follow_the_target() -> None:
    marker = 'python_version < "3.9"'

    assert marker_applies(marker) is False

    set_target_python_version("3.8.0")

    assert marker_applies(marker) is True


def test_requires_python_follows_the_target() -> None:
    assert CandidateEvaluator.requires_python_matches("<3.9") is False

    set_target_python_version("3.8.0")

    assert CandidateEvaluator.requires_python_matches("<3.9") is True


def test_an_explicit_target_beats_the_process_wide_one() -> None:
    """The callers that hold a target of their own are not overridden."""
    set_target_python_version("3.8.0")

    assert CandidateEvaluator.requires_python_matches("<3.9", "3.12.0") is False
    assert CandidateEvaluator.requires_python_matches(">=3.12", "3.12.0") is True


def test_clearing_the_target_restores_the_running_interpreter() -> None:
    set_target_python_version("3.8.0")
    assert marker_applies('python_version < "3.9"') is True

    set_target_python_version(None)

    assert target_python_version() is None
    assert marker_applies('python_version < "3.9"') is False
    assert default_environment()["python_version"] == RUNNING
