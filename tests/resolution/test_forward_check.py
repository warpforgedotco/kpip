"""The forward check may reorder work, never change the answer.

``NabProvider._pins_are_impossible`` skips candidate versions whose ``==``
pins already contradict each other, so the resolver never spends a decision
and a conflict discovering it. That is an optimization, which makes its
failure mode quiet: a check that rejects a *satisfiable* version returns an
older solution, or reports a solvable graph as unsolvable, and every existing
assertion still passes.

So the tests here are differential. Each randomized graph is resolved twice --
once normally, once with the check disabled -- and the two must agree on
whether they solved it and on every selected version. Comparing against the
resolver's own behavior is what makes the property checkable at all: there is
no independent oracle for "which version should PubGrub have picked", because
the contract is the locally-newest one its decision order happens to reach.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import kpip.resolution.nab_provider as nab_provider
from kpip._vendor.nab_resolver import propagate
from kpip._vendor.nab_resolver.ranges import Range
from kpip.core.errors import ResolutionError
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider

_HAS_EXACT_CLAUSE_DISPATCH = hasattr(propagate, "_related_incompatibility_groups")

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import (  # noqa: E402
    make_dependency_graph,
    make_selected_dependency_graph,
    make_transitive_backtracking_graph,
    make_wheel,
    make_wrong_package_graph,
    reset_caches,
)

VERSIONS = ("1.0.0", "1.1.0", "2.0.0", "2.1.0")


def resolve(wheelhouse: Path, roots: list[str]) -> dict[str, str] | None:
    """Resolve, returning name -> version, or None when unsolvable."""
    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    try:
        result = engine.resolve(roots)
    except ResolutionError:
        return None
    return {candidate.name: str(candidate.version) for candidate in result.candidates}


def resolve_with_metrics(
    wheelhouse: Path, roots: list[str]
) -> tuple[dict[str, str] | None, dict[str, int | float] | None]:
    """Resolve and retain the algorithm counters for differential checks."""
    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    try:
        result = engine.resolve(roots)
    except ResolutionError:
        return None, None
    selected = {
        candidate.name: str(candidate.version) for candidate in result.candidates
    }
    return selected, dict(result.metrics)


def build_random_graph(wheelhouse: Path, seed: int) -> list[str]:
    """Write a small random wheelhouse; return its root requirements.

    Pins are deliberately over-represented: a graph with no ``==`` never
    reaches the check, so it would prove nothing.
    """
    rng = random.Random(seed)
    names = [f"pkg{index}" for index in range(rng.randint(3, 5))]

    for depth, name in enumerate(names):
        for version in VERSIONS:
            requires = []
            for other in names[depth + 1 :]:
                if rng.random() > 0.55:
                    continue
                target = rng.choice(VERSIONS)
                form = rng.random()
                if form < 0.55:
                    requires.append(f"{other}=={target}")
                elif form < 0.8:
                    requires.append(f"{other}>={target}")
                else:
                    requires.append(f"{other}<{target}")
            make_wheel(wheelhouse, name, version, requires=requires)

    return [names[0]]


@pytest.mark.parametrize("seed", range(40))
def test_forward_check_never_changes_the_answer(
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    with_check = resolve(wheelhouse, roots)

    monkeypatch.setattr(
        NabProvider,
        "_pins_are_impossible",
        lambda self, package, version: False,
    )
    monkeypatch.setattr(
        NabProvider,
        "_partial_solution_rejects",
        lambda self, package, version: False,
    )
    monkeypatch.setattr(
        NabProvider,
        "_selected_dependency_rejects",
        lambda self, package, version: False,
    )
    without_check = resolve(wheelhouse, roots)

    assert (with_check is None) == (without_check is None), (
        f"seed {seed}: the forward check changed whether the graph is solvable"
    )
    assert with_check == without_check, (
        f"seed {seed}: the forward check changed the selected versions"
    )


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.skipif(
    not _HAS_EXACT_CLAUSE_DISPATCH,
    reason="requires kpip's exact-parent clause dispatch patch",
)
def test_exact_clause_dispatch_matches_exhaustive_propagation(
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)
    indexed = resolve_with_metrics(wheelhouse, roots)

    def exhaustive_groups(resolver: Any, package: Any):
        return (
            resolver.package_to_incompatibilities.get(package, ()),
            resolver.dependency_parent_incompatibilities.get(package, ()),
        )

    monkeypatch.setattr(
        propagate,
        "_related_incompatibility_groups",
        exhaustive_groups,
    )
    exhaustive = resolve_with_metrics(wheelhouse, roots)

    assert indexed == exhaustive, f"seed {seed}: indexed propagation diverged"


def test_impossible_pins_are_skipped_without_conflicts(tmp_path: Path) -> None:
    """The workload the check exists for: only the oldest root is satisfiable."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wrong_package_graph(wheelhouse, "fam", versions=16)

    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    result = engine.resolve(["fam-root"])

    selected = {
        candidate.name: str(candidate.version) for candidate in result.candidates
    }
    assert selected == {
        "fam-root": "1.1.0",
        "fam-left": "1.1.0",
        "fam-right": "1.1.0",
        "fam-shared": "1.1.0",
    }
    assert result.metrics["nab_conflicts"] <= 2, result.metrics


