"""The compiled ``Range`` against the pure-Python one it replaces.

``_cranges`` is built by ``kpip-compile extensions``; without it ``Range`` is
the pure class and there is nothing to compare, so the module skips. Ranges
are built by the same seeded sequence of constructors and operators on both
classes, over ints (the generic comparison path) and over Versions whose keys
share their first eight bytes (the prefix-then-``memcmp`` path), and every
operation's result is compared.
"""

from __future__ import annotations

import pickle
import random
from typing import Any

import pytest

from kpip._vendor.nab_resolver import ranges
from kpip.core.versions import Version
from tests.range_oracles import RANGE_IS_COMPILED, load_pure_ranges

pytestmark = pytest.mark.skipif(
    not RANGE_IS_COMPILED, reason="needs the compiled Range (kpip-compile extensions)"
)

INTS = list(range(12))

# Keys of the 1.0.1.1.* releases agree well past eight bytes.
VERSIONS = sorted(
    {
        Version(text)
        for text in (
            "0.1",
            "1",
            "1.0.1",
            "1.0.1.1",
            "1.0.1.1.1",
            "1.0.1.1.2",
            "1.0.1.1.2.post1",
            "1.0.1.1.2a1",
            "1.0.1.1.2.dev0",
            "2",
            "2.1rc1",
            "2.1",
            "10.0.0.0.0.1",
            "10.0.0.0.0.2",
            "1!0.1",
        )
    }
)


@pytest.fixture(scope="module")
def pure() -> Any:
    return load_pure_ranges()


def build(cls: Any, universe: list[Any], rng: random.Random) -> Any:
    """A range from a random walk over every constructor and operator."""
    result = cls.empty()
    for _ in range(rng.randint(0, 4)):
        lower, upper = sorted(rng.sample(universe, 2))
        kind = rng.randint(0, 7)
        if kind == 0:
            piece = cls.singleton(lower)
        elif kind == 1:
            piece = cls.at_least(lower)
        elif kind == 2:
            piece = cls.greater_than(lower)
        elif kind == 3:
            piece = cls.at_most(upper)
        elif kind == 4:
            piece = cls.less_than(upper)
        elif kind == 5:
            piece = cls.full()
        elif kind == 6:
            piece = cls.from_versions(rng.sample(universe, rng.randint(0, 6)))
        else:
            piece = cls.between(
                lower,
                upper,
                lower_inclusive=rng.random() < 0.5,
                upper_inclusive=rng.random() < 0.5,
            )
        operator = rng.randint(0, 3)
        if operator == 1 and rng.random() < 0.3:
            result = result & piece
        elif operator == 2 and rng.random() < 0.3:
            result = result - piece
        elif operator == 3:
            result = ~(~result | piece)
        else:
            result = result | piece
    return result


def intervals(candidate: Any, pure: Any) -> tuple[Any, ...]:
    """``_intervals`` with the pure copy's own sentinels mapped onto ours."""
    return tuple(
        (
            ranges.NEGATIVE_INFINITY if lower is pure.NEGATIVE_INFINITY else lower,
            lower_inclusive,
            ranges.POSITIVE_INFINITY if upper is pure.POSITIVE_INFINITY else upper,
            upper_inclusive,
        )
        for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals
    )


def pair(pure: Any, universe: list[Any], seed: int) -> tuple[Any, Any]:
    compiled = build(ranges.Range, universe, random.Random(seed))
    reference = build(pure.Range, universe, random.Random(seed))
    assert intervals(compiled, pure) == intervals(reference, pure), seed
    return compiled, reference


def test_the_compiled_class_is_in_use(pure: Any) -> None:
    assert ranges.Range is not pure.Range
    assert ranges.Range.__module__.endswith("._cranges")


@pytest.mark.parametrize("universe", [INTS, VERSIONS], ids=["ints", "versions"])
@pytest.mark.parametrize("block", range(4))
def test_every_operation_matches_the_pure_class(
    pure: Any, universe: list[Any], block: int
) -> None:
    for seed in range(block * 500, (block + 1) * 500):
        left, pure_left = pair(pure, universe, seed)
        right, pure_right = pair(pure, universe, seed + 1_000_000)

        for operate in (
            lambda a, b: a & b,
            lambda a, b: a | b,
            lambda a, b: a - b,
        ):
            assert intervals(operate(left, right), pure) == intervals(
                operate(pure_left, pure_right), pure
            ), seed
        assert intervals(~left, pure) == intervals(~pure_left, pure), seed

        assert left.is_subset(right) == pure_left.is_subset(pure_right), seed
        assert left.is_superset(right) == pure_left.is_superset(pure_right), seed
        assert left.is_disjoint(right) == pure_left.is_disjoint(pure_right), seed
        assert left.relation(right) is pure_left.relation(pure_right), seed
        assert (left == right) == (pure_left == pure_right), seed
        assert (left != right) == (pure_left != pure_right), seed
        assert bool(left) == bool(pure_left)
        assert left.is_empty == pure_left.is_empty

        assert [version in left for version in universe] == [
            version in pure_left for version in universe
        ], seed
        assert left.select_sorted(universe) == pure_left.select_sorted(universe)
        assert left._as_points() == pure_left._as_points(), seed

        assert str(left) == str(pure_left), seed
        assert repr(left) == repr(pure_left), seed
        if left == right:
            assert hash(left) == hash(right), seed
        restored = pickle.loads(pickle.dumps(left))
        assert restored == left
        assert hash(restored) == hash(left)


def test_bounds_of_another_type_use_their_own_comparison() -> None:
    """Floats probe int bounds through rich comparison, as the pure class does."""
    candidate = ranges.Range.between(1, 3) | ranges.Range.singleton(5)

    assert 1.5 in candidate
    assert 3.0 not in candidate
    assert 5.0 in candidate
    assert 4.5 not in candidate


def test_a_version_never_compares_with_text() -> None:
    candidate = ranges.Range.at_least(Version("1.0"))

    with pytest.raises(TypeError):
        "2.0" in candidate  # noqa: B015


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_a_pickle_loads_without_the_extension(protocol: int) -> None:
    """It names only what the pure-Python ``ranges`` also has."""
    candidate = ranges.Range.between(
        Version("1"), Version("2")
    ) | ranges.Range.at_least(Version("3"))
    data = pickle.dumps(candidate, protocol)

    assert b"_cranges" not in data
    assert pickle.loads(data) == candidate


def test_a_pure_pickle_loads_as_the_compiled_class(pure: Any) -> None:
    """What the pure class pickles, rebuilt as pickle does where it is compiled."""
    original = pure.Range.between(Version("1"), Version("2")) | pure.Range.singleton(
        Version("3")
    )
    new_object, (cls,), state = original.__reduce_ex__(2)[:3]
    assert cls is pure.Range

    # ``ranges.Range`` is the compiled class in a process that has it.
    restored = new_object(ranges.Range)
    restored.__setstate__(state)

    assert restored == ranges.Range.between(
        Version("1"), Version("2")
    ) | ranges.Range.singleton(Version("3"))


def test_an_unpickled_sentinel_is_still_an_infinity() -> None:
    """Unpickling copies the sentinels; they must not become ordinary bounds."""
    unbounded = ranges.Range.less_than(Version("2"))
    restored = pickle.loads(pickle.dumps(unbounded))

    assert restored == unbounded
    assert Version("0.0.1") in restored
    assert str(~restored) == str(~unbounded)


def test_a_range_is_immutable() -> None:
    candidate = ranges.Range.singleton(1)

    with pytest.raises(TypeError):
        candidate.__init__(((2, True, 2, True),))
