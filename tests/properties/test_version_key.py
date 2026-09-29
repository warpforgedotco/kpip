"""A Version's bytes must sort as its parts do.

:func:`kpip.core.versions.version_key` writes ``(epoch, release, suffix,
local)`` so that byte order is tuple order; the resolver compares Versions
by ``memcmp`` alone and trusts this. These properties state the rule on the
key functions directly, below ``Version``'s intern table, over the shapes
``_parse`` produces: a suffix is six integers whose first may be -1, and a
local label is parts ``(0, text)`` or ``(1, number)``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from kpip.core.versions import (
    FINAL_SUFFIX,
    _int_code,
    _trimmed,
    release_key,
    version_key,
)

# A number's code spends one byte on its length, so 255 bytes of digits is
# the most a key can hold.
LIMIT = 256**255

numbers = st.integers(min_value=0, max_value=LIMIT - 1)
small_numbers = st.integers(min_value=0, max_value=2**20)
releases = st.lists(small_numbers, min_size=1, max_size=5).map(
    lambda release: _trimmed(tuple(release))
)
suffixes = st.one_of(
    st.just(FINAL_SUFFIX),
    st.tuples(
        st.integers(min_value=-1, max_value=3),
        *[small_numbers] * 5,
    ),
)
# Text parts never hold a zero byte, which ends them, nor a lone surrogate,
# which cannot be encoded.
local_text = st.text(
    st.characters(exclude_categories=("Cs",), exclude_characters="\0"),
    min_size=1,
    max_size=6,
)
locals_ = st.lists(
    st.one_of(
        st.tuples(st.just(0), local_text),
        st.tuples(st.just(1), small_numbers),
    ),
    max_size=3,
).map(tuple)
epochs = st.integers(min_value=0, max_value=3) | small_numbers
parts = st.tuples(epochs, releases, suffixes, locals_)


def _order(a: object, b: object) -> tuple[bool, bool]:
    return (a < b, a == b)  # type: ignore[operator]


@given(numbers, numbers)
def test_int_codes_sort_as_their_numbers(a: int, b: int) -> None:
    assert _order(_int_code(a), _int_code(b)) == _order(a, b)


@given(numbers, numbers)
def test_int_codes_are_self_delimiting(a: int, b: int) -> None:
    code_a, code_b = _int_code(a), _int_code(b)
    if a != b:
        assert not code_a.startswith(code_b)
        assert not code_b.startswith(code_a)


@given(st.integers(min_value=LIMIT, max_value=LIMIT * 256))
def test_int_code_refuses_numbers_too_long_to_measure(number: int) -> None:
    with pytest.raises(ValueError, match="too large"):
        _int_code(number)


@given(parts, parts)
def test_keys_sort_as_their_parts(
    a: tuple[int, tuple[int, ...], tuple[int, ...], tuple],
    b: tuple[int, tuple[int, ...], tuple[int, ...], tuple],
) -> None:
    assert _order(version_key(*a), version_key(*b)) == _order(a, b)


@given(parts, st.lists(st.just(0), max_size=3))
def test_release_key_starts_every_key_of_its_release(
    version: tuple[int, tuple[int, ...], tuple[int, ...], tuple],
    zeros: list[int],
) -> None:
    epoch, release, suffix, local = version
    # ``release_key`` takes a release as written, trailing zeros and all.
    written = release + tuple(zeros)
    assert version_key(epoch, release, suffix, local).startswith(
        release_key(epoch, written)
    )


@given(parts, epochs, releases)
def test_release_key_follows_every_earlier_release(
    earlier: tuple[int, tuple[int, ...], tuple[int, ...], tuple],
    epoch: int,
    release: tuple[int, ...],
) -> None:
    if (earlier[0], earlier[1]) < (epoch, release):
        assert version_key(*earlier) < release_key(epoch, release)
