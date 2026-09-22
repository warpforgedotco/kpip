"""A dependency edge scans only the releases its specifier can admit.

Every boto3 release names a different botocore window, so the dependency
range memo never hit and each edge checked all 1,900 botocore releases for
the 20 it admits.  The specifier's conservative bounds bisect a sorted copy
of the catalog first; ``contains`` still decides inside the window, so the
selected set is exactly the set a full scan selects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.core.packaging import SpecifierSet
from kpip.core.versions import Version
from kpip.resolution.nab_provider import NabProvider

from .test_forward_check import build_random_graph, resolve
from .test_provider_memos import make_provider, wheelhouse_with_demo

CATALOG = tuple(
    Version(text)
    for text in (
        "0.9",
        "1.0a1",
        "1.0",
        "1.0.post1",
        "1.0+local",
        "1.1",
        "1.2rc1",
        "1.2",
        "1.10",
        "2.0",
        "2.0.1",
        "3.0b2",
    )
)

SPECIFIERS = (
    "",
    ">=1.0",
    ">1.0",
    "<1.2",
    "<=1.2",
    ">=1.0,<2.0",
    ">1.0,<=2.0.1",
    "~=1.0",
    "~=1.1",
    "==1.0",
    "==1.*",
    "!=1.1",
    ">=1.0,!=1.1,<2",
    "===1.0",
    ">=3.0a1",
    "<1.0a2",
)


@pytest.mark.parametrize("specifier", SPECIFIERS)
@pytest.mark.parametrize("order", ["sorted", "shuffled"])
def test_the_window_selects_what_a_full_scan_selects(
    tmp_path: Path, specifier: str, order: str
) -> None:
    provider = make_provider(wheelhouse_with_demo(tmp_path))
    allowed = CATALOG
    if order == "shuffled":
        # ``_versions`` appends an installed release out of order.
        allowed = CATALOG[3:] + CATALOG[:3]
    specifier_set = SpecifierSet(specifier)

    window = provider._bounded_versions("demo", allowed, specifier_set)

    full = [v for v in allowed if specifier_set.contains(v, allow_prereleases=True)]
    windowed = [v for v in window if specifier_set.contains(v, allow_prereleases=True)]
    assert sorted(windowed) == sorted(full), specifier
    assert len(window) <= len(allowed)


def test_a_bounded_specifier_scans_fewer_releases(tmp_path: Path) -> None:
    provider = make_provider(wheelhouse_with_demo(tmp_path))

    window = provider._bounded_versions("demo", CATALOG, SpecifierSet(">=1.1,<2.0"))

    # The edge release itself stays in the window (its local variants would
    # be admitted by ``<=``/``==``); ``contains`` rejects it for ``<``.
    assert list(window) == [
        Version("1.1"),
        Version("1.2rc1"),
        Version("1.2"),
        Version("1.10"),
        Version("2.0"),
    ]
    assert provider._bounded_versions("demo", CATALOG, SpecifierSet("!=1.1")) is CATALOG


def test_the_sorted_copy_follows_the_catalog_identity(tmp_path: Path) -> None:
    provider = make_provider(wheelhouse_with_demo(tmp_path))
    first = CATALOG
    provider._bounded_versions("demo", first, SpecifierSet(">=1.0"))
    memo = provider._sorted_versions_memo["demo"]
    assert memo[0] is first

    second = CATALOG + (Version("9.0"),)
    provider._bounded_versions("demo", second, SpecifierSet(">=1.0"))

    assert provider._sorted_versions_memo["demo"][0] is second
    assert provider._sorted_versions_memo["demo"][1][-1] == Version("9.0")


@pytest.mark.parametrize("seed", range(12))
def test_bounding_never_changes_a_resolution(
    seed: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    bounded = resolve(wheelhouse, roots)

    monkeypatch.setattr(
        NabProvider, "_bounded_versions", lambda self, package, allowed, spec: allowed
    )
    unbounded = resolve(wheelhouse, roots)

    assert bounded == unbounded, f"seed {seed}"
