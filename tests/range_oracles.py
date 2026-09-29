"""Reference definitions ``Range``'s fast paths must agree with.

Shared by the seeded tests in ``tests/resolution/test_ranges.py`` and the
property tests in ``tests/properties/test_range_algebra.py``.
"""

from __future__ import annotations

from kpip._vendor.nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Range,
)


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


def build(intervals: list[tuple]) -> Range:
    """Normalize intervals the way the resolver does, through union."""

    result: Range = Range.empty()
    for interval in intervals:
        result = result | Range((interval,))
    return result
