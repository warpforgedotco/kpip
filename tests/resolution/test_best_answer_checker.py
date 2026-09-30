"""The best-answer checker finds a better answer exactly when there is one."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.resolution.best_answer import (
    InvalidAnswer,
    build_wheelhouse,
    check_best,
    wheelhouse_resolver,
)

# ``a`` 2 and ``b`` 2 cannot both be had.
TWO_ROOTS = {
    "a": {"2": ["b<2"], "1": []},
    "b": {"2": [], "1": []},
}

# ``x`` 2 and ``y`` 2 cannot both be had; ``r`` declares ``y`` first.
SAME_DEPTH = {
    "r": {"1": ["y", "x"]},
    "x": {"2": ["y<2"], "1": []},
    "y": {"2": [], "1": []},
}


def test_checker_finds_nothing_in_a_best_answer(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, TWO_ROOTS)
    roots = ["a", "b"]

    report = check_best(
        wheelhouse_resolver(wheelhouse, roots), roots, {"a": "2", "b": "1"}
    )

    assert report.best
    assert report.order == ["a", "b"]
    assert report.checked == ["a", "b"]


def test_checker_finds_the_answer_that_beats_a_worse_one(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, TWO_ROOTS)
    roots = ["a", "b"]

    # Valid, but ``a`` was named first and could have had its newest release.
    report = check_best(
        wheelhouse_resolver(wheelhouse, roots), roots, {"a": "1", "b": "2"}
    )

    assert not report.best
    (found,) = report.counterexamples
    assert (found.package, found.position) == ("a", 0)
    assert (found.version, found.better) == ("1", "2")
    assert found.witness == {"a": "2", "b": "1"}


def test_checker_finds_a_worse_dependency_behind_equal_roots(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, SAME_DEPTH)
    roots = ["r"]

    report = check_best(
        wheelhouse_resolver(wheelhouse, roots), roots, {"r": "1", "y": "1", "x": "2"}
    )

    (found,) = report.counterexamples
    assert (found.package, found.version, found.better) == ("y", "1", "2")
    assert found.witness == {"r": "1", "y": "2", "x": "1"}


def test_checker_respects_its_limit(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, SAME_DEPTH)
    roots = ["r"]

    report = check_best(
        wheelhouse_resolver(wheelhouse, roots),
        roots,
        {"r": "1", "y": "1", "x": "2"},
        limit=1,
    )

    assert report.checked == ["r"]
    assert report.best


def test_checker_counts_dependencies_past_the_roots(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, SAME_DEPTH)
    roots = ["r"]

    report = check_best(
        wheelhouse_resolver(wheelhouse, roots),
        roots,
        {"r": "1", "y": "2", "x": "1"},
        dependencies=1,
    )

    assert report.checked == ["r", "y"]


def test_checker_refuses_an_answer_that_is_not_a_solution(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, TWO_ROOTS)
    roots = ["a", "b"]

    with pytest.raises(InvalidAnswer):
        check_best(wheelhouse_resolver(wheelhouse, roots), roots, {"a": "2", "b": "2"})
