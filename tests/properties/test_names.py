"""PEP 503 name normalization, against the regular expression it replaces."""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st
from kpip.core.names import canonicalize_name

# The function body, below its memo: a memo keyed by a symbolic string
# would pin it to one concrete value.
canonicalize = canonicalize_name.__wrapped__

names = st.text(
    st.one_of(
        st.sampled_from("-_.aZz09"),
        st.characters(exclude_categories=("Cs",)),
    ),
    max_size=12,
)


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@given(names)
def test_matches_pep_503(name: str) -> None:
    assert canonicalize(name) == pep503(name)


@given(names)
def test_is_idempotent(name: str) -> None:
    once = canonicalize(name)
    assert canonicalize(once) == once
