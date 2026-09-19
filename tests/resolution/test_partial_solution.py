"""Backtracking must restore exactly the state a full replay would produce.

``PartialSolution`` maintains ``_positive_ranges``, ``_negative_ranges``,
``_decided_versions`` and ``_undecided`` incrementally, and ``backtrack``
rebuilds them from the ``cum_*`` snapshot carried by the surviving top entry
of each package's trail.  That is a local patch to a vendored package (see
``src/kpip/_vendor/VENDORED.md``) replacing a rescan of the whole trail, so
these tests pin the equivalence rather than the implementation: they compare
the incremental state against a replay of the surviving assignments after
randomized decide/derive/backtrack sequences.
"""

from __future__ import annotations

import random
from typing import Any, cast

import pytest
from kpip._vendor.nab_resolver import partial_solution
from kpip._vendor.nab_resolver.partial_solution import (
    Assignment,
    PartialSolution,
)
from kpip._vendor.nab_resolver.ranges import Range
from kpip._vendor.nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    Term,
)

_HAS_PARTIAL_SOLUTION_FAST_PATHS = "_effective" in Assignment.__slots__

PACKAGES = ("alpha", "beta", "gamma", "delta")
VERSIONS = tuple(range(1, 9))

CAUSE: Incompatibility[str, int] = Incompatibility(
    [],
    IncompatibilityCause.DEPENDENCY,
)

State = dict[str, tuple[object, object, object, bool]]


def replay(solution: PartialSolution[str, int]) -> State:
    """Rebuild every package's state from its surviving trail.

    The definition the ``cum_*`` snapshots replaced: walk the package's
    assignments in order and keep the latest of each kind.
    """
    internals = cast("Any", solution)
    expected: State = {}
    for package, entries in internals._assignments_by_package.items():
        if not entries:
            continue
        positive: object = None
        negative: object = None
        decided: object = None
        for assignment in entries:
            if assignment.is_decision:
                positive = assignment.accumulated_range
                decided = assignment.version
            elif assignment.positive:
                positive = assignment.accumulated_range
            else:
                negative = assignment.accumulated_range
        expected[package] = (
            positive,
            negative,
            decided,
            positive is not None and decided is None,
        )
    return expected


def observed(solution: PartialSolution[str, int]) -> State:
    """Read the same state back out of the incrementally maintained maps."""
    internals = cast("Any", solution)
    packages = (
        set(internals._positive_ranges)
        | set(internals._negative_ranges)
        | set(internals._decided_versions)
        | set(internals._undecided)
    )
    return {
        package: (
            internals._positive_ranges.get(package),
            internals._negative_ranges.get(package),
            internals._decided_versions.get(package),
            package in internals._undecided,
        )
        for package in packages
    }


def assert_consistent(solution: PartialSolution[str, int]) -> None:
    assert observed(solution) == replay(solution)

    internals = cast("Any", solution)
    indexed = [
        assignment
        for entries in internals._assignments_by_package.values()
        for assignment in entries
    ]
    assert sorted(id(entry) for entry in indexed) == sorted(
        id(entry) for entry in internals._assignments
    )
    for assignment in internals._assignments:
        assert assignment.decision_level <= solution.decision_level


def drive(seed: int, steps: int = 200) -> None:
    """Run a random decide/derive/backtrack sequence, checking as it goes."""
    rng = random.Random(seed)
    solution: PartialSolution[str, int] = PartialSolution()

    for _ in range(steps):
        package = rng.choice(PACKAGES)
        action = rng.random()

        if action < 0.35:
            undecided = solution.undecided_packages()
            target = rng.choice(sorted(undecided)) if undecided else package
            if target not in solution.decisions():
                solution.decide(target, rng.choice(VERSIONS))
        elif action < 0.85:
            version = rng.choice(VERSIONS)
            positive = rng.random() < 0.5
            constraint = (
                Range.at_least(version)
                if rng.random() < 0.5
                else Range.less_than(version)
            )
            solution.derive(package, constraint, positive=positive, cause=CAUSE)
        elif solution.decision_level > 0:
            solution.backtrack(rng.randrange(solution.decision_level))

        assert_consistent(solution)


