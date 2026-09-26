"""``Range`` operations must agree with the sets they denote.

The seeded tests in ``tests/resolution/test_ranges.py`` sample the same
equivalences; here Hypothesis -- or CrossHair, under the ``crosshair``
profile -- chooses the ranges, so bound collisions and inclusive/exclusive
endpoint pairs are sought out rather than hoped for. Bounds are small ints,
so every comparison stays symbolic under CrossHair; a probe halfway between
two ints falls strictly inside or outside every interval.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from kpip._vendor.nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Range,
)

from tests.range_oracles import (
    build,
    contains_by_linear_scan,
    disjoint_by_set_algebra,
    subset_by_set_algebra,
)

bounds = st.integers(min_value=0, max_value=8)


@st.composite
def intervals(draw: st.DrawFn) -> tuple:
    lower, upper = sorted(draw(st.tuples(bounds, bounds)))
    shape = draw(st.sampled_from(("bounded", "point", "below", "above")))
    if shape == "point" or lower == upper:
        return (lower, True, lower, True)
    if shape == "below":
        return (NEGATIVE_INFINITY, False, upper, draw(st.booleans()))
    if shape == "above":
        return (lower, draw(st.booleans()), POSITIVE_INFINITY, False)
    return (lower, draw(st.booleans()), upper, draw(st.booleans()))


# Unions of a few intervals, plus point sets past the size at which ``Range``
# answers from a frozenset instead of walking intervals.
ranges = st.one_of(
    st.lists(intervals(), max_size=3).map(build),
    st.sets(st.integers(min_value=0, max_value=40), max_size=24).map(
        Range.from_versions
    ),
)
probes = st.integers(min_value=-2, max_value=42).map(lambda value: value / 2)


@given(ranges, probes)
def test_contains_matches_a_linear_scan(candidate: Range, probe: float) -> None:
    assert (probe in candidate) == contains_by_linear_scan(candidate, probe)


@given(ranges, ranges, probes)
def test_operators_act_on_members(left: Range, right: Range, probe: float) -> None:
    in_left, in_right = probe in left, probe in right

    assert (probe in left & right) == (in_left and in_right)
    assert (probe in left | right) == (in_left or in_right)
    assert (probe in left - right) == (in_left and not in_right)
    assert (probe in ~left) == (not in_left)


@given(ranges, ranges)
def test_results_are_canonical(left: Range, right: Range) -> None:
    """Equal sets must be equal interval lists: ``==`` and hashing say so."""
    assert (left - right)._intervals == (left & ~right)._intervals
    assert ~(left | right) == ~left & ~right
    assert ~(left & right) == ~left | ~right
    assert ~~left == left


@given(ranges, ranges)
def test_predicates_match_set_algebra(left: Range, right: Range) -> None:
    assert left.is_subset(right) == subset_by_set_algebra(left, right)
    assert left.is_disjoint(right) == disjoint_by_set_algebra(left, right)

    relation = left.relation(right)
    assert relation.is_subset == left.is_subset(right)
    assert relation.is_disjoint == left.is_disjoint(right)


@given(ranges, st.sets(st.integers(min_value=-1, max_value=41)))
def test_select_sorted_filters_in_order(candidate: Range, values: set[int]) -> None:
    ordered = sorted(values)
    assert candidate.select_sorted(ordered) == [
        value for value in ordered if value in candidate
    ]