@pytest.mark.parametrize(
    "implied_ranges, expected, expected_relations, expected_intersections",
    [
        ([Range.empty(), Range.full()], True, 0, 0),
        (
            [Range.singleton(Version("1")), Range.singleton(Version("2"))],
            True,
            1,
            0,
        ),
        (
            [Range.singleton(Version("1")), Range.at_least(Version("1"))],
            False,
            1,
            0,
        ),
        (
            [
                Range.between(Version("1"), Version("3")),
                Range.between(Version("2"), Version("4")),
                Range.at_most(Version("1")),
            ],
            True,
            2,
            1,
        ),
    ],
)
def test_pin_domain_fold_avoids_redundant_intersections(
    monkeypatch: pytest.MonkeyPatch,
    implied_ranges: list[Range[Version]],
    expected: bool,
    expected_relations: int,
    expected_intersections: int,
) -> None:
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    version = Version("1")
    pins = tuple(
        parse_requirement(f"child-{index}==1") for index in range(len(implied_ranges))
    )
    candidates = {
        ("parent", version): SimpleNamespace(dependencies=pins),
        **{
            (f"child-{index}", version): SimpleNamespace(
                dependencies=(parse_requirement(f"shared=={index + 1}"),),
            )
            for index in range(len(implied_ranges))
        },
    }
    ranges_by_specifier = {
        f"=={index + 1}": implied for index, implied in enumerate(implied_ranges)
    }
    monkeypatch.setattr(
        adapter,
        "_catalog_candidate",
        lambda package, candidate_version: candidates.get(
            (package, candidate_version),
        ),
    )
    monkeypatch.setattr(
        nab_provider,
        "_implied_range",
        lambda specifier: ranges_by_specifier[specifier.text],
    )

    relation_calls = 0
    intersection_calls = 0
    original_relation = Range.relation
    original_and = Range.__and__

    def counting_relation(self: Range[Version], other: Range[Version]):
        nonlocal relation_calls
        relation_calls += 1
        return original_relation(self, other)

    def counting_and(self: Range[Version], other: object) -> Range[Version]:
        nonlocal intersection_calls
        intersection_calls += 1
        return original_and(self, other)

    monkeypatch.setattr(Range, "relation", counting_relation)
    monkeypatch.setattr(Range, "__and__", counting_and)

    assert (
        adapter._compute_pins_are_impossible("parent", version, frozenset()) is expected
    )
    assert relation_calls == expected_relations
    assert intersection_calls == expected_intersections