@pytest.mark.parametrize("seed", range(24))
def test_backtrack_matches_a_full_replay(seed: int) -> None:
    drive(seed)


def test_backtrack_restores_an_earlier_decision() -> None:
    """A decision below the target level survives, along with its version.

    The case the snapshot has to get right: ``alpha`` keeps a decision made
    before the target level even though a later derivation of its own is
    popped.  Losing it would leave ``alpha`` undecided and let the resolver
    re-decide a version it had already committed to.
    """
    solution: PartialSolution[str, int] = PartialSolution()

    solution.decide("alpha", 3)
    solution.derive("alpha", Range.at_least(1), positive=True, cause=CAUSE)
    level_after_alpha = solution.decision_level
    solution.decide("beta", 5)
    solution.derive("alpha", Range.less_than(9), positive=True, cause=CAUSE)

    solution.backtrack(level_after_alpha)

    assert solution.decisions() == {"alpha": 3}
    assert "alpha" not in solution.undecided_packages()
    assert_consistent(solution)


def test_backtrack_to_zero_clears_every_package() -> None:
    solution: PartialSolution[str, int] = PartialSolution()

    solution.decide("alpha", 3)
    solution.derive("beta", Range.at_least(2), positive=True, cause=CAUSE)
    solution.decide("beta", 4)
    solution.derive("gamma", Range.less_than(7), positive=False, cause=CAUSE)

    solution.backtrack(0)

    assert solution.decisions() == {}
    assert solution.undecided_packages() == set()
    assert solution.trail_length == 0
    assert_consistent(solution)


def test_snapshot_truthiness_stays_pinned() -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    empty_decisions = solution.decisions()

    solution.decide("alpha", 3)
    active_decisions = solution.decisions()
    solution.backtrack(0)

    assert not empty_decisions
    assert active_decisions
    assert not solution.decisions()


def test_first_derivations_retain_their_range_objects() -> None:
    """The first range of either sign needs no identity fold or memo entry."""
    solution: PartialSolution[str, int] = PartialSolution()
    allowed = Range.at_least(1)
    excluded = Range.singleton(4)

    solution.derive("allowed", allowed, positive=True, cause=CAUSE)
    solution.derive("excluded", excluded, positive=False, cause=CAUSE)

    internals = cast("Any", solution)
    assert solution.positive_range("allowed") is allowed
    assert internals._negative_ranges["excluded"] is excluded
    assert internals._range_ops == {}


@pytest.mark.skipif(
    not _HAS_PARTIAL_SOLUTION_FAST_PATHS,
    reason="requires kpip's partial-solution optimization patch",
)
def test_derive_returns_the_cached_effective_range() -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    allowed = Range.at_least(1)
    excluded = Range.singleton(4)

    positive = solution.derive("positive", allowed, positive=True, cause=CAUSE)
    negative = solution.derive("negative", excluded, positive=False, cause=CAUSE)
    solution.derive("mixed", allowed, positive=True, cause=CAUSE)
    mixed = solution.derive("mixed", excluded, positive=False, cause=CAUSE)

    assert positive is solution.get("positive")
    assert positive is allowed
    assert negative is solution.get("negative")
    assert 3 in negative
    assert 4 not in negative
    assert mixed is solution.get("mixed")
    assert 3 in mixed
    assert 4 not in mixed

    epoch_before = solution.contradiction_epoch
    empty = solution.derive("empty", Range.empty(), positive=True, cause=CAUSE)

    assert empty is solution.get("empty")
    assert empty.is_empty
    assert solution.contradiction_epoch == epoch_before + 1
    assert solution.trail_length == 5
    empty_assignment = cast(
        "Assignment[str, int]", solution.assignments_for("empty")[-1]
    )
    assert empty_assignment.trail_index == solution.trail_length - 1
    assert cast("Any", solution)._assignments[-1] is empty_assignment


