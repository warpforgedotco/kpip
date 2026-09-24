"""``Range`` set predicates must agree with the set algebra they replace.

``is_subset`` and ``is_disjoint`` are hot enough during resolution that they
walk the interval lists directly instead of building a difference or an
intersection and asking whether it is empty.  That is a local patch to a
vendored package (see ``src/kpip/_vendor/VENDORED.md``), so these tests pin
the equivalence rather than the implementation: they compare each predicate
both against its original set-algebra definition and against an independent
membership oracle over sampled points.
"""

from __future__ import annotations

import inspect
import random

import pytest
from kpip._vendor.nab_resolver import ranges as ranges_module
from kpip._vendor.nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Range,
)

_HAS_EXTENDED_BETWEEN = "lower_inclusive" in inspect.signature(Range.between).parameters
_HAS_POINT_SET_CACHE = "_points" in Range.__slots__
_HAS_SAFE_INFINITY_COMPARISONS = hasattr(ranges_module, "_same_bound")

PROBES = [value * 0.5 for value in range(-2, 20)]


def subset_by_set_algebra(left: Range, right: Range) -> bool:
    """The definition the walk replaced."""

    return (left - right).is_empty


def disjoint_by_set_algebra(left: Range, right: Range) -> bool:
    """The definition the walk replaced."""

    return (left & right).is_empty


def contains_by_linear_scan(candidate: Range, version: object) -> bool:
    """The scan the binary search in ``__contains__`` replaced."""

    for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals:
        if lower is not NEGATIVE_INFINITY and (
            version < lower or (version == lower and not lower_inclusive)
        ):
            continue
        if upper is not POSITIVE_INFINITY and (
            version > upper or (version == upper and not upper_inclusive)
        ):
            continue
        return True
    return False


def members(candidate: Range) -> set[float]:
    return {probe for probe in PROBES if contains_by_linear_scan(candidate, probe)}


def build(intervals: list[tuple]) -> Range:
    """Normalize intervals the way the resolver does, through union."""

    result: Range = Range.empty()
    for interval in intervals:
        result = result | Range((interval,))
    return result


def random_range(rng: random.Random) -> Range:
    intervals = []
    for _ in range(rng.randint(0, 3)):
        lower, upper = sorted(rng.sample(range(9), 2))
        kind = rng.randint(0, 3)
        if kind == 0:
            intervals.append((lower, True, upper, True))
        elif kind == 1:
            intervals.append((lower, True, upper, False))
        elif kind == 2:
            intervals.append(
                (NEGATIVE_INFINITY, False, upper, rng.choice([True, False])),
            )
        else:
            intervals.append(
                (lower, rng.choice([True, False]), POSITIVE_INFINITY, False),
            )
    return build(intervals)


def point_range(values: set[int]) -> Range:
    return Range(tuple((value, True, value, True) for value in sorted(values)))


@pytest.mark.parametrize(
    "lower_inclusive, upper_inclusive",
    [(False, False), (False, True), (True, False), (True, True)],
)
@pytest.mark.skipif(
    not _HAS_EXTENDED_BETWEEN,
    reason="requires kpip's extended Range.between patch",
)
def test_between_preserves_bound_inclusivity(
    lower_inclusive: bool,
    upper_inclusive: bool,
) -> None:
    result = Range.between(
        1,
        2,
        lower_inclusive=lower_inclusive,
        upper_inclusive=upper_inclusive,
    )

    assert result._intervals == ((1, lower_inclusive, 2, upper_inclusive),)


@pytest.mark.parametrize(
    "lower, upper, lower_inclusive, upper_inclusive, expected",
    [
        (1, 1, True, True, Range.singleton(1)),
        (1, 1, True, False, Range.empty()),
        (1, 1, False, True, Range.empty()),
        (1, 1, False, False, Range.empty()),
        (2, 1, True, True, Range.empty()),
    ],
)
@pytest.mark.skipif(
    not _HAS_EXTENDED_BETWEEN,
    reason="requires kpip's extended Range.between patch",
)
def test_between_rejects_empty_bound_combinations(
    lower: int,
    upper: int,
    lower_inclusive: bool,
    upper_inclusive: bool,
    expected: Range[int],
) -> None:
    assert (
        Range.between(
            lower,
            upper,
            lower_inclusive=lower_inclusive,
            upper_inclusive=upper_inclusive,
        )
        == expected
    )


@pytest.mark.parametrize("seed", range(12))
def test_contains_matches_a_linear_scan(seed: int) -> None:
    rng = random.Random(seed)

    for _ in range(200):
        candidate = random_range(rng)
        for probe in PROBES:
            assert (probe in candidate) == contains_by_linear_scan(candidate, probe)


