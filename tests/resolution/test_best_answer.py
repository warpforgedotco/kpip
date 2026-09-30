"""The resolver's answer is the best one: no valid answer beats it.

Each case is a wheelhouse where two packages cannot both have their newest
release, and says which of them the breadth-first order has give way.  The
answer is compared with the expected pins and then handed to the checker,
which looks for any valid answer that beats it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpip.resolution.api import ResolutionEngine
from tests.resolution.best_answer import (
    build_wheelhouse,
    check_best,
    pins_of,
    solution_order,
    wheelhouse_resolver,
)

Wheels = dict[str, dict[str, list[str]]]


TWO_ROOTS: Wheels = {
    "a": {"2": ["b<2"], "1": []},
    "b": {"2": [], "1": []},
}

# ``b`` is named, ``x`` is only a dependency of ``a``: ``x`` gives way.
ROOT_AND_TRANSITIVE: Wheels = {
    "a": {"1": ["x"]},
    "x": {"2": ["b<2"], "1": []},
    "b": {"2": [], "1": []},
}

# ``s`` is one step from the root and ``d`` two; ``d`` sorts first by name.
DIFFERENT_DEPTHS: Wheels = {
    "r": {"1": ["p", "s"]},
    "p": {"1": ["d"]},
    "d": {"2": ["s<2"], "1": []},
    "s": {"2": [], "1": []},
}

# ``r`` declares ``y`` before ``x``; ``x`` sorts first by name.
SAME_DEPTH: Wheels = {
    "r": {"1": ["y", "x"]},
    "x": {"2": ["y<2"], "1": []},
    "y": {"2": [], "1": []},
}

# ``zeta`` is declared first and wants the newer ``shared``; ``alpha`` sorts
# first by name and wants the older.
DIAMOND: Wheels = {
    "r": {"1": ["zeta", "alpha"]},
    "zeta": {"2": ["shared>=2"], "1": ["shared"]},
    "alpha": {"2": ["shared<2"], "1": ["shared"]},
    "shared": {"2": [], "1": []},
}

# ``a`` 3 fails four levels down, where ``d`` 3 asks for a ``k`` that does not
# exist, so the search has to come all the way back up to ``a``.  Under
# ``a`` 2, ``e`` is declared at the level above ``d`` and holds it down;
# ``d`` sorts first by name.
DEEP_BACKTRACK: Wheels = {
    "a": {"3": ["b>=3"], "2": ["b<3", "e"], "1": []},
    "b": {"3": ["c>=3"], "2": ["c<3"], "1": []},
    "c": {"3": ["d>=3"], "2": ["d<3"], "1": []},
    "d": {"3": ["k>=2"], "2": ["k"], "1": ["k"]},
    "e": {"3": ["d<2"], "2": [], "1": []},
    "k": {"1": []},
}

CASES = {
    "two-roots": (TWO_ROOTS, ["a", "b"], {"a": "2", "b": "1"}),
    "two-roots-reversed": (TWO_ROOTS, ["b", "a"], {"a": "1", "b": "2"}),
    "root-and-transitive": (
        ROOT_AND_TRANSITIVE,
        ["a", "b"],
        {"a": "1", "b": "2", "x": "1"},
    ),
    "different-depths": (
        DIFFERENT_DEPTHS,
        ["r"],
        {"r": "1", "p": "1", "s": "2", "d": "1"},
    ),
    "same-depth": (SAME_DEPTH, ["r"], {"r": "1", "y": "2", "x": "1"}),
    "diamond": (
        DIAMOND,
        ["r"],
        {"r": "1", "zeta": "2", "alpha": "1", "shared": "2"},
    ),
    "deep-backtrack": (
        DEEP_BACKTRACK,
        ["a", "k"],
        {"a": "2", "k": "1", "b": "2", "e": "3", "c": "2", "d": "1"},
    ),
}


# Dependencies are decided by conflict count, catalog size and name, not
# outward from the roots, so these come out with the wrong package held back.
NOT_BEST_YET = {"different-depths", "same-depth", "diamond", "deep-backtrack"}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            marks=pytest.mark.xfail(strict=True) if case in NOT_BEST_YET else (),
        )
        for case in CASES
    ],
)
def test_answer_is_best(tmp_path: Path, case: str) -> None:
    wheels, roots, expected = CASES[case]
    wheelhouse = build_wheelhouse(tmp_path, wheels)

    result = ResolutionEngine.resolve_wheelhouse([wheelhouse], roots)

    assert result is not None
    pins = pins_of(result)
    report = check_best(wheelhouse_resolver(wheelhouse, roots), roots, pins)
    assert report.counterexamples == []
    assert report.inconclusive == []
    assert report.checked == report.order
    assert pins == expected


def test_order_is_breadth_first_in_declared_order(tmp_path: Path) -> None:
    wheelhouse = build_wheelhouse(tmp_path, DEEP_BACKTRACK)

    result = ResolutionEngine.resolve_wheelhouse([wheelhouse], ["a", "k"])

    assert result is not None
    assert solution_order(["a", "k"], result) == ["a", "k", "b", "e", "c", "d"]