@pytest.mark.skipif(
    not _HAS_PARTIAL_SOLUTION_FAST_PATHS,
    reason="requires kpip's partial-solution optimization patch",
)
def test_decide_returns_exact_range_after_recording_trails() -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    excluded = Range.singleton(2)
    solution.derive("package", excluded, positive=False, cause=CAUSE)

    exact = solution.decide("package", 2)

    assignment = cast("Assignment[str, int]", solution.assignments_for("package")[-1])
    internals = cast("Any", solution)
    assert exact is assignment.accumulated_range
    assert exact is assignment.cum_positive
    assert exact is solution.positive_range("package")
    assert internals._assignments[-1] is assignment
    assert internals._assignments_by_package["package"][-1] is assignment

    effective = solution.get("package")
    assert effective is not exact
    assert effective is not None
    assert effective.is_empty


def test_replayed_derivations_reuse_intersection_and_union_results() -> None:
    """A backtrack replay gets the same memoized range objects for both signs."""
    solution: PartialSolution[str, int] = PartialSolution()
    lower = Range.at_least(1)
    upper = Range.at_most(9)
    first_exclusion = Range.singleton(4)
    second_exclusion = Range.singleton(7)

    solution.derive("positive", lower, positive=True, cause=CAUSE)
    solution.derive("negative", first_exclusion, positive=False, cause=CAUSE)
    solution.decide("gate", 1)
    solution.derive("positive", upper, positive=True, cause=CAUSE)
    solution.derive("negative", second_exclusion, positive=False, cause=CAUSE)

    internals = cast("Any", solution)
    first_intersection = solution.positive_range("positive")
    first_union = internals._negative_ranges["negative"]

    solution.backtrack(0)
    solution.derive("positive", upper, positive=True, cause=CAUSE)
    solution.derive("negative", second_exclusion, positive=False, cause=CAUSE)

    assert solution.positive_range("positive") is first_intersection
    assert internals._negative_ranges["negative"] is first_union


def test_range_operation_memo_keeps_its_operands_alive() -> None:
    """Identity keys remain valid because the memo retains both operands."""
    solution: PartialSolution[str, int] = PartialSolution()
    current = Range.at_least(1)
    constraint = Range.at_most(9)

    solution.derive("package", current, positive=True, cause=CAUSE)
    solution.derive("package", constraint, positive=True, cause=CAUSE)

    internals = cast("Any", solution)
    assert internals._range_op_operands[-2] is current
    assert internals._range_op_operands[-1] is constraint


def test_range_operation_memo_cap_clears_operands_with_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing at the cap drops stale identity keys and their strong refs."""
    monkeypatch.setattr(partial_solution, "RANGE_OP_MEMO_MAX", 1)
    solution: PartialSolution[str, int] = PartialSolution()

    solution.derive("package", Range.at_least(1), positive=True, cause=CAUSE)
    solution.derive("package", Range.at_most(9), positive=True, cause=CAUSE)
    solution.derive("package", Range.at_most(8), positive=True, cause=CAUSE)

    internals = cast("Any", solution)
    assert len(internals._range_ops) == 1
    assert len(internals._range_op_operands) == 2
    effective = solution.get("package")
    assert effective is not None
    assert 5 in effective
    assert 9 not in effective


@pytest.mark.skipif(
    not _HAS_PARTIAL_SOLUTION_FAST_PATHS,
    reason="requires kpip's partial-solution optimization patch",
)
def test_subtraction_and_assignment_effective_ranges_are_reused() -> None:
    """Both the solution memo and the trail-entry cache reuse subtraction."""
    solution: PartialSolution[str, int] = PartialSolution()
    allowed = Range.at_least(1)
    excluded = Range.singleton(4)

    solution.derive("mixed", allowed, positive=True, cause=CAUSE)
    solution.derive("mixed", excluded, positive=False, cause=CAUSE)

    internals = cast("Any", solution)
    first_effective = solution.get("mixed")
    internals._effective_range_cache.pop("mixed")
    assert solution.get("mixed") is first_effective

    assignment = cast("Assignment[str, int]", solution.assignments_for("mixed")[-1])
    term = Term("mixed", allowed)
    assert assignment._effective is None
    assert solution.satisfier(term) is not None
    assignment_effective = assignment._effective
    assert assignment_effective is not None
    assert solution.satisfier(term) is not None
    assert assignment._effective is assignment_effective
