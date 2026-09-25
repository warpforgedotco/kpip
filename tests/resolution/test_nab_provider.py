from __future__ import annotations

from types import SimpleNamespace

import pytest

import kpip.resolution.nab_provider
from kpip._vendor.nab_resolver.ranges import Range
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.provider import CandidateProvider
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider


class FakeProvider:
    def __init__(self) -> None:
        self.available_calls = 0
        self.events: list[tuple[str, tuple[str, ...] | str]] = []
        self.prefetch_calls: list[tuple[str, ...]] = []
        self.versions = {
            "app": (Version("1"),),
            "dep": (Version("1"), Version("2")),
        }
        self.candidates = {
            ("app", Version("1")): SimpleNamespace(
                name="app",
                canonical_name="app",
                version=Version("1"),
                dependencies=(parse_requirement("dep<2"),),
                source_url="file:///app.whl",
                source_kind="wheel",
            ),
            ("dep", Version("1")): SimpleNamespace(
                name="dep",
                canonical_name="dep",
                version=Version("1"),
                dependencies=(),
                source_url="file:///dep-1.whl",
                source_kind="wheel",
            ),
            ("dep", Version("2")): SimpleNamespace(
                name="dep",
                canonical_name="dep",
                version=Version("2"),
                dependencies=(),
                source_url="file:///dep-2.whl",
                source_kind="wheel",
            ),
        }

    def available_versions(self, requirement):
        self.available_calls += 1
        return tuple(
            SimpleNamespace(version=version)
            for version in self.versions[requirement.name]
        )

    def prefetch_available_versions(self, requirements):
        names = tuple(requirement.canonical_name for requirement in requirements)
        self.prefetch_calls.append(names)
        self.events.append(("prefetch", names))

    def find_candidates(self, requirement, *, allowed_versions=None):
        self.events.append(("find", requirement.canonical_name))
        if allowed_versions is None:
            return tuple(
                candidate
                for (name, _), candidate in self.candidates.items()
                if name == requirement.name
            )
        return (self.candidates[(requirement.name, next(iter(allowed_versions)))],)


class IndexedFakeProvider(CandidateProvider):
    def __init__(self) -> None:
        self.candidate = SimpleNamespace(
            name="app",
            canonical_name="app",
            version=Version("1"),
            dependencies=(),
            source_url="file:///app.whl",
            source_kind="wheel",
        )
        self.release_calls = 0
        self.uploaded_prior_to = None

    def available_versions(self, requirement):
        return (SimpleNamespace(version=Version("1")),)

    def catalog_versions(self, requirement):
        return (Version("1"),), frozenset()

    def find_candidates(self, requirement, *, allowed_versions):
        raise AssertionError("indexed releases should not rescan the catalog")

    def release_candidates(self, requirement, version):
        self.release_calls += 1
        return (self.candidate,)

    def get_materializer_internal(self):
        return SimpleNamespace(materialize=lambda requirement, records: records)


class EmptyConstraints(tuple):
    def __iter__(self):
        raise AssertionError("empty constraints do not need filtering")


def test_constraints_are_indexed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    marker_calls = 0
    marker_applies = kpip.resolution.nab_provider.marker_applies

    def counting_marker_applies(marker, extras=()):
        nonlocal marker_calls
        marker_calls += 1
        return marker_applies(marker, extras=extras)

    monkeypatch.setattr(
        kpip.resolution.nab_provider,
        "marker_applies",
        counting_marker_applies,
    )
    adapter = NabProvider(
        FakeProvider(),
        ResolutionConfig(
            constraints=("dep==1", "ignored==1; python_version < '0'"),
        ),
    )

    assert adapter._constraint_for("dep") == (parse_requirement("dep==1"),)
    assert adapter._constraint_for("dep[extra]") == (parse_requirement("dep==1"),)
    assert adapter._constraint_for("unrelated") == ()
    assert marker_calls == 2