@pytest.mark.parametrize("seed", range(12))
def test_predicates_match_set_algebra_and_membership(seed: int) -> None:
    rng = random.Random(seed)

    for _ in range(400):
        left, right = random_range(rng), random_range(rng)
        left_members, right_members = members(left), members(right)

        assert left.is_subset(right) == subset_by_set_algebra(left, right)
        assert left.is_disjoint(right) == disjoint_by_set_algebra(left, right)
        assert left.is_disjoint(right) == (not left_members & right_members)

        relation = left.relation(right)
        assert relation.is_subset == left.is_subset(right)
        assert relation.is_disjoint == left.is_disjoint(right)


@pytest.mark.parametrize("seed", range(12))
def test_discrete_fast_paths_match_set_operations(seed: int) -> None:
    """Finite release domains retain the exact interval-set semantics."""
    rng = random.Random(seed + 200)

    for _ in range(30):
        left_values = set(rng.sample(range(256), rng.randint(0, 160)))
        right_values = set(rng.sample(range(256), rng.randint(0, 160)))
        left = point_range(left_values)
        right = point_range(right_values)

        assert members(left & right) == (left_values & right_values) & set(PROBES)
        assert members(left | right) == (left_values | right_values) & set(PROBES)
        assert members(left - right) == (left_values - right_values) & set(PROBES)
        assert left.is_subset(right) == (left_values <= right_values)
        assert left.is_disjoint(right) == left_values.isdisjoint(right_values)

        relation = left.relation(right)
        assert relation.is_subset == (left_values <= right_values)
        assert relation.is_disjoint == left_values.isdisjoint(right_values)
        for probe in rng.sample(range(256), 12):
            assert (probe in left) == (probe in left_values)

        for result in (left & right, left | right, left - right):
            assert result._intervals == tuple(
                (value, True, value, True)
                for value in sorted(probe for probe in range(256) if probe in result)
            )


def test_discrete_and_continuous_ranges_use_the_same_semantics() -> None:
    points = point_range({1, 3, 5, 7})
    span = Range.between(2, 6)

    assert members(points & span) == {3, 5}
    assert members(points | span) == members(points) | members(span)
    assert members(points - span) == {1, 7}
    assert not points.is_subset(span)
    assert not points.is_disjoint(span)


@pytest.mark.skipif(
    not _HAS_POINT_SET_CACHE,
    reason="requires kpip's discrete-range optimization patch",
)
def test_point_set_cache_is_reserved_for_large_discrete_ranges() -> None:
    small = point_range(set(range(15)))
    large = point_range(set(range(16)))
    small_cache = small._points

    assert 7 in small
    assert small.relation(Range.between(0, 20)).is_subset
    assert small._points is small_cache

    assert 7 in large
    assert isinstance(large._points, frozenset)


@pytest.mark.skipif(
    not _HAS_POINT_SET_CACHE,
    reason="requires kpip's discrete-range optimization patch",
)
def test_large_discrete_binary_operation_converts_both_operands() -> None:
    small = point_range({1, 3})
    large = point_range(set(range(0, 32, 2)))

    assert members(small & large) == set()
    assert isinstance(small._points, frozenset)
    assert isinstance(large._points, frozenset)


@pytest.mark.skipif(
    not _HAS_POINT_SET_CACHE,
    reason="requires kpip's discrete-range optimization patch",
)
def test_large_discrete_range_rebuilds_point_cache_after_pickle() -> None:
    import pickle

    original = Range.from_versions(range(32))
    restored = pickle.loads(pickle.dumps(original))

    assert restored == original
    assert not isinstance(restored._points, frozenset)
    assert 17 in restored
    assert restored._points == frozenset(range(32))


