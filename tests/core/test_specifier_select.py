"""Selecting a sorted catalog by bisection agrees with ``contains``."""

from __future__ import annotations

import random

import pytest
from kpip.core.packaging import SpecifierSet
from kpip.core.versions import Version

SUFFIXES = (
    "",
    "a1",
    "b2",
    "rc1",
    "rc2",
    ".post1",
    ".post2",
    ".dev0",
    "rc1.post1",
    ".post1.dev3",
)
LOCALS = ("", "+local", "+ubuntu.1", "+2")
OPERATORS = ("==", "!=", ">=", "<=", ">", "<", "~=")


def random_release(rng: random.Random) -> str:
    return ".".join(
        str(rng.choice((0, 0, 1, 2, 3, 10))) for _ in range(rng.randint(1, 3))
    )


def random_version(rng: random.Random) -> str:
    epoch = "1!" if rng.random() < 0.05 else ""
    return f"{epoch}{random_release(rng)}{rng.choice(SUFFIXES)}{rng.choice(LOCALS)}"


def random_clause(rng: random.Random) -> str:
    operator = rng.choice(OPERATORS)
    if operator in ("==", "!=") and rng.random() < 0.3:
        return f"{operator}{random_release(rng)}.*"
    version = random_version(rng)
    if operator not in ("==", "!="):
        # Local labels are only valid with == and !=.
        version = version.split("+")[0]
    if operator == "~=" and "." not in version.split("!")[-1].rstrip(
        "abcdefghijklmnopqrstuvwxyz0123456789"
    ):
        version = f"{version.split('!')[-1].split('a')[0].split('b')[0].split('r')[0].split('.p')[0].split('.d')[0]}.0"
    return f"{operator}{version}"


@pytest.mark.parametrize("seed", range(12))
def test_selection_matches_contains(seed: int) -> None:
    rng = random.Random(seed)
    catalog = sorted({Version(random_version(rng)) for _ in range(80)})
    # A catalog lists a release twice when it has yanked and unyanked files.
    ordered = sorted(catalog + rng.sample(catalog, 8))

    for _ in range(40):
        text = ",".join(random_clause(rng) for _ in range(rng.randint(1, 3)))
        try:
            specifier = SpecifierSet(text)
        except Exception:  # noqa: BLE001 - a clause the grammar rejects
            continue
        runs = specifier.select_indices(ordered)
        if runs is None:
            continue
        selected = [version for start, stop in runs for version in ordered[start:stop]]
        expected = [
            version
            for version in ordered
            if specifier.contains(version, allow_prereleases=True)
        ]
        assert selected == expected, text


def test_an_arbitrary_clause_is_not_selected_by_order() -> None:
    assert SpecifierSet("===1.0").select_indices([Version("1.0")]) is None