def test_empty_constraint_lookup_skips_name_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())

    def fail_normalization(_name: str) -> str:
        raise AssertionError("empty constraint maps do not need name normalization")

    monkeypatch.setattr(
        kpip.resolution.nab_provider,
        "canonicalize_name",
        fail_normalization,
    )

    assert adapter._constraint_for("Dep_Pkg[extra]") == ()


class CutoffFakeProvider(IndexedFakeProvider):
    """app 1, 2 and 3, of which an upload cutoff admits only 1 and 2 -- and
    2's only file is yanked. 3 is the newest release, and the one a resolve
    that looked past the cutoff would have chosen first."""

    def __init__(self) -> None:
        super().__init__()
        self.uploaded_prior_to = object()
        self.allow_yanked = False

    def candidate_for(self, version: Version, yanked_reason: str | None):
        return SimpleNamespace(
            **{
                **vars(self.candidate),
                "version": version,
                "yanked_reason": yanked_reason,
            }
        )

    def catalog_versions(self, requirement):
        return (Version("1"), Version("2"), Version("3")), frozenset({Version("2")})

    def releases_after_cutoff(self, requirement):
        return frozenset({Version("3")})

    def release_candidates(self, requirement, version):
        self.release_calls += 1
        if version == Version("1"):
            return (self.candidate_for(version, None),)
        if version == Version("2"):
            return (self.candidate_for(version, "broken"),)
        return ()

    def find_candidates(self, requirement, *, allowed_versions=None):
        return ()

    def with_yanked_policy(self, allow_yanked):
        raise AssertionError("a release past the cutoff needs no yanked rescan")

    def get_materializer_internal(self):
        return SimpleNamespace(
            materialize=lambda requirement, records: records,
            materialize_one=lambda requirement, record: record,
        )


def test_a_release_past_the_cutoff_is_not_chosen_from() -> None:
    """A release an upload cutoff admits nothing of is left out of the
    choice, as if the index had not published it yet: the newest release
    with an unyanked file is chosen, and nothing searches the package again
    with yanked files admitted."""
    adapter = NabProvider(CutoffFakeProvider(), ResolutionConfig())

    package, version_range = adapter.add_root(parse_requirement("app"))

    assert adapter.choose_version(package, version_range) == Version("1")


def test_selected_release_materializes_without_catalog_rescan() -> None:
    provider = IndexedFakeProvider()
    adapter = NabProvider(provider, ResolutionConfig())

    package, version_range = adapter.add_root(parse_requirement("app"))

    assert adapter.choose_version(package, version_range) == Version("1")
    assert provider.release_calls == 1


@pytest.mark.parametrize(
    "version_text, allow_prereleases, policy, expected, expected_calls",
    [
        ("1", False, False, True, []),
        ("1", True, False, True, []),
        ("2a1", True, False, True, []),
        ("2a1", False, "absent", True, []),
        ("2a1", False, None, True, ["dep"]),
        ("2a1", False, True, True, ["dep"]),
        ("2a1", False, False, False, ["dep"]),
    ],
)
def test_prerelease_policy_truth_table(
    version_text: str,
    allow_prereleases: bool,
    policy: bool | None | str,
    expected: bool,
    expected_calls: list[str],
) -> None:
    provider = FakeProvider()
    calls: list[str] = []
    if policy != "absent":

        def allows_prereleases(package: str) -> bool | None:
            calls.append(package)
            assert policy is None or isinstance(policy, bool)
            return policy

        provider.release_control = SimpleNamespace(
            allows_prereleases=allows_prereleases
        )
    adapter = NabProvider(
        provider,
        ResolutionConfig(allow_prereleases=allow_prereleases),
    )

    assert adapter._allows("dep", Version(version_text)) is expected
    assert calls == expected_calls


def test_eligible_versions_only_checks_prerelease_policy_for_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    stable = Version("1")
    prerelease = Version("2a1")
    provider.versions["dep"] = (stable, prerelease)
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    package, _ = adapter.add_root(parse_requirement("dep"))
    calls: list[tuple[str, Version]] = []

    def deny(package: str, version: Version) -> bool:
        calls.append((package, version))
        return False

    monkeypatch.setattr(adapter, "_allows", deny)
    monkeypatch.setattr(
        adapter,
        "_constraint_for",
        lambda _package: EmptyConstraints(),
    )

    assert adapter._eligible_versions(package) == (stable,)
    assert calls == [("dep", prerelease)]