@pytest.mark.parametrize("size", [0, 1, 5, 40])
def test_from_versions_does_not_depend_on_order_or_repeats(size: int) -> None:
    """Ordered input skips the sort, and must build the same range."""
    ascending = list(range(0, size * 2, 2))
    shuffled = random.Random(size).sample(ascending, len(ascending))
    inputs = [
        ascending,
        tuple(ascending),
        ascending[::-1],
        shuffled,
        ascending + ascending[: size // 2],
        iter(shuffled),
    ]

    built = [Range.from_versions(versions) for versions in inputs]

    for result in built:
        assert result == built[0]
        assert result._as_points() == built[0]._as_points()
    assert list(built[0]._intervals) == [
        (version, True, version, True) for version in ascending
    ]


def test_empty_range_is_subset_and_disjoint() -> None:
    empty: Range = Range.empty()
    other = Range.between(1, 5)

    assert empty.is_subset(other)
    assert empty.is_disjoint(other)

    relation = empty.relation(other)
    assert relation.is_subset
    assert relation.is_disjoint


def test_nonempty_against_empty_is_disjoint_but_not_subset() -> None:
    populated = Range.between(1, 5)
    empty: Range = Range.empty()

    assert not populated.is_subset(empty)
    assert populated.is_disjoint(empty)


def test_touching_exclusive_bounds_do_not_overlap() -> None:
    lower = Range.less_than(3)
    upper = Range.at_least(3)

    assert lower.is_disjoint(upper)
    assert not lower.is_subset(upper)


def test_subset_requires_a_single_covering_interval() -> None:
    """A gap in the covering range makes the span a non-subset."""

    span = Range.between(1, 5)
    gapped = Range.between(1, 2) | Range.between(3, 5)

    assert not span.is_subset(gapped)
    assert Range.between(3, 5).is_subset(gapped)


def test_full_range_contains_everything() -> None:
    full: Range = Range.full()

    assert Range.between(1, 5).is_subset(full)
    assert not Range.between(1, 5).is_disjoint(full)
    assert full.is_subset(full)


def test_hash_is_stable_and_matches_equality() -> None:
    left = Range.between(1, 5) | Range.at_least(9)
    right = Range.between(1, 5) | Range.at_least(9)

    assert left == right
    assert hash(left) == hash(right)
    assert hash(left) == hash(left)


@pytest.mark.parametrize("seed", range(12))
def test_difference_matches_complement_and_intersect(seed: int) -> None:
    """``a - b`` carves intervals directly; it must equal ``a & ~b``."""

    rng = random.Random(seed + 100)

    for _ in range(300):
        left, right = random_range(rng), random_range(rng)

        assert (left - right)._intervals == (left & ~right)._intervals
        assert members(left - right) == members(left) - members(right)


class Strict:
    """An integer-like bound that refuses to compare with anything else.

    ``Version`` compares only with ``Version``; a range operation that lets
    an infinity sentinel reach a bound comparison would raise here instead
    of quietly succeeding through the sentinel's reflected methods.
    """

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value

    def _other(self, other: object) -> int:
        assert isinstance(other, Strict), f"bound compared with {other!r}"
        return other.value

    def __eq__(self, other: object) -> bool:
        return self.value == self._other(other)

    def __hash__(self) -> int:
        return hash(self.value)

    def __lt__(self, other: object) -> bool:
        return self.value < self._other(other)

    def __le__(self, other: object) -> bool:
        return self.value <= self._other(other)

    def __gt__(self, other: object) -> bool:
        return self.value > self._other(other)

    def __ge__(self, other: object) -> bool:
        return self.value >= self._other(other)

    def __repr__(self) -> str:
        return f"Strict({self.value})"


def strict(candidate: Range) -> Range:
    return Range(
        tuple(
            (
                lower if lower is NEGATIVE_INFINITY else Strict(lower),
                lower_inclusive,
                upper if upper is POSITIVE_INFINITY else Strict(upper),
                upper_inclusive,
            )
            for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals
        ),
    )


def plain(candidate: Range) -> Range:
    return Range(
        tuple(
            (
                lower if lower is NEGATIVE_INFINITY else lower.value,
                lower_inclusive,
                upper if upper is POSITIVE_INFINITY else upper.value,
                upper_inclusive,
            )
            for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals
        ),
    )


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.skipif(
    not _HAS_SAFE_INFINITY_COMPARISONS,
    reason="requires kpip's infinity-bound compatibility patch",
)
def test_bounds_are_never_compared_with_a_sentinel(seed: int) -> None:
    """The interval algebra over bounds that refuse foreign operands gives
    the same answers as over plain integers."""
    rng = random.Random(seed)

    for _ in range(300):
        left, right = random_range(rng), random_range(rng)
        strict_left, strict_right = strict(left), strict(right)

        assert plain(strict_left & strict_right) == (left & right)
        assert plain(strict_left | strict_right) == (left | right)
        assert plain(strict_left - strict_right) == (left - right)
        assert plain(~strict_left) == ~left
        assert strict_left.is_subset(strict_right) == left.is_subset(right)
        assert strict_left.is_disjoint(strict_right) == left.is_disjoint(right)
        assert strict_left.relation(strict_right) == left.relation(right)
        for probe in range(-1, 10):
            assert (Strict(probe) in strict_left) == (probe in left)


@pytest.mark.parametrize("seed", range(10))
def test_select_sorted_matches_membership(seed: int) -> None:
    """Bisected slices hold exactly the members a scan would find."""
    rng = random.Random(seed)
    values = sorted({rng.randint(0, 60) for _ in range(40)})
    # A catalog repeats a release it lists both yanked and not.
    ordered = sorted(values + rng.sample(values, 5))
    candidates = [
        Range.at_least(rng.choice(values)),
        Range.less_than(rng.choice(values)),
        Range.between(rng.choice(values), rng.choice(values)),
        Range.from_versions(rng.sample(values, 12)),
        Range.full(),
        Range.empty(),
    ]
    for _ in range(20):
        left, right = rng.sample(candidates, 2)
        candidates.append(left | right if rng.random() < 0.5 else left & ~right)

    for version_range in candidates:
        assert version_range.select_sorted(ordered) == [
            value for value in ordered if value in version_range
        ]