def test_transitive_conflicts_are_skipped_without_backtracking(tmp_path: Path) -> None:
    """A known root range can disqualify every child of a newer candidate."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_transitive_backtracking_graph(wheelhouse, "fam", versions=256)

    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    result = engine.resolve(["fam-root", "fam-shared==1.1.0", "fam-left"])

    selected = {
        candidate.name: str(candidate.version) for candidate in result.candidates
    }
    assert selected == {
        "fam-root": "1.0.0",
        "fam-left": "1.1.0",
        "fam-right": "1.1.0",
        "fam-shared": "1.1.0",
    }
    assert result.metrics["nab_conflicts"] <= 2, result.metrics


@pytest.mark.parametrize(
    "implied, expected",
    [
        (Range.singleton(Version("2")), True),
        (Range.between(Version("1"), Version("3")), False),
    ],
)
@pytest.mark.parametrize(
    "method_name",
    ["_partial_solution_rejects", "_candidate_conflicts_with_active"],
)
def test_active_constraint_checks_disjoint_without_intersection(
    method_name: str,
    implied: Range[Version],
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    dependency = parse_requirement("shared>=1")
    candidate = SimpleNamespace(source_kind="wheel", dependencies=(dependency,))
    adapter.requirements["parent"] = parse_requirement("parent")
    adapter._root_packages.add("parent")
    adapter._constrained_root_packages.add("shared")
    active = Range.singleton(Version("1"))
    adapter._active_positive_ranges = {"shared": active}
    monkeypatch.setattr(adapter, "_catalog_candidate", lambda *_args: candidate)
    monkeypatch.setattr(
        adapter,
        "_dependency_domain_is_blocked",
        lambda *_args: False,
    )
    monkeypatch.setattr(nab_provider, "_implied_range", lambda _specifier: implied)

    disjoint_calls = 0
    original_is_disjoint = Range.is_disjoint

    def counting_is_disjoint(
        self: Range[Version],
        other: Range[Version],
    ) -> bool:
        nonlocal disjoint_calls
        disjoint_calls += 1
        return original_is_disjoint(self, other)

    def fail_intersection(self: Range[Version], other: object) -> Range[Version]:
        raise AssertionError((self, other))

    monkeypatch.setattr(Range, "is_disjoint", counting_is_disjoint)
    monkeypatch.setattr(Range, "__and__", fail_intersection)

    if method_name == "_partial_solution_rejects":
        verdict = adapter._partial_solution_rejects("parent", Version("1"))
    else:
        verdict = adapter._candidate_conflicts_with_active(
            candidate,
            extras=frozenset(),
        )

    assert verdict is expected
    assert disjoint_calls == 1


def test_selected_dependency_conflicts_are_skipped_before_backtracking(
    tmp_path: Path,
) -> None:
    """A late parent must not replay a wide graph for every blocked release."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_selected_dependency_graph(wheelhouse)

    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    result = engine.resolve(["selected-application"])
    selected = {
        candidate.name: str(candidate.version) for candidate in result.candidates
    }

    assert selected["selected-parent"] == "1.1.0"
    assert selected["selected-shared"] == "1.0.0"
    assert len(selected) == 99
    assert result.metrics["nab_conflicts"] <= 1, result.metrics
    assert result.metrics["nab_rounds"] <= 110, result.metrics