def test_choose_version_only_checks_prerelease_policy_for_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    stable = Version("1")
    prerelease = Version("2a1")
    provider.versions["dep"] = (stable, prerelease)
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    package, version_range = adapter.add_root(parse_requirement("dep"))
    calls: list[str] = []

    def deny(package: str) -> bool:
        calls.append(package)
        return False

    monkeypatch.setattr(adapter, "_allows_prereleases", deny)
    monkeypatch.setattr(
        adapter,
        "_constraint_for",
        lambda _package: EmptyConstraints(),
    )

    assert adapter.choose_version(package, version_range) == stable
    # Asked once for the package, because its catalog has a pre-release.
    assert calls == ["dep"]


def test_forward_check_only_checks_prerelease_policy_for_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig(ignore_installed=True))
    stable = Version("1")
    prerelease = Version("2a1")
    calls: list[tuple[str, Version]] = []

    def deny(package: str, version: Version) -> bool:
        calls.append((package, version))
        return False

    monkeypatch.setattr(adapter, "_allows", deny)
    monkeypatch.setattr(
        adapter,
        "_constraint_for",
        lambda _package: EmptyConstraints(),
    )
    monkeypatch.setattr(
        adapter,
        "_matching_catalog_versions",
        lambda _dependency: (stable, prerelease),
    )
    monkeypatch.setattr(adapter, "_prefetch_catalog_candidates", lambda *_args: None)
    monkeypatch.setattr(adapter, "_catalog_candidate", lambda *_args: object())
    monkeypatch.setattr(
        adapter,
        "_candidate_conflicts_with_active",
        lambda *_args, **_kwargs: False,
    )

    assert not adapter._dependency_domain_is_blocked(
        parse_requirement("dep"), active=None
    )
    assert calls == [("dep", prerelease)]


def test_choose_version_does_not_iterate_empty_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(
        FakeProvider(),
        ResolutionConfig(ignore_installed=True),
    )
    package, version_range = adapter.add_root(parse_requirement("app"))
    monkeypatch.setattr(
        adapter,
        "_constraint_for",
        lambda _package: EmptyConstraints(),
    )

    assert adapter.choose_version(package, version_range) == Version("1")


def test_constraints_apply_to_transitive_dependencies() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig(constraints=("dep==2",)))
    root, root_range = adapter.add_root(parse_requirement("app"))

    assert root == "app"
    assert adapter.choose_version(root, root_range) == Version("1")
    dependencies = adapter.get_dependencies(root, Version("1"))

    assert not adapter.has_satisfying_version("dep", dependencies["dep"])


def test_dependency_constraints_are_looked_up_once_per_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig(ignore_installed=True))
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    monkeypatch.setattr(adapter, "_prefetch_available_versions", lambda _: None)
    lookups = []

    def count_lookup(package: str):
        lookups.append(package)
        return EmptyConstraints()

    monkeypatch.setattr(adapter, "_constraint_for", count_lookup)

    dependencies = adapter.get_dependencies(root, Version("1"))

    assert lookups == ["dep"]
    assert dependencies["dep"] == Range.singleton(Version("1"))


def test_exact_dependency_does_not_iterate_empty_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep==1"),
    )
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    monkeypatch.setattr(adapter, "_prefetch_available_versions", lambda _: None)
    monkeypatch.setattr(
        adapter,
        "_constraint_for",
        lambda _package: EmptyConstraints(),
    )

    dependencies = adapter.get_dependencies(root, Version("1"))

    assert dependencies["dep"] == Range.singleton(Version("1"))
    assert adapter.requirements["dep"].url is None


def test_exact_transitive_dependency_skips_catalog_enumeration() -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep==2"),
    )
    adapter = NabProvider(provider, ResolutionConfig(constraints=("dep!=2",)))
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    provider.available_calls = 0

    dependencies = adapter.get_dependencies(root, Version("1"))

    assert dependencies["dep"] == Range.singleton(Version("2"))
    assert provider.available_calls == 0
    assert adapter.choose_version("dep", dependencies["dep"]) is None


