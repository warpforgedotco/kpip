"""Two shortcuts in ``choose_version`` that must not change its answer.

The version filter is memoized per (package, versions, range): the resolver
re-asks a package's choice far more often than its range moves. And a yanked
release the filter would hand ``_newest_viable`` is sidestepped up front for
the newest unyanked release in range that the constraints admit, which is
what ``_retry_including_yanked`` would settle on after two catalog rescans.
"""

from __future__ import annotations

from typing import Any

from kpip._vendor.nab_resolver.ranges import Range
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.provider import CandidateProvider
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider

V = [Version(text) for text in ("1.0", "1.1", "2.0", "2.1")]


def _adapter() -> NabProvider:
    return NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )


def test_sidestep_returns_the_newest_unyanked_release_the_constraints_admit() -> None:
    adapter = _adapter()
    adapter._yanked_versions["demo"] = frozenset({V[3], V[2]})
    constraints = (parse_requirement("demo<2.1"),)

    chosen = adapter._sidestep_yanked("demo", V[3], list(V), constraints)

    assert chosen == V[1]


def test_sidestep_keeps_a_yanked_release_that_is_all_that_is_left() -> None:
    adapter = _adapter()
    adapter._yanked_versions["demo"] = frozenset({V[3]})

    assert adapter._sidestep_yanked("demo", V[3], [V[3]], ()) == V[3]


def test_sidestep_leaves_an_unyanked_choice_alone() -> None:
    adapter = _adapter()
    adapter._yanked_versions["demo"] = frozenset({V[0]})

    assert adapter._sidestep_yanked("demo", V[3], list(V), ()) == V[3]
    assert adapter._sidestep_yanked("other", V[3], list(V), ()) == V[3]


def test_version_filter_is_memoized_until_the_versions_or_range_move(
    monkeypatch: Any,
) -> None:
    adapter = _adapter()
    versions = tuple(V)
    adapter.requirements["demo"] = parse_requirement("demo")
    monkeypatch.setattr(adapter, "_versions", lambda package: versions)
    monkeypatch.setattr(
        adapter, "_newest_viable", lambda package, matching: matching[-1]
    )
    monkeypatch.setattr(adapter, "_candidates_for_version", lambda *a: (object(),))
    monkeypatch.setattr(adapter, "_invalid_metadata_rejects", lambda c: False)
    monkeypatch.setattr(adapter, "_prefetch_descent_window", lambda *a: None)
    wide = Range.from_versions(V[:3])

    adapter.choose_version("demo", wide)
    first = adapter._matching_memo["demo"][2]
    adapter.choose_version("demo", Range.from_versions(V[:3]))

    assert adapter._matching_memo["demo"][2] is first
    assert first == V[:3]

    adapter.choose_version("demo", Range.from_versions(V[:2]))

    assert adapter._matching_memo["demo"][2] == V[:2]


def test_dependency_ranges_are_memoized_per_specifier_and_catalog(
    monkeypatch: Any,
) -> None:
    """The same specifier on the same catalog is scanned once, across parents
    and re-decisions, until the catalog or the constraints move."""
    adapter = _adapter()
    versions = tuple(V)
    scans: list[str] = []
    monkeypatch.setattr(adapter, "_versions", lambda package: versions)
    real_finite = adapter._finite_range

    def counting(selected: Any) -> Any:
        scans.append("scan")
        return real_finite(selected)

    monkeypatch.setattr(adapter, "_finite_range", counting)
    monkeypatch.setattr(adapter, "_prefetch_available_versions", lambda deps: None)
    adapter.requirements["parent"] = parse_requirement("parent")
    adapter.records[("parent", V[0])] = type(
        "R", (), {"dependencies": (parse_requirement("child>=1.1"),)}
    )()

    first = adapter.get_dependencies("parent", V[0])
    adapter._dependency_cache.clear()
    second = adapter.get_dependencies("parent", V[0])

    assert first == second
    assert scans == ["scan"]
    assert set(str(v) for v in first["child"]._as_points()) == {"1.1", "2.0", "2.1"}
