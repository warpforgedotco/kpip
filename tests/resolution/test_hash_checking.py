"""The rules ``--require-hashes`` enforces.

The flag used to be recorded and never acted on: an unpinned, unhashed
requirement installed silently, and so did a VCS URL. These pin each of the
four rules, and the exemptions pip makes for direct URLs and for a pin that
comes from a constraints file.
"""

from __future__ import annotations

import pytest
from kpip.core.errors import (
    DirectoryUrlHashUnsupported,
    HashMissing,
    HashUnpinned,
    VcsHashUnsupported,
)
from kpip.resolution.hash_checking import (
    HashErrors,
    check_requirement,
    constraint_pinned_names,
    enforce_hash_checking,
)
from kpip.resolution.input_requirements import install_req_from_line

DIGEST = "0" * 64


def _requirement(line: str, *, hashes: bool = False, editable: bool = False):
    item = install_req_from_line(line)
    if hashes:
        item.hash_options = {"sha256": [DIGEST]}
    if editable:
        item.editable = True
    return item


def _failures(item, constraints: tuple[str, ...] = ()) -> set[type]:
    errors = HashErrors()
    check_requirement(item, errors, constraint_pinned_names(constraints))
    return {type(error) for error, _ in errors.errors}


def test_pinned_and_hashed_requirement_passes() -> None:
    assert _failures(_requirement("demo==1.0", hashes=True)) == set()


def test_unpinned_requirement_is_rejected() -> None:
    assert HashUnpinned in _failures(_requirement("demo>=1.0", hashes=True))


def test_missing_hash_is_rejected() -> None:
    assert HashMissing in _failures(_requirement("demo==1.0"))


def test_unpinned_and_unhashed_reports_both() -> None:
    assert _failures(_requirement("demo>=1.0")) == {HashUnpinned, HashMissing}


def test_wildcard_pin_is_not_a_pin() -> None:
    assert HashUnpinned in _failures(_requirement("demo==1.*", hashes=True))


def test_arbitrary_equality_counts_as_a_pin() -> None:
    assert HashUnpinned not in _failures(_requirement("demo===1.0", hashes=True))


def test_vcs_requirement_is_rejected_for_that_alone() -> None:
    """A repository cannot be hashed, so the pin and digest rules are moot."""
    item = _requirement("git+https://example.invalid/demo.git#egg=demo")
    assert _failures(item) == {VcsHashUnsupported}


def test_editable_requirement_is_rejected_for_that_alone() -> None:
    item = _requirement("demo", editable=True)
    assert _failures(item) == {DirectoryUrlHashUnsupported}


def test_direct_url_needs_no_pin_but_still_needs_a_hash() -> None:
    """A URL already names one artifact; a pin would add nothing."""
    url = "demo @ https://example.invalid/demo-1.0-py3-none-any.whl"
    assert _failures(_requirement(url, hashes=True)) == set()
    assert _failures(_requirement(url)) == {HashMissing}


@pytest.mark.parametrize(
    "line",
    [
        f"demo @ https://example.invalid/demo-1.0-py3-none-any.whl#sha256={DIGEST}",
        f"https://example.invalid/demo-1.0-py3-none-any.whl#sha256={DIGEST}",
    ],
)
def test_a_direct_urls_own_hash_fragment_is_its_hash(line: str) -> None:
    """pip 26.2.1 installs these with --require-hashes and no --hash."""
    assert _failures(_requirement(line)) == set()


def test_an_index_links_hash_is_not_the_users() -> None:
    """A hash on a link the index handed back is not one the user wrote."""
    from kpip.index.links import Link

    item = _requirement("demo==1.0")
    item.link = Link.from_url(
        f"https://example.invalid/demo-1.0-py3-none-any.whl#sha256={DIGEST}",
        source_url=None,
    )
    assert not item.hashes(trust_internet=False)
    assert _failures(item) == {HashMissing}


def test_a_user_supplied_direct_url_counts_its_hash_without_trusting_the_internet() -> (
    None
):
    item = install_req_from_line(
        f"demo @ https://example.invalid/demo-1.0-py3-none-any.whl#sha256={DIGEST}",
        user_supplied=True,
    )
    assert item.hashes(trust_internet=False)


def test_a_constraint_can_supply_the_pin() -> None:
    """pypa/pip#9243: `demo` in requirements, `demo==1.0` in constraints.

    The pair does resolve to a single release, so the pin rule is satisfied
    even though the requirement itself carries no specifier.
    """
    item = _requirement("demo", hashes=True)
    assert HashUnpinned in _failures(item)
    assert HashUnpinned not in _failures(item, ("demo==1.0",))
    # A constraint that does not pin does not help.
    assert HashUnpinned in _failures(item, ("demo>=1.0",))
    # Nor does one for a different project.
    assert HashUnpinned in _failures(item, ("other==1.0",))


def test_constraint_pinned_names_normalises_and_survives_junk() -> None:
    assert constraint_pinned_names(["Demo_Pkg==1.0"]) == {"demo-pkg"}
    assert constraint_pinned_names(["not a requirement", "x==1"]) == {"x"}


def test_enforce_collects_every_failure_before_raising() -> None:
    """One run should list every line to fix, not just the first."""
    with pytest.raises(HashErrors) as raised:
        enforce_hash_checking(
            [
                _requirement("a>=1.0"),
                _requirement("b==1.0"),
                _requirement("git+https://example.invalid/c.git#egg=c"),
            ],
        )
    message = str(raised.value)
    assert "a>=1.0" in message
    assert "b==1.0" in message
    assert "c" in message
    # Hardest to act on first: a VCS URL cannot be hashed at all, so telling
    # the user to pin a version above that would be noise.
    assert message.index("version control") < message.index("pinned with ==")


def test_enforce_passes_a_clean_set() -> None:
    enforce_hash_checking(
        [_requirement("a==1.0", hashes=True), _requirement("b==2.0", hashes=True)],
    )