def test_missing_exact_dependency_is_validated_when_selected() -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep==3"),
    )
    adapter = NabProvider(provider, ResolutionConfig())
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    provider.available_calls = 0

    dependencies = adapter.get_dependencies(root, Version("1"))

    assert dependencies["dep"] == Range.singleton(Version("3"))
    assert provider.available_calls == 0
    assert adapter.choose_version("dep", dependencies["dep"]) is None
    assert provider.available_calls == 1
    assert ("dep", Version("3")) not in adapter.records


def test_arbitrary_equality_dependency_still_enumerates_catalog() -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep===2"),
    )
    adapter = NabProvider(provider, ResolutionConfig())
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    provider.available_calls = 0

    adapter.get_dependencies(root, Version("1"))

    assert provider.available_calls == 1


def test_no_deps_does_not_expand_selected_candidate() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig(no_deps=True))
    root, root_range = adapter.add_root(parse_requirement("app"))

    assert adapter.choose_version(root, root_range) == Version("1")
    assert adapter.get_dependencies(root, Version("1")) == {}


def test_leaf_dependencies_skip_empty_prefetch_and_are_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())
    package, version_range = adapter.add_root(parse_requirement("dep"))
    version = adapter.choose_version(package, version_range)
    assert version == Version("2")

    def fail_prefetch(_requirements) -> None:
        raise AssertionError("leaf candidates do not need dependency prefetch")

    monkeypatch.setattr(adapter, "_prefetch_available_versions", fail_prefetch)

    dependencies = adapter.get_dependencies(package, version)

    assert dependencies == {}
    assert adapter.get_dependencies(package, version) is dependencies


def test_dependency_cache_shares_extra_order_but_isolates_membership() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())
    package, version_range = adapter.add_root(parse_requirement("dep[a,b]"))
    version = adapter.choose_version(package, version_range)
    assert version == Version("2")

    first = adapter.get_dependencies(package, version)
    assert len(adapter._dependency_cache) == 1

    adapter.requirements[package] = parse_requirement("dep[b,a]")
    assert adapter.get_dependencies(package, version) is first
    assert len(adapter._dependency_cache) == 1

    adapter.requirements[package] = parse_requirement("dep[a]")
    second = adapter.get_dependencies(package, version)
    assert second == first
    assert second is not first
    assert len(adapter._dependency_cache) == 2
    assert all(
        isinstance(cache_key[2], frozenset) for cache_key in adapter._dependency_cache
    )


def test_cache_keys_do_not_renormalize_frozen_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(
        FakeProvider(),
        ResolutionConfig(),
        installed={},
    )
    package, version_range = adapter.add_root(parse_requirement("dep[a,b]"))
    version = adapter.choose_version(package, version_range)
    assert version == Version("2")
    requirement = adapter.requirements[package]
    monkeypatch.setattr(
        adapter,
        "_compute_pins_are_impossible",
        lambda *_args: False,
    )

    def fail_normalization(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("frozen extras are already valid cache keys")

    monkeypatch.setattr(
        kpip.resolution.nab_provider,
        "sorted",
        fail_normalization,
        raising=False,
    )
    monkeypatch.setattr(
        kpip.resolution.nab_provider,
        "frozenset",
        fail_normalization,
        raising=False,
    )

    assert adapter._versions_uncached(package, requirement)
    assert not adapter._pins_are_impossible(package, version)
    assert adapter.get_dependencies(package, version) == {}
    assert adapter._installed_candidate(package) is None
    assert all(
        isinstance(cache_key[1], frozenset) for cache_key in adapter._installed_cache
    )
    assert all(
        isinstance(cache_key[2], frozenset) for cache_key in adapter._preflight_cache
    )
    assert all(
        isinstance(cache_key[2], frozenset) for cache_key in adapter._dependency_cache
    )


def test_has_satisfying_version_applies_requirement_and_constraints() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig(constraints=("dep==2",)))
    package, _ = adapter.add_root(parse_requirement("dep<2"))

    assert not adapter.has_satisfying_version(
        package, adapter._finite_range((Version("1"), Version("2")))
    )


