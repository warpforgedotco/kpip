"""Generic version range with interval operations.

A Range represents a set of versions as a canonical list of intervals; see
``Range.__init__`` for what makes a list canonical. Supports intersection,
union, complement, and containment.

This is equivalent to pubgrub-rs's ``version_ranges::Ranges<V>``:
https://github.com/pubgrub-rs/pubgrub/tree/release/version-ranges

The type parameter V can be any ordered, hashable type. The simple test
provider uses int; the Python provider uses packaging.version.Version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeAlias, cast

from ._compat import override
from .types import RangeRelation, VersionType

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# Bound once so the hot return paths load a module global instead of a class
# attribute.
_EMPTY_REL = RangeRelation.EMPTY
_SUBSET_REL = RangeRelation.SUBSET
_DISJOINT_REL = RangeRelation.DISJOINT
_OVERLAPPING_REL = RangeRelation.OVERLAPPING
_POINTS_UNSET = object()
_POINT_SET_MIN_INTERVALS = 16

__all__ = [
    "NEGATIVE_INFINITY",
    "POSITIVE_INFINITY",
    "Bound",
    "Interval",
    "Range",
]


class _NegativeInfinity:
    """Sentinel that sorts before every version.

    Used as the lower bound of unbounded intervals like ``(-inf, 5)``.
    Use the module-level ``NEGATIVE_INFINITY`` constant, not this class.
    """

    def __lt__(self, other: object) -> bool:
        return not isinstance(other, _NegativeInfinity)

    def __le__(self, other: object) -> bool:
        return True

    def __gt__(self, other: object) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return isinstance(other, _NegativeInfinity)

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        return isinstance(other, _NegativeInfinity)

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash("_NegativeInfinity")

    @override
    def __repr__(self) -> str:
        return "-inf"


class _PositiveInfinity:
    """Sentinel that sorts after every version.

    Used as the upper bound of unbounded intervals like ``[5, +inf)``.
    Use the module-level ``POSITIVE_INFINITY`` constant, not this class.
    """

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return isinstance(other, _PositiveInfinity)

    def __gt__(self, other: object) -> bool:
        return not isinstance(other, _PositiveInfinity)

    def __ge__(self, other: object) -> bool:
        return True

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        return isinstance(other, _PositiveInfinity)

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash("_PositiveInfinity")

    @override
    def __repr__(self) -> str:
        return "+inf"


NEGATIVE_INFINITY = _NegativeInfinity()
POSITIVE_INFINITY = _PositiveInfinity()

# An interval is (lower, lower_inclusive, upper, upper_inclusive)
# where lower/upper can be NEGATIVE_INFINITY/POSITIVE_INFINITY for unbounded.
Bound: TypeAlias = Any  # V | _NegativeInfinity | _PositiveInfinity
Interval: TypeAlias = tuple[Bound, bool, Bound, bool]


def _same_bound(left: Bound, right: Bound) -> bool:
    """Compare bounds without asking a version to compare with infinity."""
    if left is right:
        return True
    if (
        left is NEGATIVE_INFINITY
        or left is POSITIVE_INFINITY
        or right is NEGATIVE_INFINITY
        or right is POSITIVE_INFINITY
    ):
        return False
    return bool(left == right)


def _max_lower_bound(left: Interval, right: Interval) -> tuple[Bound, bool]:
    """Return the higher of two lower bounds (for intersection)."""
    left_lower, left_lower_inc = left[0], left[1]
    right_lower, right_lower_inc = right[0], right[1]
    if _same_bound(left_lower, right_lower):
        return left_lower, left_lower_inc and right_lower_inc
    if left_lower is NEGATIVE_INFINITY or (
        right_lower is not NEGATIVE_INFINITY and left_lower < right_lower
    ):
        return right_lower, right_lower_inc
    return left_lower, left_lower_inc


def _min_upper_bound(left: Interval, right: Interval) -> tuple[Bound, bool]:
    """Return the lower of two upper bounds (for intersection)."""
    left_upper, left_upper_inc = left[2], left[3]
    right_upper, right_upper_inc = right[2], right[3]
    if _same_bound(left_upper, right_upper):
        return left_upper, left_upper_inc and right_upper_inc
    if left_upper is POSITIVE_INFINITY or (
        right_upper is not POSITIVE_INFINITY and left_upper > right_upper
    ):
        return right_upper, right_upper_inc
    return left_upper, left_upper_inc


def _ends_before(interval: Interval, other: Interval) -> bool:
    """Return whether ``interval`` finishes below everything in ``other``."""
    upper, upper_inclusive = interval[2], interval[3]
    lower, lower_inclusive = other[0], other[1]
    if upper is POSITIVE_INFINITY or lower is NEGATIVE_INFINITY:
        return False
    if upper == lower:
        # They meet at a point, which they share only if both ends include it.
        return not (upper_inclusive and lower_inclusive)
    return bool(upper < lower)


def _advance_left(left: Interval, right: Interval) -> bool:
    """Return whether a walk over two interval lists should step ``left``.

    Whichever interval ends first cannot meet anything further along the other
    list, so it is the one to retire.
    """
    left_upper = left[2]
    right_upper = right[2]
    if left_upper is POSITIVE_INFINITY:
        return right_upper is POSITIVE_INFINITY
    if right_upper is POSITIVE_INFINITY:
        return True
    return bool(left_upper <= right_upper)


def _interval_is_empty(
    lower: Bound,
    *,
    lower_inclusive: bool,
    upper: Bound,
    upper_inclusive: bool,
) -> bool:
    """Return True if the interval contains no versions."""
    if lower is NEGATIVE_INFINITY or upper is POSITIVE_INFINITY:
        return False
    if lower > upper:
        return True
    return lower == upper and not (lower_inclusive and upper_inclusive)


class Range(Generic[VersionType]):
    """A set of versions represented as a canonical list of intervals.

    Modeled after pubgrub-rs ``version_ranges::Ranges<V>``:
    https://docs.rs/version-ranges/latest/version_ranges/struct.Ranges.html

    Each interval is ``(lower, lower_inclusive, upper, upper_inclusive)``.
    See :meth:`__init__` for the invariant the interval list must satisfy.
    """

    __slots__ = ("_hash", "_intervals", "_points")

    def __init__(self, intervals: tuple[Interval, ...] = ()) -> None:
        """Create a range from intervals that already satisfy the invariant.

        The intervals must be sorted by lower bound, must not overlap or
        touch, must each hold at least one version, and must be exclusive at
        ``NEGATIVE_INFINITY`` and ``POSITIVE_INFINITY``.  Every operator
        returns through here, so this neither checks nor normalizes: the
        classmethods and the operators maintain the invariant, and a caller
        that assembles its own tuple owns it.

        Equality and hashing compare interval tuples, so two lists denoting
        the same set must be the same list, and
        ``((1, True, 2, False), (2, True, 3, True))`` is not a legal way to
        write ``[1, 3]``.
        """
        self._intervals = intervals
        self._hash = 0
        self._points: frozenset[VersionType] | None | object = _POINTS_UNSET

    def _as_points(self) -> frozenset[VersionType] | None:
        """Return members for a discrete singleton range, otherwise ``None``."""
        cached = self._points
        if cached is None or isinstance(cached, frozenset):
            return cached

        values: list[VersionType] = []
        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            if (
                lower is NEGATIVE_INFINITY
                or upper is POSITIVE_INFINITY
                or not lower_inclusive
                or not upper_inclusive
                or not _same_bound(lower, upper)
            ):
                self._points = None
                return None
            values.append(lower)

        points = frozenset(values)
        self._points = points
        return points

    @classmethod
    def _from_points(cls, points: frozenset[VersionType]) -> Range[VersionType]:
        result = cls(
            tuple((version, True, version, True) for version in sorted(points))
        )
        result._points = points
        return result

    @classmethod
    def empty(cls) -> Range[VersionType]:
        """Create a range containing no versions."""
        return cls(())

    @classmethod
    def full(cls) -> Range[VersionType]:
        """Create a range containing all versions.

        Mirrors :meth:`packaging.ranges.VersionRange.full`.
        """
        return cls(((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),))

    @classmethod
    def singleton(cls, version: VersionType) -> Range[VersionType]:
        """Create a range containing exactly one version.

        Mirrors :meth:`packaging.ranges.VersionRange.singleton`.
        For a set of versions use :meth:`from_versions`.
        """
        return cls(((version, True, version, True),))

    @classmethod
    def from_versions(cls, versions: Iterable[VersionType]) -> Range[VersionType]:
        """Create a range holding exactly the given versions.

        The iterable is consumed once and equal versions collapse.
        Distinct versions never merge: a range has no notion of one
        version following another, so the result still excludes
        everything strictly between them.

        Cheaper than folding :meth:`singleton` with ``|``.
        """
        values: Sequence[Any] = (
            versions if isinstance(versions, (list, tuple)) else list(versions)
        )
        # Callers nearly always pass distinct versions in ascending order,
        # e.g. a slice of a sorted catalog.  Confirming that costs one
        # comparison per version, where sorting the arbitrary order of a set
        # of them costs a full sort.
        ordered = True
        for index in range(1, len(values)):
            if not values[index - 1] < values[index]:
                ordered = False
                break
        if ordered:
            result = cls(tuple([(version, True, version, True) for version in values]))
            if len(values) >= _POINT_SET_MIN_INTERVALS:
                result._points = frozenset(values)
            return result

        # sorted() needs an ordering bound, which VersionType does not declare.
        points = frozenset(cast("Iterable[Any]", values))
        result = cls(
            tuple((version, True, version, True) for version in sorted(points))
        )
        if len(points) >= _POINT_SET_MIN_INTERVALS:
            result._points = points
        return result

    @classmethod
    def at_least(cls, version: VersionType) -> Range[VersionType]:
        """Create ``[version, +inf)``."""
        return cls(((version, True, POSITIVE_INFINITY, False),))

    @classmethod
    def greater_than(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(version, +inf)``."""
        return cls(((version, False, POSITIVE_INFINITY, False),))

    @classmethod
    def at_most(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(-inf, version]``."""
        return cls(((NEGATIVE_INFINITY, False, version, True),))

    @classmethod
    def less_than(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(-inf, version)``."""
        return cls(((NEGATIVE_INFINITY, False, version, False),))

    @classmethod
    def between(
        cls,
        lower: VersionType,
        upper: VersionType,
        *,
        lower_inclusive: bool = True,
        upper_inclusive: bool = False,
    ) -> Range[VersionType]:
        """Create one bounded interval, or empty when its bounds exclude all."""
        if _interval_is_empty(
            lower,
            lower_inclusive=lower_inclusive,
            upper=upper,
            upper_inclusive=upper_inclusive,
        ):
            return cls(())
        return cls(((lower, lower_inclusive, upper, upper_inclusive),))

    @property
    def is_empty(self) -> bool:
        """``True`` if this range contains no versions."""
        return len(self._intervals) == 0

    def __contains__(self, version: object) -> bool:
        """Test membership by point lookup or binary-searching intervals."""
        intervals = self._intervals
        interval_count = len(intervals)
        if interval_count == 0:
            return False
        if interval_count >= _POINT_SET_MIN_INTERVALS:
            points = self._as_points()
            if points is not None:
                return version in points

        if interval_count == 1:
            lower, lower_inclusive, upper, upper_inclusive = intervals[0]
            if lower is not NEGATIVE_INFINITY:
                if version < lower:
                    return False
                if not lower_inclusive and _same_bound(version, lower):
                    return False
        else:
            low = 0
            high = interval_count
            while low < high:
                middle = (low + high) // 2
                lower = intervals[middle][0]
                if lower is NEGATIVE_INFINITY or not version < lower:
                    low = middle + 1
                else:
                    high = middle
            if low == 0:
                return False
            lower, lower_inclusive, upper, upper_inclusive = intervals[low - 1]
            if lower is not NEGATIVE_INFINITY and _same_bound(version, lower):
                return lower_inclusive
        if upper is POSITIVE_INFINITY:
            return True
        return not (
            version > upper or (not upper_inclusive and _same_bound(version, upper))
        )

    def __and__(self, other: object) -> Range[VersionType]:
        """Compute the intersection of two ranges (versions in both)."""
        if not isinstance(other, Range):
            return NotImplemented
        if (
            len(self._intervals) >= _POINT_SET_MIN_INTERVALS
            or len(other._intervals) >= _POINT_SET_MIN_INTERVALS
        ):
            left_points = self._as_points()
            right_points = other._as_points()
            if left_points is not None and right_points is not None:
                return self._from_points(left_points & right_points)
        left_intervals = self._intervals
        right_intervals = other._intervals
        left_count = len(left_intervals)
        right_count = len(right_intervals)
        result: list[Interval] = []
        left_index = right_index = 0
        while left_index < left_count and right_index < right_count:
            left_interval = left_intervals[left_index]
            right_interval = right_intervals[right_index]

            inter_lower, inter_lower_inc = _max_lower_bound(
                left_interval, right_interval
            )
            inter_upper, inter_upper_inc = _min_upper_bound(
                left_interval, right_interval
            )

            # _interval_is_empty, written out.
            if not (
                inter_lower is not NEGATIVE_INFINITY
                and inter_upper is not POSITIVE_INFINITY
                and (
                    inter_lower > inter_upper
                    or (
                        inter_lower == inter_upper
                        and not (inter_lower_inc and inter_upper_inc)
                    )
                )
            ):
                result.append(
                    (inter_lower, inter_lower_inc, inter_upper, inter_upper_inc)
                )

            # Advance the side with the smaller upper bound
            left_upper = left_interval[2]
            right_upper = right_interval[2]
            if _same_bound(left_upper, right_upper):
                left_index += 1
                right_index += 1
            elif left_upper is POSITIVE_INFINITY or (
                right_upper is not POSITIVE_INFINITY and left_upper > right_upper
            ):
                right_index += 1
            else:
                left_index += 1

        return Range(tuple(result))

    def __or__(self, other: object) -> Range[VersionType]:
        """Union of two ranges (versions in either)."""
        if not isinstance(other, Range):
            return NotImplemented
        if (
            len(self._intervals) >= _POINT_SET_MIN_INTERVALS
            or len(other._intervals) >= _POINT_SET_MIN_INTERVALS
        ):
            left_points = self._as_points()
            right_points = other._as_points()
            if left_points is not None and right_points is not None:
                return self._from_points(left_points | right_points)
        left_intervals = self._intervals
        right_intervals = other._intervals
        # Union with an empty range is the other operand; both are immutable.
        if not left_intervals:
            return other
        if not right_intervals:
            return self
        return Range(_normalize_intervals([*left_intervals, *right_intervals]))

    def __invert__(self) -> Range[VersionType]:
        """Complement (versions NOT in this range)."""
        if self.is_empty:
            return Range.full()

        result: list[Interval] = []
        previous_upper: Bound = NEGATIVE_INFINITY
        previous_upper_inclusive = False

        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            # Gap between the previous interval's upper and this lower.
            if (
                previous_upper is not NEGATIVE_INFINITY
                or lower is not NEGATIVE_INFINITY
            ):
                gap_lower = previous_upper
                gap_lower_inclusive = (
                    not previous_upper_inclusive
                    and previous_upper is not NEGATIVE_INFINITY
                )
                gap_upper = lower
                gap_upper_inclusive = (
                    not lower_inclusive and lower is not POSITIVE_INFINITY
                )

                if (
                    gap_lower is NEGATIVE_INFINITY
                    or gap_upper is POSITIVE_INFINITY
                    or gap_lower < gap_upper
                    or (
                        gap_lower == gap_upper
                        and gap_lower_inclusive
                        and gap_upper_inclusive
                    )
                ):
                    result.append(
                        (gap_lower, gap_lower_inclusive, gap_upper, gap_upper_inclusive)
                    )

            previous_upper = upper
            previous_upper_inclusive = upper_inclusive

        # Trailing gap after the last interval.
        if previous_upper is not POSITIVE_INFINITY:
            result.append(
                (previous_upper, not previous_upper_inclusive, POSITIVE_INFINITY, False)
            )

        return Range(tuple(result))

    def __sub__(self, other: object) -> Range[VersionType]:
        """Set difference: versions in self but not in other.

        Carves each of this range's intervals against ``other``'s in a single
        walk of both lists, so the complement of ``other`` is never built.
        """
        if not isinstance(other, Range):
            return NotImplemented

        left_intervals = self._intervals
        right_intervals = other._intervals
        if (
            len(left_intervals) >= _POINT_SET_MIN_INTERVALS
            or len(right_intervals) >= _POINT_SET_MIN_INTERVALS
        ):
            left_points = self._as_points()
            right_points = other._as_points()
            if left_points is not None and right_points is not None:
                return self._from_points(left_points - right_points)

        right_count = len(right_intervals)
        result: list[Interval] = []
        right_index = 0

        # These comparisons stay inline: subtraction runs on every probe of
        # the conflict trail, where helper-call overhead is measurable.
        for left in left_intervals:
            lower, lower_inclusive, upper, upper_inclusive = left

            # A right interval below this one is below every later one too.
            while right_index < right_count:
                right = right_intervals[right_index]
                right_upper = right[2]
                if right_upper is POSITIVE_INFINITY or lower is NEGATIVE_INFINITY:
                    break
                if right_upper == lower:
                    if right[3] and lower_inclusive:
                        break
                elif not right_upper < lower:
                    break
                right_index += 1

            exhausted = False
            scan = right_index

            while scan < right_count:
                right = right_intervals[scan]
                right_lower, right_lower_inc, right_upper, right_upper_inc = right

                if (
                    upper is not POSITIVE_INFINITY
                    and right_lower is not NEGATIVE_INFINITY
                ):
                    if upper == right_lower:
                        if not (upper_inclusive and right_lower_inc):
                            break
                    elif upper < right_lower:
                        break

                # Whatever of the remainder sits below this right interval survives.
                if right_lower is not NEGATIVE_INFINITY and not (
                    lower is not NEGATIVE_INFINITY
                    and (
                        lower > right_lower
                        or (
                            lower == right_lower
                            and not (lower_inclusive and not right_lower_inc)
                        )
                    )
                ):
                    result.append(
                        (lower, lower_inclusive, right_lower, not right_lower_inc)
                    )

                if right_upper is POSITIVE_INFINITY:
                    exhausted = True
                    break

                # Resume above this right interval, stopping when nothing of
                # it is left.  _interval_is_empty, written out.
                lower, lower_inclusive = right_upper, not right_upper_inc
                if upper is not POSITIVE_INFINITY and (
                    lower > upper
                    or (lower == upper and not (lower_inclusive and upper_inclusive))
                ):
                    exhausted = True
                    break

                scan += 1

            if not exhausted and not (
                lower is not NEGATIVE_INFINITY
                and upper is not POSITIVE_INFINITY
                and (
                    lower > upper
                    or (lower == upper and not (lower_inclusive and upper_inclusive))
                )
            ):
                result.append((lower, lower_inclusive, upper, upper_inclusive))

        return Range(tuple(result))

    def is_subset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in self is also in other.

        Walks both interval lists once and stops at the first uncovered
        interval.  This leans on the invariant: consecutive intervals in
        ``other`` always leave a gap, so an interval of self is covered only
        when a single interval of other holds all of it.
        """
        left_intervals = self._intervals
        right_intervals = other._intervals
        if (
            len(left_intervals) >= _POINT_SET_MIN_INTERVALS
            or len(right_intervals) >= _POINT_SET_MIN_INTERVALS
        ):
            left_points = self._as_points()
            right_points = other._as_points()
            if left_points is not None and right_points is not None:
                return left_points <= right_points

        right_count = len(right_intervals)
        right_index = 0

        for left in left_intervals:
            left_lower = left[0]
            left_lower_inclusive = left[1]
            while right_index < right_count:
                right = right_intervals[right_index]
                right_upper = right[2]
                if right_upper is POSITIVE_INFINITY or left_lower is NEGATIVE_INFINITY:
                    break
                if right_upper == left_lower:
                    if right[3] and left_lower_inclusive:
                        break
                elif not right_upper < left_lower:
                    break
                right_index += 1

            if right_index >= right_count:
                return False

            right = right_intervals[right_index]
            # Coverage check, written out; see Range.relation for the
            # derivation from _max_lower_bound/_min_upper_bound.
            right_lower = right[0]
            if _same_bound(left_lower, right_lower):
                if left_lower_inclusive and not right[1]:
                    return False
            elif left_lower is NEGATIVE_INFINITY or (
                right_lower is not NEGATIVE_INFINITY and left_lower < right_lower
            ):
                return False
            left_upper = left[2]
            right_upper = right[2]
            if _same_bound(left_upper, right_upper):
                if left[3] and not right[3]:
                    return False
            elif left_upper is POSITIVE_INFINITY or (
                right_upper is not POSITIVE_INFINITY and left_upper > right_upper
            ):
                return False

        return True

    def is_superset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in other is also in self."""
        return other.is_subset(self)

    def is_disjoint(self, other: Range[VersionType]) -> bool:
        """Return whether self and other share no version.

        Stops at the first shared version rather than building the whole
        intersection.
        """
        if (
            len(self._intervals) >= _POINT_SET_MIN_INTERVALS
            or len(other._intervals) >= _POINT_SET_MIN_INTERVALS
        ):
            left_points = self._as_points()
            right_points = other._as_points()
            if left_points is not None and right_points is not None:
                return left_points.isdisjoint(right_points)

        left_intervals = self._intervals
        right_intervals = other._intervals
        left_count = len(left_intervals)
        right_count = len(right_intervals)
        left_index = right_index = 0

        while left_index < left_count and right_index < right_count:
            left = left_intervals[left_index]
            right = right_intervals[right_index]

            lower, lower_inclusive = _max_lower_bound(left, right)
            upper, upper_inclusive = _min_upper_bound(left, right)

            # _interval_is_empty, written out.
            if not (
                lower is not NEGATIVE_INFINITY
                and upper is not POSITIVE_INFINITY
                and (
                    lower > upper
                    or (lower == upper and not (lower_inclusive and upper_inclusive))
                )
            ):
                return False

            if _advance_left(left, right):
                left_index += 1
            else:
                right_index += 1

        return True

    def relation(self, other: Range[VersionType]) -> RangeRelation:
        """Return how self's members sit against other's in one interval walk."""
        left_intervals = self._intervals
        if not left_intervals:
            return _EMPTY_REL

        right_intervals = other._intervals
        if (
            len(left_intervals) >= _POINT_SET_MIN_INTERVALS
            or len(right_intervals) >= _POINT_SET_MIN_INTERVALS
        ):
            left_points = self._as_points()
            right_points = other._as_points()
            if left_points is not None and right_points is not None:
                if left_points <= right_points:
                    return _SUBSET_REL
                if left_points.isdisjoint(right_points):
                    return _DISJOINT_REL
                return _OVERLAPPING_REL

        right_count = len(right_intervals)
        is_subset = True
        is_disjoint = True
        right_index = 0

        for left in left_intervals:
            left_lower = left[0]
            left_lower_inclusive = left[1]
            while right_index < right_count:
                right = right_intervals[right_index]
                right_upper = right[2]
                if right_upper is POSITIVE_INFINITY or left_lower is NEGATIVE_INFINITY:
                    break
                if right_upper == left_lower:
                    if right[3] and left_lower_inclusive:
                        break
                elif not right_upper < left_lower:
                    break
                right_index += 1

            if right_index >= right_count:
                is_subset = False
                if not is_disjoint:
                    return _OVERLAPPING_REL
                break

            right = right_intervals[right_index]
            if _ends_before(left, right):
                is_subset = False
                if not is_disjoint:
                    return _OVERLAPPING_REL
                continue

            is_disjoint = False
            # Coverage check, written out: ``right`` must reach at least as
            # low and as high as ``left``, sharing an endpoint only when
            # ``right`` includes it too.  Equivalent to comparing the
            # _max_lower_bound/_min_upper_bound results against ``left``'s own
            # bounds, without building two bound tuples per interval pair.
            right_lower = right[0]
            if _same_bound(left_lower, right_lower):
                if left_lower_inclusive and not right[1]:
                    return _OVERLAPPING_REL
            elif left_lower is NEGATIVE_INFINITY or (
                right_lower is not NEGATIVE_INFINITY and left_lower < right_lower
            ):
                return _OVERLAPPING_REL
            left_upper = left[2]
            right_upper = right[2]
            if _same_bound(left_upper, right_upper):
                if left[3] and not right[3]:
                    return _OVERLAPPING_REL
            elif left_upper is POSITIVE_INFINITY or (
                right_upper is not POSITIVE_INFINITY and left_upper > right_upper
            ):
                return _OVERLAPPING_REL

        if is_subset:
            return _SUBSET_REL
        if is_disjoint:
            return _DISJOINT_REL
        return _OVERLAPPING_REL

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        if not isinstance(other, Range):
            return NotImplemented
        return self._intervals == other._intervals

    @override
    def __hash__(self) -> int:
        """Hash the interval tuple once and keep the answer.

        A range is immutable and is hashed over and over as a cache key, and
        each hash walks every interval and every bound in it.  Zero marks "not
        computed yet", so a tuple that really hashes to zero is stored as one.
        """
        cached = self._hash
        if cached == 0:
            cached = hash(self._intervals) or 1
            self._hash = cached
        return cached

    def __getstate__(self) -> tuple[tuple[Interval, ...]]:
        """Return the intervals alone, keeping the memo out of the pickle.

        The infinity sentinels hash as plain strings, so a memo computed in
        one process is wrong in a process running a different
        ``PYTHONHASHSEED``.  The wrapping tuple is what makes the empty range
        survive: protocols 0 and 1 discard a pickle state that is falsy, and
        an empty range's intervals are ``()``.
        """
        return (self._intervals,)

    def __setstate__(self, state: tuple[tuple[Interval, ...]]) -> None:
        """Restore from :meth:`__getstate__`, leaving the hash to be recomputed."""
        (self._intervals,) = state
        self._hash = 0
        self._points = _POINTS_UNSET

    @override
    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"Range({self._intervals!r})"

    @override
    def __str__(self) -> str:
        """Return a human-readable representation."""
        if self.is_empty:
            return "<empty>"
        if self._intervals == ((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),):
            return "*"
        parts = []
        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            if lower == upper and lower_inclusive and upper_inclusive:
                parts.append(str(lower))
            else:
                left_bracket = "[" if lower_inclusive else "("
                right_bracket = "]" if upper_inclusive else ")"
                parts.append(f"{left_bracket}{lower}, {upper}{right_bracket}")
        return " | ".join(parts)

    def __bool__(self) -> bool:
        """Return True if this range is non-empty."""
        return not self.is_empty


def _interval_sort_key(interval: Interval) -> tuple[Any, ...]:
    """Order intervals by lower bound for :func:`_normalize_intervals`."""
    lower, lower_inclusive, _upper, _upper_inclusive = interval
    if lower is NEGATIVE_INFINITY:
        return (0,)
    return (1, lower, 0 if lower_inclusive else 1)


def _normalize_intervals(intervals: list[Interval]) -> tuple[Interval, ...]:
    """Sort intervals by lower bound and merge overlapping or adjacent ones.

    Order, overlap and touching are all it repairs.  An empty interval, a
    reversed one or an inclusive infinity bound comes through untouched unless
    a merge happens to absorb or rebuild it, so this is not a way to normalize
    a list that breaks the ``Range`` invariant.
    """
    if not intervals:
        return ()

    intervals.sort(key=_interval_sort_key)

    merged: list[Interval] = [intervals[0]]
    for lower, lower_inclusive, upper, upper_inclusive in intervals[1:]:
        merged_lower, merged_lower_inclusive, merged_upper, merged_upper_inclusive = (
            merged[-1]
        )

        # Infinities at the boundary always overlap; otherwise compare values.
        intervals_overlap = (
            merged_upper is POSITIVE_INFINITY
            or lower is NEGATIVE_INFINITY
            or (
                merged_upper > lower
                or (
                    merged_upper == lower
                    and (merged_upper_inclusive or lower_inclusive)
                )
            )
        )

        if intervals_overlap:
            if merged_upper is POSITIVE_INFINITY or upper is POSITIVE_INFINITY:
                new_upper: Bound = POSITIVE_INFINITY
                new_upper_inclusive = False
            elif merged_upper > upper:
                new_upper, new_upper_inclusive = merged_upper, merged_upper_inclusive
            elif merged_upper == upper:
                new_upper = merged_upper
                new_upper_inclusive = merged_upper_inclusive or upper_inclusive
            else:
                new_upper, new_upper_inclusive = upper, upper_inclusive
            merged[-1] = (
                merged_lower,
                merged_lower_inclusive,
                new_upper,
                new_upper_inclusive,
            )
        else:
            merged.append((lower, lower_inclusive, upper, upper_inclusive))

    return tuple(merged)
