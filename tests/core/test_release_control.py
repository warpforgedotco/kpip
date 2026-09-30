"""``--all-releases`` and ``--only-final`` add up as pip's do."""

from __future__ import annotations

import pytest
from kpip.core.release_control import ReleaseControl


def control(*given: tuple[str, str]) -> ReleaseControl:
    result = ReleaseControl()
    for kind, value in given:
        result.apply(kind, value)
    return result


@pytest.mark.parametrize(
    "given, all_releases, only_final",
    [
        ([("all_releases", "Foo_Bar,baz")], {"foo-bar", "baz"}, set()),
        ([("all_releases", "foo"), ("only_final", "foo")], set(), {"foo"}),
        # :all: replaces what either option said before it.
        ([("all_releases", "foo"), ("only_final", ":all:")], set(), {":all:"}),
        ([("only_final", "foo"), ("all_releases", ":all:")], {":all:"}, set()),
        ([("all_releases", "foo,:all:")], {":all:"}, set()),
        # What follows :all: counts only once it is emptied again.
        ([("all_releases", ":all:,foo")], {":all:"}, set()),
        ([("all_releases", ":all:,:none:,foo")], {"foo"}, set()),
        ([("only_final", ":all:"), ("all_releases", "foo")], {"foo"}, {":all:"}),
        ([("all_releases", "foo"), ("all_releases", ":none:")], set(), set()),
    ],
)
def test_options_add_up_in_the_order_given(
    given: list[tuple[str, str]], all_releases: set[str], only_final: set[str]
) -> None:
    result = control(*given)

    assert result.all_releases == all_releases
    assert result.only_final == only_final


def test_a_named_project_is_decided_before_all() -> None:
    result = control(("only_final", ":all:"), ("all_releases", "foo"))

    assert result.allows_prereleases("foo") is True
    assert result.allows_prereleases("bar") is False
    assert ReleaseControl().allows_prereleases("bar") is None