def test_version_discovery_is_cached_for_same_requirement_state() -> None:
    provider = FakeProvider()
    adapter = NabProvider(provider, ResolutionConfig())
    package, _ = adapter.add_root(parse_requirement("dep"))

    assert adapter._versions(package) == adapter._versions(package)
    assert provider.available_calls == 1


def test_dependency_markers_are_filtered_at_adapter_boundary() -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep<2; python_version < '0'"),
    )
    adapter = NabProvider(provider, ResolutionConfig())
    root, root_range = adapter.add_root(parse_requirement("app"))

    adapter.choose_version(root, root_range)

    assert adapter.get_dependencies(root, Version("1")) == {}


def test_add_roots_prefetches_without_materializing_candidates() -> None:
    provider = FakeProvider()
    adapter = NabProvider(provider, ResolutionConfig())

    adapter.add_roots([parse_requirement("app"), parse_requirement("dep")])

    assert provider.prefetch_calls[0] == ("app", "dep")
    assert provider.events == [("prefetch", ("app", "dep"))]


def test_unique_roots_do_not_intersect_their_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())

    def fail_intersection(_left, _right):
        raise AssertionError("a unique root has no existing range to intersect")

    monkeypatch.setattr(Range, "__and__", fail_intersection)

    roots = adapter.add_roots([parse_requirement("app"), parse_requirement("dep")])

    assert set(roots) == {"app", "dep"}


def test_duplicate_root_ranges_are_still_intersected() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())

    roots = adapter.add_roots([parse_requirement("dep<2"), parse_requirement("dep>=2")])

    assert roots["dep"].is_empty


def test_conflicting_exact_roots_skip_catalog_work() -> None:
    provider = FakeProvider()
    adapter = NabProvider(provider, ResolutionConfig())

    roots = adapter.add_roots(
        [parse_requirement("dep==1"), parse_requirement("dep==2")]
    )

    assert roots == {"dep": Range.empty()}
    assert provider.prefetch_calls == []
    assert provider.available_calls == 0


def test_public_and_local_exact_roots_keep_their_shared_candidate() -> None:
    provider = FakeProvider()
    provider.versions["dep"] = (Version("1+cpu"),)
    adapter = NabProvider(provider, ResolutionConfig())

    roots = adapter.add_roots(
        [parse_requirement("dep==1"), parse_requirement("dep==1+cpu")]
    )

    assert roots["dep"] == Range.singleton(Version("1+cpu"))
    assert provider.available_calls == 2


def test_exact_root_preflight_checks_every_local_variant() -> None:
    provider = FakeProvider()
    adapter = NabProvider(provider, ResolutionConfig())

    roots = adapter.add_roots(
        [
            parse_requirement("dep==1"),
            parse_requirement("dep==1+cpu"),
            parse_requirement("dep==1+gpu"),
        ]
    )

    assert roots == {"dep": Range.empty()}
    assert provider.available_calls == 0


def test_conflicting_direct_url_roots_are_empty() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())

    roots = adapter.add_roots(
        [
            parse_requirement("dep @ https://example.invalid/dep.whl"),
            parse_requirement("dep @ https://mirror.invalid/dep.whl"),
        ]
    )

    assert roots["dep"].is_empty
    assert adapter.requirements["dep"].url == "https://example.invalid/dep.whl"


def test_equivalent_direct_url_roots_remain_satisfiable() -> None:
    adapter = NabProvider(FakeProvider(), ResolutionConfig())

    roots = adapter.add_roots(
        [
            parse_requirement("dep @ https://example.invalid/dep.whl?b=2&a=1"),
            parse_requirement("dep @ https://example.invalid/dep.whl?a=1&b=2"),
        ]
    )

    assert not roots["dep"].is_empty