def test_selected_dependency_check_defers_when_every_release_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookahead must leave PubGrub free to backtrack the selected package."""
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    versions = [Version(f"{index}.0.0") for index in range(1, 17)]
    adapter._active_decisions["selected"] = Version("1.0.0")
    monkeypatch.setattr(
        adapter,
        "_selected_dependency_rejects",
        lambda package, version: True,
    )

    assert adapter._newest_viable("demo", versions) == Version("16.0.0")


@pytest.mark.parametrize("version_count, expected_calls", [(1, 0), (2, 1), (16, 1)])
def test_selected_dependency_lookahead_runs_for_any_domain_with_a_choice(
    version_count: int,
    expected_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    adapter._active_decisions["selected"] = Version("1.0.0")
    calls = 0

    def rejects(package: str, version: Version) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(adapter, "_selected_dependency_rejects", rejects)
    monkeypatch.setattr(adapter, "_pins_are_impossible", lambda package, version: False)
    versions = [Version(f"{index}.0.0") for index in range(1, version_count + 1)]

    assert adapter._newest_viable("demo", versions) == versions[-1]
    assert calls == expected_calls


@pytest.mark.parametrize(
    "dependency, source_kind, expected",
    [
        ("selected-shared<2; extra == 'feature'", "wheel", True),
        ("selected-shared<2; extra == 'other'", "wheel", False),
        (
            "selected-shared @ https://example.invalid/shared.whl",
            "wheel",
            False,
        ),
        ("selected-shared<2", "sdist", False),
    ],
)
def test_selected_dependency_check_respects_uncertain_sources_and_markers(
    dependency: str,
    source_kind: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    adapter.requirements["parent"] = parse_requirement("parent[feature]")
    adapter._active_decisions = {"selected-shared": Version("2.0.0")}
    candidate = SimpleNamespace(
        source_kind=source_kind,
        dependencies=(parse_requirement(dependency),),
    )
    monkeypatch.setattr(
        adapter,
        "_catalog_candidate",
        lambda package, version: candidate,
    )

    assert adapter._selected_dependency_rejects("parent", Version("1.0.0")) is expected


@pytest.mark.parametrize("seed", range(12))
def test_transitive_forward_check_never_changes_the_answer(
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random compatible releases must select identically without lookahead."""
    monkeypatch.setattr(nab_provider, "_TRANSITIVE_FORWARD_CHECK_MIN_VERSIONS", 4)
    checked = 0
    partial_solution_rejects = NabProvider._partial_solution_rejects

    def counting_check(
        self: NabProvider,
        package: str,
        version: Version,
    ) -> bool:
        nonlocal checked
        checked += 1
        return partial_solution_rejects(self, package, version)

    monkeypatch.setattr(NabProvider, "_partial_solution_rejects", counting_check)
    rng = random.Random(seed)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "shared", "1.0.0")
    make_wheel(wheelhouse, "shared", "2.0.0")

    compatible = {index for index in range(1, 13) if rng.random() < 0.3}
    compatible.add(rng.randint(1, 12))
    for index in range(1, 13):
        shared = "1.0.0" if index in compatible else "2.0.0"
        make_wheel(
            wheelhouse,
            "child",
            f"1.{index}.0",
            requires=[f"shared=={shared}"],
        )
        make_wheel(
            wheelhouse,
            "parent",
            f"1.{index}.0",
            requires=[f"child>=1.{index}.0,<1.{index + 1}.0"],
        )
    make_wheel(
        wheelhouse,
        "app",
        "1.0.0",
        requires=["shared==1.0.0", "parent"],
    )

    roots = ["app", "shared==1.0.0", "parent"]
    with_check = resolve(wheelhouse, roots)
    assert checked > 0
    monkeypatch.setattr(
        NabProvider,
        "_partial_solution_rejects",
        lambda self, package, version: False,
    )
    without_check = resolve(wheelhouse, roots)

    assert with_check == without_check, seed


def test_transitive_check_stays_off_small_candidate_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary packages must not pay speculative two-hop metadata reads."""
    provider = CandidateProvider.from_options(no_index=True)
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    versions = [Version(f"1.{index}.0") for index in range(255)]

    monkeypatch.setattr(
        adapter,
        "_pins_are_impossible",
        lambda package, version: False,
    )

    def explode(package: str, version: Version) -> bool:
        raise AssertionError((package, version))

    monkeypatch.setattr(adapter, "_partial_solution_rejects", explode)

    assert adapter._newest_viable("demo", versions) == versions[-1]


def test_unsolvable_graphs_still_fail(tmp_path: Path) -> None:
    """Rejecting every candidate must not turn into a silent wrong answer."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "app", "1.0.0", requires=["left==1.0.0", "right==1.0.0"])
    make_wheel(wheelhouse, "left", "1.0.0", requires=["shared==1.0.0"])
    make_wheel(wheelhouse, "right", "1.0.0", requires=["shared==2.0.0"])
    make_wheel(wheelhouse, "shared", "1.0.0")
    make_wheel(wheelhouse, "shared", "2.0.0")

    with pytest.raises(ResolutionError) as caught:
        resolve_or_raise(wheelhouse, ["app"])

    assert str(caught.value)


def resolve_or_raise(wheelhouse: Path, roots: list[str]) -> None:
    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    engine.resolve(roots)


def test_verdict_cache_shares_extra_order_but_isolates_membership(
    tmp_path: Path,
) -> None:
    """Extras sets with equal members share; different members never do."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "app", "1.0.0")

    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    adapter.requirements["app"] = parse_requirement("app[a,b]")

    adapter._pins_are_impossible("app", Version("1.0.0"))
    first = dict(adapter._preflight_cache)

    adapter.requirements["app"] = parse_requirement("app[b,a]")
    adapter._pins_are_impossible("app", Version("1.0.0"))
    assert adapter._preflight_cache == first

    adapter.requirements["app"] = parse_requirement("app[a,b,c]")
    adapter._pins_are_impossible("app", Version("1.0.0"))
    assert len(adapter._preflight_cache) == len(first) + 1
    assert all(
        isinstance(cache_key[2], frozenset) for cache_key in adapter._preflight_cache
    )


def test_unreadable_metadata_is_undecidable_not_fatal(tmp_path: Path) -> None:
    """A release whose metadata will not load must not fail the resolution."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "app", "1.0.0")

    class Exploding:
        version = Version("1.0.0")

        @property
        def dependencies(self) -> tuple[object, ...]:
            raise OSError("metadata is unreachable")

    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    adapter.requirements["app"] = parse_requirement("app")
    adapter._catalog_candidate_cache[("app", Version("1.0.0"))] = Exploding()

    assert adapter._pins_are_impossible("app", Version("1.0.0")) is False


