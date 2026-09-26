"""The causes kpip's packaging may disagree with the ``packaging`` library for.

Shared by the seeded ratchet in ``tests/core/test_packaging_oracle.py`` and
the property tests in ``tests/properties/test_packaging_oracle_props.py``,
so both accept exactly the same disagreements.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from packaging import specifiers, version


class Divergence:
    """One observable on which kpip's packaging and the oracle disagree."""

    __slots__ = ("observable", "ours", "specifier", "theirs", "version")

    def __init__(
        self,
        observable: str,
        specifier: str | None,
        version: str,
        ours: object,
        theirs: object,
    ) -> None:
        self.observable = observable
        self.specifier = specifier
        self.version = version
        self.ours = ours
        self.theirs = theirs

    def __repr__(self) -> str:
        return (
            f"Divergence(observable={self.observable!r}, specifier={self.specifier!r}, "
            f"version={self.version!r}, ours={self.ours!r}, theirs={self.theirs!r})"
        )


def _normalized_specifier(text: str) -> str | None:
    """The same specifier with every operand in packaging's canonical form,
    or None when an operand cannot be normalised (wildcards, ``===``)."""
    clauses = []
    for clause in text.split(","):
        match = re.match(r"(===|==|!=|<=|>=|<|>|~=)(.*)", clause)
        assert match is not None
        operator, operand = match.groups()
        if operator == "===" or operand.endswith(".*"):
            return None
        clauses.append(operator + str(version.Version(operand)))
    return ",".join(clauses)


def _packaging_disagrees_with_its_normalised_self(d: Divergence) -> bool:
    if d.specifier is None or not d.observable.startswith("contains"):
        return False
    normalised = _normalized_specifier(d.specifier)
    if normalised is None:
        return False
    allow = d.observable.endswith("True)")
    theirs_set = specifiers.SpecifierSet(normalised)
    prereleases = True if allow else bool(theirs_set.prereleases)
    return (
        theirs_set.contains(version.Version(d.version), prereleases=prereleases)
        == d.ours
    )


KNOWN_DIVERGENCES: dict[str, Callable[[Divergence], bool]] = {
    "packaging's ~= prefix is taken from the unnormalised operand": (
        _packaging_disagrees_with_its_normalised_self
    ),
}


def classify(divergence: Divergence) -> str | None:
    for cause, matches in KNOWN_DIVERGENCES.items():
        if matches(divergence):
            return cause
    return None
