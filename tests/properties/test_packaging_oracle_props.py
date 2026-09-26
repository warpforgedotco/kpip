"""kpip's versions and specifiers against the ``packaging`` library.

The property form of ``tests/core/test_packaging_oracle.py``: texts are
assembled from the same PEP 440 pieces, but Hypothesis -- or CrossHair,
under the ``crosshair`` profile -- picks them, and every disagreement must
have a cause in :data:`tests.packaging_oracles.KNOWN_DIVERGENCES`.

Each example starts from empty caches: ``Version`` interns by text, and an
earlier example's instance must not answer for this one.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from kpip.core.caches import clear_all
from kpip.core.packaging import SpecifierSet
from kpip.core.versions import InvalidVersion, Version
from packaging import specifiers, version

from tests.packaging_oracles import Divergence, classify

digits = st.integers(min_value=0, max_value=12).map(str) | st.sampled_from(
    ("00", "007", "10", "256")
)
numbers = st.one_of(st.just(""), digits)
separators = st.sampled_from(("", ".", "-", "_"))
pre_labels = st.sampled_from(("a", "b", "rc", "c", "alpha", "beta", "pre", "preview"))
post_markers = st.sampled_from((".post", "-post", "_post", "-", ".r", ".rev", "post"))
dev_markers = st.sampled_from((".dev", "-dev", "_dev", "dev"))
local_labels = st.sampled_from(("local", "ubuntu1", "1", "abc.2", "a-b", "x_y", "0"))


@st.composite
def version_texts(draw: st.DrawFn) -> str:
    text = ".".join(draw(st.lists(digits, min_size=1, max_size=4)))
    if draw(st.booleans()):
        text = f"{draw(digits)}!{text}"
    if draw(st.booleans()):
        text += draw(separators) + draw(pre_labels) + draw(numbers)
    if draw(st.booleans()):
        text += draw(post_markers) + draw(digits)
    if draw(st.booleans()):
        text += draw(dev_markers) + draw(numbers)
    if draw(st.booleans()):
        text += "+" + draw(local_labels)
    spelling = draw(st.sampled_from(("", "v", "upper", "spaced", "junk")))
    if spelling == "v":
        text = "v" + text
    elif spelling == "upper":
        text = text.upper()
    elif spelling == "spaced":
        text = f" {text} "
    elif spelling == "junk":
        text += draw(st.sampled_from(("..", "+", "!", "-", "x")))
    return text


operators = st.sampled_from(("==", "!=", "<=", ">=", "<", ">", "~=", "==="))


@st.composite
def specifier_texts(draw: st.DrawFn) -> str:
    clauses = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        operator = draw(operators)
        operand = draw(version_texts()).strip()
        if operator in ("==", "!=") and "+" not in operand and draw(st.booleans()):
            operand += ".*"
        clauses.append(operator + operand)
    return ",".join(clauses)


def parse_both(text: str) -> tuple[Version | None, version.Version | None]:
    try:
        ours = Version(text)
    except InvalidVersion:
        ours = None
    try:
        theirs = version.Version(text)
    except version.InvalidVersion:
        theirs = None
    return ours, theirs


def assert_explained(divergences: list[Divergence]) -> None:
    unknown = [d for d in divergences if classify(d) is None]
    assert not unknown, f"unexplained divergences from packaging: {unknown}"


@given(version_texts())
def test_versions_parse_and_print_alike(text: str) -> None:
    clear_all()
    ours, theirs = parse_both(text)
    divergences = []
    if (ours is None) != (theirs is None):
        divergences.append(
            Divergence("validity", None, text, ours is not None, theirs is not None)
        )
    elif ours is not None and theirs is not None:
        for observable, mine, yours in (
            ("str", str(ours), str(theirs)),
            ("is_prerelease", ours.is_prerelease, theirs.is_prerelease),
            ("base_version", ours.base_version, theirs.base_version),
        ):
            if mine != yours:
                divergences.append(Divergence(observable, None, text, mine, yours))
    assert_explained(divergences)


@given(version_texts(), version_texts())
def test_versions_order_alike(text_a: str, text_b: str) -> None:
    clear_all()
    ours_a, theirs_a = parse_both(text_a)
    ours_b, theirs_b = parse_both(text_b)
    if None in (ours_a, theirs_a, ours_b, theirs_b):
        return
    mine = (ours_a < ours_b, ours_a == ours_b, ours_a > ours_b)
    yours = (theirs_a < theirs_b, theirs_a == theirs_b, theirs_a > theirs_b)
    divergences = []
    if mine != yours:
        divergences.append(Divergence("ordering", text_a, text_b, mine, yours))
    assert_explained(divergences)


@given(specifier_texts(), version_texts())
def test_specifiers_contain_alike(text: str, candidate: str) -> None:
    clear_all()
    try:
        theirs_set = specifiers.SpecifierSet(text)
    except specifiers.InvalidSpecifier:
        theirs_set = None
    try:
        ours_set = SpecifierSet(text)
    except ValueError:
        ours_set = None
    divergences = []
    if (ours_set is None) != (theirs_set is None):
        divergences.append(
            Divergence(
                "specifier_validity",
                text,
                "",
                ours_set is not None,
                theirs_set is not None,
            )
        )
    ours_version, theirs_version = parse_both(candidate)
    if (
        ours_set is not None
        and theirs_set is not None
        and ours_version is not None
        and theirs_version is not None
    ):
        for allow, prereleases in ((False, bool(theirs_set.prereleases)), (True, True)):
            mine = ours_set.contains(ours_version, allow_prereleases=allow)
            yours = theirs_set.contains(theirs_version, prereleases=prereleases)
            if mine != yours:
                divergences.append(
                    Divergence(f"contains(allow={allow})", text, candidate, mine, yours)
                )
    assert_explained(divergences)