def test_forward_check_does_not_hide_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CandidateProvider.from_options(no_index=True)
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    adapter.requirements["app"] = parse_requirement("app")

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected provider defect")

    monkeypatch.setattr(provider, "release_candidates", explode)

    with pytest.raises(RuntimeError, match="unexpected provider defect"):
        adapter._pins_are_impossible("app", Version("1.0.0"))


def test_malformed_requires_python_rejects_without_raising() -> None:
    """The index provider treats bad metadata as incompatible; so must this."""

    class BadMetadata:
        requires_python = "not a specifier"

    provider = CandidateProvider.from_options(no_index=True)
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))

    assert adapter._requires_python_rejects(BadMetadata()) is True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("dep==1.2.3", "1.2.3"),
        ("dep == 1.2.3", "1.2.3"),
        ("dep>=1.2.3", None),
        ("dep==1.*", None),
        ("dep===1.2.3", None),
        ("dep>=1.0,<2.0", None),
        ("dep~=1.2", None),
        ("dep", None),
    ],
)
def test_exact_pin_recognizes_only_unique_releases(
    text: str,
    expected: str | None,
) -> None:
    """Anything that is not one concrete release must read as "not a pin"."""
    pinned = parse_requirement(text).specifier.exact_version

    assert (None if pinned is None else str(pinned)) == expected


def test_finite_range_matches_a_union_of_singletons() -> None:
    """``_finite_range`` builds in one pass what unioning built step by step.

    Unioning re-sorted and re-merged the whole interval list on every step,
    which is quadratic in a package's release count. The one-pass form has to
    produce the identical range, including for duplicate and unsorted input.
    """
    from kpip._vendor.nab_resolver.ranges import Range
    from kpip.resolution.nab_provider import NabProvider

    def by_union(versions: list[Version]) -> Range:
        result: Range = Range.empty()
        for version in versions:
            result = result | Range.singleton(version)
        return result

    rng = random.Random(4)
    for _ in range(200):
        versions = [
            Version(f"1.{rng.randint(0, 20)}.{rng.randint(0, 3)}")
            for _ in range(rng.randint(0, 12))
        ]
        if versions and rng.random() < 0.4:
            versions += rng.sample(versions, k=min(3, len(versions)))
        if rng.random() < 0.3:
            versions.sort(reverse=True)

        assert (
            NabProvider._finite_range(versions)._intervals
            == by_union(versions)._intervals
        )


def test_forward_check_queries_releases_by_exact_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check reads one catalog entry per release it inspects.

    Asking the provider for a package by name re-evaluates every link and
    materializes every release -- the work the resolver's own requirement
    already paid for. The resolver's own version lookup queries each package
    by name once (a root twice: once when added, once when decided); the
    check must not add a second name-only query per package on top of it.
    """
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_dependency_graph(wheelhouse)
    queried: list[str] = []
    releases: list[tuple[str, Version]] = []
    original = CandidateProvider.find_candidates
    original_release = CandidateProvider.release_candidates

    def recording(self: CandidateProvider, requirement, **kwargs):  # type: ignore[no-untyped-def]
        queried.append(requirement.specifier.text)
        return original(self, requirement, **kwargs)

    def recording_release(self: CandidateProvider, requirement, version):  # type: ignore[no-untyped-def]
        releases.append((requirement.canonical_name, version))
        return original_release(self, requirement, version)

    monkeypatch.setattr(CandidateProvider, "find_candidates", recording)
    monkeypatch.setattr(CandidateProvider, "release_candidates", recording_release)

    selected = resolve(wheelhouse, ["application"])
    assert selected is not None

    unconstrained = [text for text in queried if not text]
    assert len(unconstrained) <= len(selected) + 1, queried
    assert releases, "the forward check inspected no release"
    assert len(releases) == len(set(releases)), "a release was read twice"
