"""A choice is kept only while nothing it read of the solution has moved.

``NabProvider.choose_version`` records what it reads of the partial solution
and returns its last choice again when every read comes back the same.  That
is only sound if the kept choice is the one making it would give, so the
differential tests resolve each graph with and without the memo and require
the same answer *and* the same search: rounds and conflicts too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip._vendor.nab_resolver.ranges import Range
from kpip.core.versions import Version
from kpip.resolution.nab_provider import NabProvider

from .test_forward_check import (
    build_random_graph,
    make_transitive_backtracking_graph,
    resolve_with_metrics,
)
from .test_rejection_clauses import _provider


def without_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        NabProvider, "choose_version", NabProvider._choose_version_uncached
    )


@pytest.mark.parametrize("seed", range(40))
def test_the_memo_never_changes_the_search(
    seed: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    with_memo = resolve_with_metrics(wheelhouse, roots)
    without_memo(monkeypatch)
    assert resolve_with_metrics(wheelhouse, roots) == with_memo


def test_a_backjump_reuses_the_choices_it_undid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_transitive_backtracking_graph(wheelhouse, "fam", versions=64)
    roots = ["fam-root", "fam-left"]
    made = []
    uncached = NabProvider._choose_version_uncached

    def counting(self: NabProvider, package: str, version_range: object) -> object:
        made.append(package)
        return uncached(self, package, version_range)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(NabProvider, "_choose_version_uncached", counting)
    asked = []
    memoized = NabProvider.choose_version

    def asking(self: NabProvider, package: str, version_range: object) -> object:
        asked.append(package)
        return memoized(self, package, version_range)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(NabProvider, "choose_version", asking)
    with_memo = resolve_with_metrics(wheelhouse, roots)

    assert with_memo[1] is not None
    assert with_memo[1]["nab_conflicts"] > 0
    assert len(made) < len(asked)

    monkeypatch.setattr(NabProvider, "choose_version", uncached)
    assert resolve_with_metrics(wheelhouse, roots) == with_memo


def counted(provider: NabProvider, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []
    uncached = provider._choose_version_uncached

    def counting(package: str, version_range: object) -> object:
        made.append(package)
        return uncached(package, version_range)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(provider, "_choose_version_uncached", counting)
    return made


def test_an_unchanged_solution_reuses_the_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    provider._active_decisions = {"other": Version("1.0")}
    made = counted(provider, monkeypatch)

    first = provider.choose_version("dep", Range.full())
    again = provider.choose_version("dep", Range.full())

    assert first == again == Version("3.1")
    assert made == ["dep"]


@pytest.mark.parametrize(
    "change",
    [
        "a new range",
        "a replaced requirement",
    ],
)
def test_a_changed_input_makes_the_choice_again(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    made = counted(provider, monkeypatch)
    provider.choose_version("dep", Range.full())

    version_range = Range.full()
    if change == "a new range":
        version_range = Range.less_than(Version("3.0"))
    else:
        from kpip.core.packaging import parse_requirement

        # A different requirement: parsing the same text returns the same one.
        provider.requirements["dep"] = parse_requirement("dep>=1")

    provider.choose_version("dep", version_range)
    assert made == ["dep", "dep"]


@pytest.mark.parametrize("solution", ["_active_decisions", "_active_positive_ranges"])
def test_a_moved_read_makes_the_choice_again(
    solution: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A choice that read ``dep`` is made again once ``dep`` reads otherwise,
    and not when only an entry it never read moves."""
    provider = _provider(tmp_path, monkeypatch)
    made: list[object] = []

    def choosing(package: str, version_range: object) -> object:
        seen = getattr(provider, solution).get("dep")
        made.append(seen)
        return Version("1.0") if seen is None else Version("2.0")

    monkeypatch.setattr(provider, "_choose_version_uncached", choosing)
    value = Version("3.1") if solution == "_active_decisions" else Range.full()
    setattr(provider, solution, {"dep": value, "other": value})

    assert provider.choose_version("parent", Range.full()) == Version("2.0")
    setattr(provider, solution, {"dep": value, "other": None})
    assert provider.choose_version("parent", Range.full()) == Version("2.0")
    assert made == [value]

    setattr(provider, solution, {"other": value})
    assert provider.choose_version("parent", Range.full()) == Version("1.0")
    assert made == [value, None]


def test_a_read_of_the_whole_solution_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    made = counted(provider, monkeypatch)
    uncached = provider._choose_version_uncached

    def iterating(package: str, version_range: object) -> object:
        list(provider._active_decisions)
        return uncached(package, version_range)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(provider, "_choose_version_uncached", iterating)
    provider.choose_version("dep", Range.full())
    provider.choose_version("dep", Range.full())

    assert made == ["dep", "dep"]