def test_dependency_prefetch_reuses_non_self_dependency_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    dependencies = (parse_requirement("dep<2"), parse_requirement("other"))
    provider.candidates[("app", Version("1"))].dependencies = dependencies
    adapter = NabProvider(provider, ResolutionConfig())
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    monkeypatch.setattr(adapter, "_versions", lambda _package: (Version("1"),))

    key_calls: list[object] = []
    calls_at_prefetch: list[tuple[object, ...]] = []
    prefetched: list[tuple[object, ...]] = []
    dependency_key = kpip.resolution.nab_provider._key

    def counting_key(requirement):
        key_calls.append(requirement)
        return dependency_key(requirement)

    def capture_prefetch(requirements):
        calls_at_prefetch.append(tuple(key_calls))
        prefetched.append(requirements)

    monkeypatch.setattr(kpip.resolution.nab_provider, "_key", counting_key)
    monkeypatch.setattr(adapter, "_prefetch_available_versions", capture_prefetch)

    adapter.get_dependencies(root, Version("1"))

    assert len(calls_at_prefetch) == 1
    assert len(calls_at_prefetch[0]) == len(dependencies)
    assert all(
        actual is expected
        for actual, expected in zip(calls_at_prefetch[0], dependencies, strict=True)
    )
    assert type(prefetched[0]) is tuple
    assert all(
        actual is expected
        for actual, expected in zip(prefetched[0], dependencies, strict=True)
    )


def test_dependency_prefetch_still_filters_self_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    cross_first = parse_requirement("dep<2")
    self_dependency = parse_requirement("app")
    cross_last = parse_requirement("other")
    provider.candidates[("app", Version("1"))].dependencies = (
        cross_first,
        self_dependency,
        cross_last,
    )
    adapter = NabProvider(provider, ResolutionConfig())
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    monkeypatch.setattr(adapter, "_versions", lambda _package: (Version("1"),))

    prefetched: list[tuple[object, ...]] = []
    monkeypatch.setattr(adapter, "_prefetch_available_versions", prefetched.append)

    adapter.get_dependencies(root, Version("1"))

    assert len(prefetched) == 1
    assert len(prefetched[0]) == 2
    assert prefetched[0][0] is cross_first
    assert prefetched[0][1] is cross_last


def test_dependency_prefetch_filters_unusable_catalog_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep<2"),
        parse_requirement("other"),
        parse_requirement("app[extra]"),
        parse_requirement("ignored; python_version < '0'"),
        parse_requirement("direct @ https://example.invalid/direct.whl"),
        parse_requirement("constrained"),
    )
    adapter = NabProvider(
        provider,
        ResolutionConfig(
            constraints=("constrained @ https://example.invalid/constrained.whl",),
        ),
    )
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    monkeypatch.setattr(adapter, "_versions", lambda _package: (Version("1"),))

    adapter.get_dependencies(root, Version("1"))

    assert provider.prefetch_calls == [("dep", "other")]


def test_dependency_prefetch_respects_an_existing_direct_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    provider.candidates[("app", Version("1"))].dependencies = (
        parse_requirement("dep<2"),
    )
    adapter = NabProvider(provider, ResolutionConfig())
    adapter.requirements["dep"] = parse_requirement(
        "dep @ https://example.invalid/dep.whl"
    )
    root, root_range = adapter.add_root(parse_requirement("app"))
    adapter.choose_version(root, root_range)
    monkeypatch.setattr(adapter, "_versions", lambda _package: (Version("1"),))

    adapter.get_dependencies(root, Version("1"))

    assert provider.prefetch_calls == []


@pytest.mark.parametrize("direct_first", [False, True])
def test_root_prefetch_gives_direct_urls_precedence(direct_first: bool) -> None:
    provider = FakeProvider()
    adapter = NabProvider(provider, ResolutionConfig())
    named = parse_requirement("dep")
    direct = parse_requirement("dep @ https://example.invalid/dep.whl")
    duplicates = [direct, named] if direct_first else [named, direct]

    adapter.add_roots([parse_requirement("app"), *duplicates])

    assert provider.prefetch_calls[0] == ("app",)
