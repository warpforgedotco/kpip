from __future__ import annotations

import sys
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from kpip._vendor.nab_resolver.ranges import Range
from kpip._vendor.nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)
from kpip.core.metadata import InstalledDistribution, find_installed
from kpip.core.packaging import (
    Requirement,
    SpecifierSet,
    intersect_runs,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from kpip.core.errors import KpipError
from kpip.core.versions import InvalidVersion, Version, ZERO_VERSION
from kpip.core.wheel import WheelCandidate
from kpip.index.candidate_evaluators import CandidateEvaluator
from kpip.index.provider import CandidateProvider
from kpip.index.source_models import CandidateRecord
from kpip.resolution.models import ResolutionConfig, canonical_url, url_name
from kpip.resolution.nab_types import (
    _MIN_PINS_TO_DISAGREE,
    InstalledCandidate,
    _dependencies_or_none,
    _implied_range,
    _key,
    _RecordingRequirements,
)

_MISSING = object()
_FORWARD_CHECK_BATCH = 32

# Releases to start metadata for below the one just chosen. The forward-check
# batch above cannot fire on a descent: _newest_viable accepts the newest
# survivor at index 0 and returns first, leaving the walk serial.
_DESCENT_PREFETCH_WINDOW = 32
# The one-hop check reads a release's own metadata, which deciding the
# release would read anyway, so it is not gated on catalog size: a package
# with 13 releases whose newest 10 all excluded a decided release cost ten
# backjumps of a hundred decisions each on airflow's graph.
_SELECTED_FORWARD_CHECK_MIN_VERSIONS = 2
# Two-hop metadata scans only repay their cost on genuinely large root domains.
_TRANSITIVE_FORWARD_CHECK_MIN_VERSIONS = 256


def _conflicting_exact_root(
    requirements: list[Requirement],
) -> tuple[str, Requirement] | None:
    """Return one root whose exact requirements are provably disjoint.

    A sole ``==`` clause names one PEP 440 equality class.  Two such clauses
    can overlap when a public pin admits the other's local version, so compare
    both exact witnesses instead of comparing their ``Version`` objects.
    """
    exact_roots: dict[str, list[tuple[SpecifierSet, Version]]] = {}
    for requirement in requirements:
        exact = requirement.specifier.exact_version
        if exact is None:
            continue
        package = _key(requirement)
        previous = exact_roots.setdefault(package, [])
        for specifier, previous_exact in previous:
            if not (
                specifier.contains(exact, allow_prereleases=True)
                or requirement.specifier.contains(
                    previous_exact,
                    allow_prereleases=True,
                )
            ):
                return package, requirement
        previous.append((requirement.specifier, exact))
    return None


class NabProvider:
    """Native NAB provider backed by kpip candidate discovery."""

    def __init__(
        self,
        provider: CandidateProvider,
        context: ResolutionConfig,
        installed: Mapping[str, InstalledDistribution] | None = None,
    ) -> None:
        self.provider = provider
        self.context = context
        self.installed = installed
        self.__post_init__()

    def __post_init__(self) -> None:
        self.allow_prereleases = self.context.allow_prereleases
        self.no_deps = self.context.no_deps
        self.constraints = self.context.constraints
        self.ignore_requires_python = self.context.ignore_requires_python
        self._preferences: dict[str, Version] = {}
        for name, text in self.context.preferences.items():
            try:
                self._preferences[name] = Version(text)
            except InvalidVersion:
                pass
        self._descent_prefetched: set[tuple[str, Version]] = set()
        self._source_metadata_started: set[str] = set()
        self._release_records: dict[
            tuple[str, Version, frozenset[str]],
            tuple[CandidateRecord, ...] | None,
        ] = {}
        self._child_lookahead_floor: dict[str, Version] = {}
        self._pending_clauses: list[Incompatibility[str, Version]] = []
        self._rejection_clauses_queued: set[tuple[str, Version, str]] = set()
        self._child_lookahead_last: dict[str, Version] = {}
        self._child_lookahead_attempts: dict[str, int] = {}
        self._descent_attempts: dict[str, int] = {}
        self._descent_last: dict[str, Version] = {}
        self._yanked_versions: dict[str, frozenset[Version]] = {}
        self._sorted_versions_memo: dict[
            str, tuple[tuple[Version, ...], list[Version]]
        ] = {}
        self._dependency_range_memo: dict[
            tuple[str, str],
            tuple[tuple[Version, ...], tuple[Requirement, ...], Range[Version]],
        ] = {}
        self._matching_memo: dict[
            str, tuple[tuple[Version, ...], RangeProtocol[Version], list[Version]]
        ] = {}
        self._catalog_shape_memo: dict[str, tuple[tuple[Version, ...], bool, bool]] = {}
        self._descent_order: dict[
            str, tuple[tuple[Version, ...], tuple[Version, ...], dict[Version, int]]
        ] = {}
        self.python_version = self.context.python_version
        self.records: dict[
            tuple[str, Version], WheelCandidate | InstalledCandidate
        ] = {}
        self.requirements: _RecordingRequirements = _RecordingRequirements()
        self.display_requirements: dict[str, Requirement] = {}
        self._unpinned_requirements: dict[str, tuple[Requirement, Requirement]] = {}
        self._version_cache: dict[tuple[object, ...], tuple[Version, ...]] = {}
        self._version_memo: dict[str, tuple[Requirement, tuple[Version, ...]]] = {}
        self._priority_memo: dict[
            str, tuple[Requirement, int, tuple[int, int, str]]
        ] = {}
        self._installed_cache: dict[
            tuple[str, frozenset[str]], InstalledCandidate | None
        ] = {}
        self._preflight_cache: dict[tuple[str, Version, frozenset[str]], bool] = {}
        self._catalog_candidate_cache: dict[tuple[str, Version], object | None] = {}
        self._catalog_by_version_cache: dict[str, dict[Version, object | None]] = {}
        self._dependency_cache: dict[
            tuple[str, Version, frozenset[str]], Mapping[str, Range[Version]]
        ] = {}
        self._active_decisions: Mapping[str, Version] = {}
        self._active_positive_ranges: Mapping[str, RangeProtocol[Version]] = {}
        self._root_packages: set[str] = set()
        self._constrained_root_packages: set[str] = set()
        self._partial_preflight_cache: dict[
            tuple[str, Version, frozenset[str]], bool
        ] = {}
        self._active_candidate_conflict_cache: dict[
            tuple[str, Version, frozenset[str]], bool
        ] = {}
        self._forward_catalog_versions: dict[str, tuple[Version, ...]] = {}
        self._dependency_invalidations: dict[str, None] = {}
        constraints_by_name: dict[str, list[Requirement]] = {}
        for value in self.constraints:
            requirement = parse_requirement(value)
            if requirement.url is not None and requirement.name.startswith(
                ("file://", "http://", "https://")
            ):
                name = url_name(requirement.url)
                if name:
                    requirement = Requirement(
                        name=name,
                        specifier=requirement.specifier,
                        extras=requirement.extras,
                        url=requirement.url,
                        marker=requirement.marker,
                        raw=requirement.raw,
                    )
            if marker_applies(requirement.marker):
                constraints_by_name.setdefault(requirement.canonical_name, []).append(
                    requirement,
                )
        self.constraints_by_name = {
            name: tuple(requirements)
            for name, requirements in constraints_by_name.items()
        }

    def _installed_candidate(self, package: str) -> InstalledCandidate | None:
        if self.context.ignore_installed:
            return None
        if self.requirements[package].url is not None or any(
            constraint.url is not None for constraint in self._constraint_for(package)
        ):
            return None
        extras = self.requirements[package].extras
        cache_key = (package, extras)
        if cache_key not in self._installed_cache:
            distribution = (
                find_installed(package)
                if self.installed is None
                else self.installed.get(package)
            )
            if distribution is None:
                candidate = None
            else:
                try:
                    candidate = InstalledCandidate(
                        distribution,
                        extras,
                    )
                except ValueError:
                    candidate = None
            self._installed_cache[cache_key] = candidate
        return self._installed_cache[cache_key]

    def _constraint_for(self, package: str) -> tuple[Requirement, ...]:
        if not self.constraints_by_name:
            return ()
        name = canonicalize_name(package.split("[", 1)[0])
        return self.constraints_by_name.get(name, ())

    def _prefetch_available_versions(
        self, requirements: tuple[Requirement, ...]
    ) -> None:
        """Start catalog requests while the resolver still has independent work."""
        if not requirements:
            return

        if isinstance(self.provider, CandidateProvider) and (
            self.provider.session is None
            or not self.provider.prefetch_remote_sources
            or len(requirements) < 2
        ):
            return

        prefetch = getattr(self.provider, "prefetch_available_versions", None)
        if prefetch is not None:
            keyed = [(_key(requirement), requirement) for requirement in requirements]
            # Look up only the incoming packages rather than walking every
            # requirement the resolver holds.
            known = self.requirements
            direct_packages = {
                package
                for package, requirement in keyed
                if requirement.url is not None
                or (
                    (current := known.get(package)) is not None
                    and current.url is not None
                )
            }
            catalog_requirements = tuple(
                requirement
                for package, requirement in keyed
                if requirement.url is None
                and package not in direct_packages
                and not any(
                    constraint.url is not None
                    for constraint in self._constraint_for(package)
                )
            )
            if catalog_requirements and (
                not isinstance(self.provider, CandidateProvider)
                or len(catalog_requirements) >= 2
            ):
                prefetch(catalog_requirements)

    def _prefetch_preferred_catalogs(self) -> None:
        """Start the page of every package the previous lock holds, at once.

        Without this the resolve reaches each page a dependency level at a
        time: a package's dependencies are known only once its own metadata
        is, so an hour-old jupyter lock revalidated its 99 pages in 35
        rounds. The previous lock names nearly every package this one will
        read, so their pages can all be asked for in the first round.
        """
        if not self._preferences or not isinstance(self.provider, CandidateProvider):
            return

        self.provider.prefetch_available_versions(
            tuple(parse_requirement(name) for name in self._preferences),
            lookahead=True,
        )

    def _versions(self, package: str) -> tuple[Version, ...]:
        requirement = self.requirements[package]
        memo = self._version_memo.get(package)
        if memo is not None and memo[0] is requirement:
            return memo[1]

        versions = self._versions_uncached(package, requirement)
        self._version_memo[package] = (requirement, versions)
        return versions

    def _versions_uncached(
        self,
        package: str,
        requirement: Requirement,
    ) -> tuple[Version, ...]:
        cache_key = (
            package,
            requirement.specifier.text,
            requirement.extras,
            requirement.url,
        )
        cached = self._version_cache.get(cache_key)
        if cached is not None:
            return cached
        if requirement.url is not None:
            candidates = tuple(self.provider.find_candidates(requirement))
            versions = tuple(candidate.version for candidate in candidates)
        else:
            catalog_versions = getattr(self.provider, "catalog_versions", None)
            if catalog_versions is not None:
                # The versions and which are yanked, without a summary built
                # for each of a catalog's releases.
                versions, yanked = catalog_versions(requirement)
            else:
                summaries = self.provider.available_versions(requirement)
                versions = tuple(summary.version for summary in summaries)
                yanked = frozenset(
                    summary.version
                    for summary in summaries
                    if getattr(summary, "is_yanked", False)
                )
            if yanked:
                self._yanked_versions[package] = yanked
            else:
                self._yanked_versions.pop(package, None)
        installed = self._installed_candidate(package)
        if installed is not None and installed.version not in versions:
            versions += (installed.version,)
        if not versions and requirement.url is None:
            fallback_provider = self._provider_with_yanked()
            fallback_candidates = tuple(
                fallback_provider.find_candidates(parse_requirement(package))
            )
            if fallback_candidates:
                unyanked = tuple(
                    candidate
                    for candidate in fallback_candidates
                    if getattr(candidate, "yanked_reason", None) is None
                )
                versions = tuple(
                    candidate.version for candidate in (unyanked or fallback_candidates)
                )
                self._version_cache[cache_key] = versions
                return versions
        self._version_cache[cache_key] = versions
        return versions

    def _provider_with_yanked(self):
        """Return an explicit yanked-policy view when the provider supports it."""
        if isinstance(self.provider, CandidateProvider):
            return self.provider.with_yanked_policy(True)
        return self.provider

    def _allows(self, package: str, version: Version) -> bool:
        return not version.is_prerelease or self._allows_prereleases(package)

    def _allows_prereleases(self, package: str) -> bool:
        """Whether ``package``'s pre-releases may be chosen at all."""
        if self.allow_prereleases:
            return True
        control = getattr(self.provider, "release_control", None)
        return control is None or control.allows_prereleases(package) is not False

    def _catalog_shape(
        self, package: str, versions: tuple[Version, ...]
    ) -> tuple[bool, bool]:
        """Whether ``versions`` ascends, and whether it has a pre-release.

        Asked once per catalog tuple: the index's catalog is sorted, but an
        installed release appended to it need not be.
        """
        memo = self._catalog_shape_memo.get(package)
        if memo is None or memo[0] is not versions:
            ascending = all(
                earlier <= later for earlier, later in zip(versions, versions[1:])
            )
            has_prerelease = any(version.is_prerelease for version in versions)
            memo = (versions, ascending, has_prerelease)
            self._catalog_shape_memo[package] = memo
        return memo[1], memo[2]

    def _eligible_versions(self, package: str) -> tuple[Version, ...]:
        """Return versions that this provider can actually offer.

        NAB calls ``has_satisfying_version`` while constructing diagnostics.
        Keep that answer consistent with ``choose_version`` instead of
        exposing the raw index catalog.
        """
        requirement = self.requirements[package]
        constraints = self._constraint_for(package)
        versions = self._versions(package)
        if not constraints:
            return tuple(
                version
                for version in versions
                if requirement.specifier.contains(version, allow_prereleases=True)
                and (not version.is_prerelease or self._allows(package, version))
            )
        return tuple(
            version
            for version in versions
            if requirement.specifier.contains(version, allow_prereleases=True)
            and (not version.is_prerelease or self._allows(package, version))
            and all(
                constraint.specifier.contains(version, allow_prereleases=True)
                for constraint in constraints
            )
        )

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        requirement = self.requirements[package]
        constraints = self._constraint_for(package)
        if constraints:
            url_constraints = tuple(
                constraint for constraint in constraints if constraint.url is not None
            )
            constraint_urls = tuple(
                canonical_url(url)
                for constraint in url_constraints
                if (url := constraint.url) is not None
            )
            if len(set(constraint_urls)) > 1:
                return None
            requirement_url = requirement.url
            if (
                requirement_url is not None
                and constraint_urls
                and any(
                    canonical_url(requirement_url) != url for url in constraint_urls
                )
            ):
                return None
        else:
            url_constraints = ()
        candidate_requirement = requirement
        if len(url_constraints) == 1 and requirement.url is None:
            candidate_requirement = url_constraints[0]
        if requirement.url is None and (
            not requirement.specifier.is_pinned
            or not isinstance(self.provider, CandidateProvider)
        ):
            if not isinstance(self.provider, CandidateProvider):
                candidate_requirement = parse_requirement(package)
            else:
                candidate_requirement = self._unpinned(package, requirement)
        versions = self._versions(package)
        if len(url_constraints) == 1 and requirement.url is None:
            constrained_candidates = tuple(
                self.provider.find_candidates(url_constraints[0])
            )
            constrained_versions = {
                candidate.version for candidate in constrained_candidates
            }
            versions = tuple(sorted(set(versions) | constrained_versions))
        # The resolver asks for a package's choice far more often than its
        # range moves: ranking what to decide next re-asks every open
        # package, and a backjump replays the same ranges.  On airflow's
        # graph 98% of the 45,000 calls here repeated a (versions, range)
        # pair already filtered, each a scan of every release.  ``versions``
        # is replaced whenever the requirement changes, so its identity
        # covers everything ``_allows`` reads besides resolve-wide policy.
        memo = self._matching_memo.get(package)
        ascending, has_prerelease = self._catalog_shape(package, versions)
        if memo is not None and memo[0] is versions and memo[1] == version_range:
            matching = memo[2]
        else:
            # A catalog is sorted, so the range is bisected rather than asked
            # about each release: 75,000 membership tests on airflow's graph.
            matching: list[Version]
            if ascending and isinstance(version_range, Range):
                matching = version_range.select_sorted(versions)  # ty: ignore[invalid-argument-type, invalid-assignment]
            else:
                matching = [version for version in versions if version in version_range]
            if has_prerelease and not self._allows_prereleases(package):
                matching = [
                    version for version in matching if not version.is_prerelease
                ]
            self._matching_memo[package] = (versions, version_range, matching)
        control = getattr(self.provider, "release_control", None)
        if (
            has_prerelease
            and not self.allow_prereleases
            and (control is None or control.allows_prereleases(package) is None)
        ):
            stable = [version for version in matching if not version.is_prerelease]
            if stable:
                matching = stable
        if len(url_constraints) == 1 and requirement.url is None:
            matching = [
                version for version in matching if version in constrained_versions
            ]
        if constraints:
            allow_prereleases = self.allow_prereleases
            constraint_contains = [
                constraint.specifier.contains for constraint in constraints
            ]
            constrained = []
            for version in matching:
                for constraint_contain in constraint_contains:
                    if not constraint_contain(
                        version, allow_prereleases=allow_prereleases
                    ):
                        break
                else:
                    constrained.append(version)
            matching = constrained
        if not matching:
            return None
        installed = self._installed_candidate(package)
        preferred = self._preferences.get(package)
        if self.context.upgrade and installed is not None:
            indexed_matching = [
                version for version in matching if version != installed.version
            ]
            installed_is_indexed = bool(
                self.provider.find_candidates(
                    parse_requirement(package),
                    allowed_versions=frozenset({installed.version}),
                )
            )
            if indexed_matching and not installed_is_indexed:
                matching = indexed_matching
        if (
            installed is not None
            and installed.version in matching
            and not self.context.upgrade
        ):
            selected = installed.version
        elif (
            preferred is not None
            and preferred in matching
            and preferred not in self._yanked_versions.get(package, ())
        ):
            # The previous lock's answer, still allowed: keeping it is what
            # keeps an edit to one requirement from moving every other pin,
            # and its metadata is what the last lock already read.
            selected = preferred
        else:
            selected = self._newest_viable(package, matching)
            selected = self._sidestep_yanked(package, selected, matching, constraints)
        if installed is not None and selected == installed.version:
            self.records[(package, selected)] = installed
            return selected
        candidates = self._candidates_for_version(candidate_requirement, selected)
        if not candidates and requirement.url is None:
            candidates = tuple(
                self.provider.find_candidates(
                    self._unpinned(package, requirement),
                    allowed_versions=frozenset({selected}),
                ),
            )
        if not candidates:
            retried = self._retry_including_yanked(
                package,
                selected,
                requirement=self._unpinned(package, requirement),
                matching=matching,
                constraints=constraints,
                version_range=version_range,
            )
            if retried is not None:
                selected, candidates = retried
        if not candidates:
            return None
        candidate = candidates[0]

        if self._invalid_metadata_rejects(candidate):
            print(
                f"WARNING: Ignoring version {candidate.version} of "
                f"{candidate.name} since it has invalid metadata",
                file=sys.stderr,
            )

            alternative = self._alternative_for_invalid_metadata(
                candidate_requirement,
                selected,
                matching=matching,
            )
            if alternative is None:
                return None
            selected, candidate = alternative

        if self._requires_python_rejects(candidate):
            alternative = self._alternative_for_requires_python(
                candidate_requirement,
                selected,
                matching=matching,
            )
            if alternative is None:
                return None
            selected, candidate = alternative

        if self._inconsistent_metadata_rejects(candidate):
            metadata_version = getattr(candidate, "metadata_version", None)

            print(
                f"WARNING: {candidate.name} has an inconsistent version: "
                f"expected '{candidate.version}', but metadata has "
                f"'{metadata_version}'",
            )

            if requirement.extras:
                print(
                    f"Requested {requirement.raw or requirement.name}, "
                    f"but installing version {metadata_version}",
                )

            alternative = self._alternative_for_inconsistent_metadata(
                candidate_requirement,
                selected,
                matching=matching,
            )
            if alternative is None:
                return None
            selected, candidate = alternative

        self.records[(package, selected)] = candidate
        return selected

    def _release_records_for(
        self,
        requirement: Requirement,
        version: Version,
    ) -> tuple[CandidateRecord, ...] | None:
        """One release's records, read from the index at most once.

        Both the forward check and the speculative metadata start want the
        artifacts of a single release, and the second to ask must not cost
        another read of the index.
        """
        key = (requirement.canonical_name, version, requirement.extras)

        if key in self._release_records:
            return self._release_records[key]

        records = self.provider.release_candidates(requirement, version)

        self._release_records[key] = records

        return records

    def _candidates_for_version(
        self,
        requirement: Requirement,
        version: Version,
    ) -> tuple[WheelCandidate, ...]:
        """Materialize one release without rescanning the package catalog."""
        if isinstance(self.provider, CandidateProvider):
            cached = self._catalog_candidate_cache.get(
                (requirement.canonical_name, version),
                _MISSING,
            )
            if not requirement.extras and isinstance(cached, WheelCandidate):
                return (cached,)
            records = self._release_records_for(requirement, version)
            if records is not None:
                materializer = self.provider.get_materializer_internal()
                materialize_one = getattr(materializer, "materialize_one", None)
                if len(records) == 1 and materialize_one is not None:
                    candidate = materialize_one(requirement, records[0])
                    return () if candidate is None else (candidate,)
                return tuple(materializer.materialize(requirement, records))
        return tuple(
            self.provider.find_candidates(
                requirement,
                allowed_versions=frozenset({version}),
            )
        )

    def _newest_viable(self, package: str, matching: list[Version]) -> Version:
        """Pick the newest version not disproved by available dependency facts.

        The resolver has no lookahead: it decides a version, decides its
        dependencies, and only then discovers that two of them pin the same
        package to different releases.  Each such candidate costs a decision
        per dependency plus a conflict, and every conflict leaves behind an
        incompatibility that all later propagation re-scans.  On a wheelhouse
        whose releases disagree pairwise that is quadratic, and the resolver
        spends it before reaching the one release that works.

        Looking one level past the pins is enough to rule those out up front.
        Restores the behavior of ``preflight_exact_dependencies``, which the
        deleted local-wheelhouse kernel ran for exactly this reason.

        A candidate can also be disproved when one of its direct dependencies
        excludes a version already selected in the current partial solution.
        Detecting that here avoids discarding and replaying every unrelated
        decision between the dependency and this late candidate.

        Rejecting a satisfiable version would silently return an older
        solution, so this defers to the resolver on anything it cannot decide
        exactly -- and if it rejects *every* candidate it defers as well,
        rather than claiming a graph is unsolvable on the strength of a
        conservative check.
        """
        if len(matching) == 1:
            # An exact pin lands here every time, so half a descent is these.
            self._prefetch_descent_window(package, matching, 0)
            return matching[0]

        newest_first = sorted(matching, reverse=True)
        check_selected_dependencies = len(
            newest_first
        ) >= _SELECTED_FORWARD_CHECK_MIN_VERSIONS and bool(
            self._active_decisions or self._active_positive_ranges
        )
        check_partial_solution = (
            len(newest_first) >= _TRANSITIVE_FORWARD_CHECK_MIN_VERSIONS
        )
        rejected_any = False
        for index, version in enumerate(newest_first):
            if rejected_any and index % _FORWARD_CHECK_BATCH == 1:
                self._prefetch_catalog_candidates(
                    package,
                    newest_first[index : index + _FORWARD_CHECK_BATCH],
                )
            selected_dependency_rejects = (
                check_selected_dependencies
                and self._selected_dependency_rejects(package, version)
            )
            pins_are_impossible = (
                not selected_dependency_rejects
                and self._pins_are_impossible(package, version)
            )
            partial_solution_rejects = (
                not selected_dependency_rejects
                and not pins_are_impossible
                and check_partial_solution
                and (self._partial_solution_rejects(package, version))
            )
            if (
                not selected_dependency_rejects
                and not pins_are_impossible
                and not partial_solution_rejects
            ):
                self._prefetch_descent_window(package, newest_first, index)
                return version
            rejected_any = True
        return newest_first[0]

    def _catalog_window_below(
        self,
        package: str,
        chosen: Version,
        size: int,
    ) -> tuple[Version, ...]:
        """The next releases below ``chosen`` in the package's own catalog."""
        versions = self._versions(package)
        cached = self._descent_order.get(package)

        if cached is None or cached[0] is not versions:
            ordered = tuple(sorted(versions, reverse=True))
            cached = (versions, ordered, {v: i for i, v in enumerate(ordered)})
            self._descent_order[package] = cached

        _, ordered, positions = cached
        position = positions.get(chosen)

        if position is None:
            return ()

        return ordered[position + 1 : position + 1 + size]

    def _prefetch_descent_window(
        self,
        package: str,
        newest_first: list[Version],
        index: int,
    ) -> None:
        """Start metadata for the releases a backtrack would try next.

        Only a package whose chosen version just dropped is descending.  The
        resolver asks for a package's choice far more often than it decides
        it -- ranking what to decide next re-asks every open package -- so a
        repeat of the same answer, or a pin re-affirmed while merging extras,
        is not a descent.  Counting those opened a window under nearly every
        package of a large graph: airflow's cold lock fetched metadata for
        ~10,000 releases this way and used ~900.
        """
        if _DESCENT_PREFETCH_WINDOW <= 0 or not isinstance(
            self.provider, CandidateProvider
        ):
            return

        chosen = newest_first[index]
        last = self._descent_last.get(package)
        self._descent_last[package] = chosen

        if last is None or chosen >= last:
            return

        attempts = self._descent_attempts.get(package, 0) + 1
        self._descent_attempts[package] = attempts

        size = min(_DESCENT_PREFETCH_WINDOW, 1 << min(attempts, 5))

        try:
            self._start_descent_window(package, newest_first, index, size)

        except Exception:  # noqa: BLE001 - lookahead must not fail a resolve
            # Nothing here is needed for correctness: whatever it would have
            # warmed is fetched on demand by the step that reaches it.
            pass

    def _start_descent_window(
        self,
        package: str,
        newest_first: list[Version],
        index: int,
        size: int,
    ) -> None:
        window = newest_first[index + 1 : index + 1 + size]

        if not window:
            window = self._catalog_window_below(package, newest_first[index], size)

        if not window:
            return

        self._start_metadata_for(package, window)

    def _start_metadata_for(self, package: str, window: Sequence[Version]) -> None:
        """Start metadata for ``window``'s releases, each at most once."""
        requirement = parse_requirement(package)

        # Not _prefetch_catalog_candidates: it materializes a candidate per
        # release and keeps it. Starting metadata needs neither.
        records: list[CandidateRecord] = []

        for version in window:
            key = (package, version)

            if key in self._descent_prefetched:
                continue

            self._descent_prefetched.add(key)

            found = self.provider.release_candidates(requirement, version)

            if found:
                records.extend(found)

        if records:
            self.provider.get_materializer_internal().prefetch_metadata(
                tuple(records),
                requirement=requirement,
            )

    def _prefetch_child_lookahead(
        self, child: str, matching: Sequence[Version]
    ) -> None:
        """Start metadata for the child releases the next parents will ask about.

        The two-hop check walks a root's releases newest first, and each
        release's dependency window sits just below the previous one's: the
        botocore window of boto3 1.43.98 is the window of 1.43.99 shifted
        down a release.  Checking a window fetched only its one or two new
        releases and blocked on them, so a 1,400-release descent was 1,400
        serial round trips.  Starting metadata for a window of the child's
        catalog below the lowest release just checked keeps the next checks
        fed; the window doubles while the descent keeps going, as the
        parent's own descent window does, and every release starts once.
        """
        if _DESCENT_PREFETCH_WINDOW <= 0 or not matching:
            return
        lowest = min(matching)
        last = self._child_lookahead_last.get(child)
        self._child_lookahead_last[child] = lowest
        if last is not None and lowest >= last:
            return  # the same window again, or a higher one: not a descent
        attempts = self._child_lookahead_attempts.get(child, 0) + 1
        self._child_lookahead_attempts[child] = attempts
        size = min(_DESCENT_PREFETCH_WINDOW, 1 << min(attempts, 5))
        # The child's sorted catalog from the check itself, not ``_versions``:
        # the child may not carry a requirement yet, the check reads it
        # straight from the provider.
        versions = self._forward_catalog_versions.get(child)
        if not versions:
            return
        # Extend below what is already started, so the buffer ahead of the
        # descent keeps growing rather than refilling only once it is overrun.
        floor = self._child_lookahead_floor.get(child)
        below = lowest if floor is None or floor > lowest else floor
        stop = bisect_left(versions, below)
        window = versions[max(0, stop - size) : stop]
        if not window:
            return
        self._child_lookahead_floor[child] = window[0]
        try:
            self._start_metadata_for(child, window[::-1])
        except Exception:  # noqa: BLE001 - lookahead must not fail a resolve
            pass

    def _selected_dependency_rejects(
        self,
        package: str,
        version: Version,
    ) -> bool:
        """Whether this wheel excludes a version already selected, or every
        version the partial solution still allows for one of its dependencies.

        This is a one-hop proof only. Unknown metadata and URL dependencies
        remain possible, and rejecting every release makes ``_newest_viable``
        defer to PubGrub so an earlier decision can still be backtracked.

        A decided dependency is tested exactly.  An undecided one whose
        positive range the solution already holds is tested against the
        interval that contains everything the specifier admits (see
        ``_implied_range``): an empty intersection with a wider set is empty
        for the specifier too.  Half of airflow's conflicts were a release
        contradicting a range derived from an earlier decision, which the
        two-hop check only asks about for constrained roots.
        """
        decisions = self._active_decisions
        positive_ranges = self._active_positive_ranges
        if not decisions and not positive_ranges:
            return False
        candidate = self._catalog_candidate(package, version)
        dependencies = None if candidate is None else _dependencies_or_none(candidate)
        if dependencies is None or getattr(candidate, "source_kind", None) != "wheel":
            return False
        extras = self.requirements[package].extras
        for dependency in dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            if dependency.url is not None:
                continue
            dependency_name = _key(dependency)
            selected = decisions.get(dependency_name)
            if selected is not None:
                if not dependency.specifier.contains(
                    selected,
                    allow_prereleases=True,
                ):
                    self._queue_rejection_clause(package, version, dependency)
                    return True
                continue
            active = positive_ranges.get(dependency_name)
            if active is not None and active.is_disjoint(
                _implied_range(dependency.specifier)
            ):
                self._queue_rejection_clause(package, version, dependency)
                return True
        return False

    def _partial_solution_rejects(self, package: str, version: Version) -> bool:
        """Whether active constraints make a candidate transitively impossible.

        This is deliberately a proof of impossibility, not a heuristic.  A
        parent is skipped only when one of its dependency domains contains no
        child release that could coexist with the current positive solution.
        Missing metadata, ambiguous artifacts, URLs, and source candidates all
        remain possible so PubGrub retains authority over uncertain cases.
        """
        if (
            package not in self._root_packages
            or not self._constrained_root_packages
            or not self._active_positive_ranges
            or not isinstance(
                self.provider,
                CandidateProvider,
            )
        ):
            return False

        extras = self.requirements[package].extras
        cache_key = (package, version, extras)
        cached = self._partial_preflight_cache.get(cache_key)
        if cached is not None:
            return cached

        candidate = self._catalog_candidate(package, version)
        dependencies = None if candidate is None else _dependencies_or_none(candidate)
        if dependencies is None or getattr(candidate, "source_kind", None) != "wheel":
            self._partial_preflight_cache[cache_key] = False
            return False

        verdict = False
        for dependency in dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            if dependency.url is not None:
                continue

            dependency_name = _key(dependency)
            active = self._active_constraint_for(dependency_name)
            if active is not None and active.is_disjoint(
                _implied_range(dependency.specifier)
            ):
                verdict = True
                break

            if self._dependency_domain_is_blocked(dependency, active):
                verdict = True
                break

        self._partial_preflight_cache[cache_key] = verdict
        return verdict

    def _active_constraint_for(
        self,
        package: str,
    ) -> RangeProtocol[Version] | None:
        """Return an active range only when it came from a constrained root."""
        if package not in self._constrained_root_packages:
            return None
        return self._active_positive_ranges.get(package)

    def _dependency_domain_is_blocked(
        self,
        dependency: Requirement,
        active: RangeProtocol[Version] | None,
    ) -> bool:
        """Whether every selectable child release conflicts with active ranges."""
        child_name = _key(dependency)
        constraints = self._constraint_for(child_name)
        catalog_versions = self._matching_catalog_versions(dependency)
        if constraints:
            matching = [
                version
                for version in catalog_versions
                if (active is None or version in active)
                and (not version.is_prerelease or self._allows(child_name, version))
                and all(
                    constraint.url is None
                    and constraint.specifier.contains(
                        version,
                        allow_prereleases=True,
                    )
                    for constraint in constraints
                )
            ]
        else:
            matching = [
                version
                for version in catalog_versions
                if (active is None or version in active)
                and (not version.is_prerelease or self._allows(child_name, version))
            ]
        if not matching:
            return False

        self._prefetch_child_lookahead(child_name, matching)

        child_extras = dependency.extras
        for start in range(0, len(matching), _FORWARD_CHECK_BATCH):
            batch = matching[start : start + _FORWARD_CHECK_BATCH]
            self._prefetch_catalog_candidates(child_name, batch)
            for child_version in batch:
                key = (child_name, child_version, child_extras)
                conflicts = self._active_candidate_conflict_cache.get(key)
                if conflicts is None:
                    child = self._catalog_candidate(child_name, child_version)
                    conflicts = self._candidate_conflicts_with_active(
                        child,
                        extras=child_extras,
                    )
                    self._active_candidate_conflict_cache[key] = conflicts
                if not conflicts:
                    return False
        return True

    def _matching_catalog_versions(
        self,
        requirement: Requirement,
    ) -> tuple[Version, ...]:
        """Catalog versions within a specifier, narrowing by bounds first."""
        package = _key(requirement)
        versions = self._forward_catalog_versions.get(package)
        if versions is None:
            try:
                summaries = self.provider.available_versions(requirement)
            except (KpipError, OSError, ValueError):
                return ()
            versions = tuple(sorted({summary.version for summary in summaries}))
            self._forward_catalog_versions[package] = versions

        lower, upper = requirement.specifier.bounds
        start = 0
        stop = len(versions)
        if lower is not None:
            version, inclusive = lower
            start = (
                bisect_left(versions, version)
                if inclusive
                else bisect_right(versions, version)
            )
        if upper is not None:
            version, inclusive = upper
            stop = (
                bisect_right(versions, version)
                if inclusive
                else bisect_left(versions, version)
            )
        return tuple(
            version
            for version in versions[start:stop]
            if requirement.specifier.contains(version, allow_prereleases=True)
        )

    def _candidate_conflicts_with_active(
        self,
        candidate: object | None,
        *,
        extras: frozenset[str],
    ) -> bool:
        """Prove that one child candidate contradicts the positive solution."""
        if candidate is None or getattr(candidate, "source_kind", None) != "wheel":
            return False
        dependencies = _dependencies_or_none(candidate)
        if dependencies is None:
            return False

        for dependency in dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            if dependency.url is not None:
                continue
            active = self._active_constraint_for(_key(dependency))
            if active is not None and active.is_disjoint(
                _implied_range(dependency.specifier)
            ):
                return True
        return False

    def _pins_are_impossible(self, package: str, version: Version) -> bool:
        """Whether this version's ``==`` pins force an empty version domain.

        Conservative by construction: every branch that cannot be decided
        exactly answers ``False`` so the resolver stays authoritative.  Only a
        provable emptiness -- two pins whose dependency requirements share a
        package but no version -- answers ``True``.
        """
        extras = self.requirements[package].extras
        cache_key = (package, version, extras)
        cached = self._preflight_cache.get(cache_key)
        if cached is not None:
            return cached

        verdict = self._compute_pins_are_impossible(package, version, extras)
        self._preflight_cache[cache_key] = verdict
        return verdict

    def _compute_pins_are_impossible(
        self,
        package: str,
        version: Version,
        extras: frozenset[str],
    ) -> bool:
        if not isinstance(self.provider, CandidateProvider):
            return False

        candidate = self._catalog_candidate(package, version)
        if candidate is None:
            return False

        dependencies = _dependencies_or_none(candidate)
        if dependencies is None:
            return False

        pins: list[tuple[Requirement, Version]] = []
        for dependency in dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            if dependency.url is not None:
                return False
            pinned = dependency.specifier.exact_version
            if pinned is None:
                return False
            pins.append((dependency, pinned))

        if len(pins) < _MIN_PINS_TO_DISAGREE:
            return False

        domains: dict[str, Range[Version]] = {}

        for dependency, pinned in pins:
            child = self._catalog_candidate(_key(dependency), pinned)
            if child is None:
                return False

            grandchildren = _dependencies_or_none(child)
            if grandchildren is None:
                return False

            for grandchild in grandchildren:
                if not marker_applies(grandchild.marker, extras=dependency.extras):
                    continue
                if grandchild.url is not None:
                    continue

                name = _key(grandchild)
                implied = _implied_range(grandchild.specifier)
                narrowed = domains.get(name)
                if narrowed is None:
                    narrowed = implied
                else:
                    relation = narrowed.relation(implied)
                    if relation.is_disjoint:
                        return True
                    if relation.is_subset:
                        continue
                    narrowed = narrowed & implied
                if narrowed.is_empty:
                    return True
                domains[name] = narrowed

        return False

    def _catalog_candidate(self, package: str, version: Version) -> object | None:
        """The catalog entry the resolver would materialize for one release.

        The per-release read returns a release's artifacts preferred first,
        the order ``_candidates_for_version`` materializes them in; a decision
        on the release records ``candidates[0]``, so the forward check reads
        the same artifact.  Declining on more than one artifact instead made
        the check blind on a cold run: a freshly parsed catalog offers every
        release's wheel *and* sdist, while a persisted one carries a single
        choice.  boto3 with ``urllib3<1.25.4`` walked 1,438 conflicts cold
        and none warm for exactly that reason.

        One release at a time: the provider reads the release's artifacts
        out of the package catalog, and only the one release inspected is
        materialized. Querying the package by name instead re-evaluated
        every link and materialized every release a second time, beside the
        evaluation the resolver's own requirement already paid for.
        """
        key = (package, version)
        cached = self._catalog_candidate_cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached

        if isinstance(self.provider, CandidateProvider):
            requirement = parse_requirement(package)
            try:
                records = self.provider.release_candidates(requirement, version)
            except (KpipError, OSError, ValueError):
                records = ()
            if records is not None:
                candidate = None
                if records:
                    try:
                        candidate = (
                            self.provider.get_materializer_internal().materialize_one(
                                requirement,
                                records[0],
                            )
                        )
                    except (KpipError, OSError, ValueError):
                        candidate = None
                self._catalog_candidate_cache[key] = candidate
                return candidate

        candidate = self._catalog_by_version(package).get(version)
        self._catalog_candidate_cache[key] = candidate
        return candidate

    def _prefetch_catalog_candidates(
        self,
        package: str,
        versions: list[Version] | tuple[Version, ...],
    ) -> None:
        """Prepare exact wheel releases and start their metadata concurrently."""
        if not isinstance(self.provider, CandidateProvider):
            return

        requirement = parse_requirement(package)
        pending: list[tuple[Version, CandidateRecord]] = []
        for version in versions:
            key = (package, version)
            if key in self._catalog_candidate_cache:
                continue
            try:
                records = self.provider.release_candidates(requirement, version)
            except (KpipError, OSError, ValueError):
                self._catalog_candidate_cache[key] = None
                continue
            if records is None:
                continue
            if not records:
                self._catalog_candidate_cache[key] = None
                continue
            pending.append((version, records[0]))

        if not pending:
            return

        materializer = self.provider.get_materializer_internal()
        records = tuple(record for _, record in pending)
        materializer.prefetch_metadata(records, requirement=requirement)
        for version, record in pending:
            try:
                candidate = materializer.materialize_one(requirement, record)
            except (KpipError, OSError, ValueError):
                candidate = None
            self._catalog_candidate_cache[(package, version)] = candidate

    def _catalog_by_version(self, package: str) -> dict[Version, object | None]:
        """Index every release of a package the provider has no catalog for.

        The fallback behind :meth:`_catalog_candidate`: one full query per
        package, materializing every release, for the cases the per-release
        read declines (a direct URL, an upload cutoff, required hashes).
        """
        cached = self._catalog_by_version_cache.get(package)
        if cached is not None:
            return cached

        try:
            found = tuple(self.provider.find_candidates(parse_requirement(package)))
        except (KpipError, OSError, ValueError):
            found = ()

        index: dict[Version, object | None] = {}
        for candidate in found:
            version = candidate.version
            index[version] = None if version in index else candidate

        self._catalog_by_version_cache[package] = index
        return index

    def _sidestep_yanked(
        self,
        package: str,
        selected: Version,
        matching: list[Version],
        constraints: tuple[Requirement, ...],
    ) -> Version:
        """The version ``_retry_including_yanked`` would settle on, up front.

        A yanked release has no admissible artifact under the default policy,
        so choosing it costs a failed materialization, a catalog rescan with
        yanked releases admitted, and a second rescan for the answer.  The
        catalog summary already says which releases are yanked, and the retry
        picks the newest unyanked release in range that the constraints
        admit, so pick that here and skip the rescans.  Same answer: on
        airflow's graph this replaces 429 of 431 retries, and the retry stays
        for a provider whose summaries carry no yanked flag.  A release that
        is the only one left in range is still offered, as before.

        The unyanked release is chosen the way ``selected`` was, through the
        forward check, not as the newest in range: taking the newest threw
        the check's verdict away whenever it had landed on a yanked release,
        and on airflow's graph snowflake-snowpark-python's 1.46.0 is yanked,
        so its 47 newer releases were each decided, conflicted with the
        cloudpickle already chosen, and backjumped 425 decisions.
        """
        yanked = self._yanked_versions.get(package)
        if not yanked or selected not in yanked:
            return selected
        usable = [
            version
            for version in matching
            if version not in yanked
            and all(
                constraint.url is None
                and constraint.specifier.contains(version, allow_prereleases=True)
                for constraint in constraints
            )
        ]
        if not usable:
            return selected
        if len(usable) == 1:
            return usable[0]
        return self._newest_viable(package, usable)

    def _unpinned(self, package: str, requirement: Requirement) -> Requirement:
        """``requirement`` with its specifier dropped and its extras kept.

        The stored requirement is whichever the resolver registered last and
        is not undone on backtrack, so its specifier can be stale against the
        live range; a catalog scan must not be narrowed by it.  Its extras
        are what the resolver asked for, and a scan that drops them hands
        back candidates whose dependencies omit the extras entirely.
        """
        memo = self._unpinned_requirements.get(package)
        if memo is None or memo[0] is not requirement:
            memo = (
                requirement,
                Requirement(
                    name=requirement.name,
                    specifier=SpecifierSet(),
                    extras=requirement.extras,
                    marker=requirement.marker,
                    raw=requirement.raw,
                ),
            )
            self._unpinned_requirements[package] = memo
        return memo[1]

    def _retry_including_yanked(
        self,
        package: str,
        selected: Version,
        *,
        requirement: Requirement,
        matching: list[Version],
        constraints: tuple[Requirement, ...],
        version_range: RangeProtocol[Version],
    ) -> tuple[Version, tuple[WheelCandidate, ...]] | None:
        """Look again with yanked releases admitted, or ``None`` to give up.

        ``requirement`` carries the extras the resolver asked for.  Scanning
        with a bare name handed back candidates whose dependencies omitted
        every extra, so a release chosen here settled into the solution
        without the extra's dependencies ever being resolved.

        Reached only when the active policy offered no artifact for a version
        the resolver already selected.  A release can be absent because every
        artifact for it is yanked, and admitting those makes the package
        resolvable again; an unyanked release found this way is preferred.

        There is nothing to relax when the provider already admits yanked
        releases, so that case declines rather than widening the search.
        """
        if not isinstance(self.provider, CandidateProvider):
            return None
        if self.provider.allow_yanked:
            return None

        fallback_provider = self.provider.with_yanked_policy(True)
        fallback = tuple(fallback_provider.find_candidates(requirement))
        usable = [
            item
            for item in fallback
            if item.version in version_range
            and item.version in matching
            and all(
                constraint.specifier.contains(
                    item.version,
                    allow_prereleases=True,
                )
                for constraint in constraints
            )
            and item.yanked_reason is None
        ]
        if usable:
            candidate = max(usable, key=lambda item: item.version)
            return candidate.version, (candidate,)

        return selected, tuple(
            fallback_provider.find_candidates(
                requirement,
                allowed_versions=frozenset({selected}),
            ),
        )

    def _invalid_metadata_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether the candidate's metadata fails to load at all.

        A malformed artifact -- e.g. one of its own dependencies declaring an
        unparseable version -- surfaces as an exception the first time
        metadata is read (accessing any of ``dependencies``,
        ``requires_python``, or ``provided_extras`` triggers the same lazy
        load). Treat that the same way materialization already does: skip
        the candidate and let the resolver fall back to the next release.
        """
        try:
            getattr(candidate, "dependencies", None)
        except (OSError, ValueError):
            return True
        return False

    def _alternative_for_invalid_metadata(
        self,
        candidate_requirement: Requirement,
        selected: Version,
        *,
        matching: list[Version],
    ) -> tuple[Version, WheelCandidate] | None:
        """Walk back to the newest release whose metadata actually loads."""
        for fallback in sorted(matching, reverse=True):
            if fallback == selected:
                continue
            alternatives = tuple(
                self.provider.find_candidates(
                    candidate_requirement,
                    allowed_versions=frozenset({fallback}),
                )
            )
            if alternatives and not self._invalid_metadata_rejects(alternatives[0]):
                return fallback, alternatives[0]
        return None

    def _requires_python_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether the targeted interpreter falls outside Requires-Python.

        When the caller targets another interpreter the declaration is read
        against *that* version rather than ignored: a release that excludes
        the target is no more installable there than one that excludes this
        interpreter, and skipping the check is how a cross-version resolve
        ends up pinning a release the target cannot run.
        """
        try:
            requires_python = getattr(candidate, "requires_python", None)
        except (OSError, ValueError):
            return True

        if self.ignore_requires_python or not requires_python:
            return False

        try:
            return not CandidateEvaluator.requires_python_matches(
                requires_python,
                self.python_version,
            )
        except ValueError:
            return True

    def _alternative_for_requires_python(
        self,
        candidate_requirement: Requirement,
        selected: Version,
        *,
        matching: list[Version],
    ) -> tuple[Version, WheelCandidate] | None:
        """Walk back to the newest release this interpreter can install."""
        for fallback in sorted(matching, reverse=True):
            if fallback == selected:
                continue
            alternatives = tuple(
                self.provider.find_candidates(
                    candidate_requirement,
                    allowed_versions=frozenset({fallback}),
                )
            )
            if alternatives and not self._requires_python_rejects(alternatives[0]):
                return fallback, alternatives[0]
        return None

    def _inconsistent_metadata_rejects(self, candidate: WheelCandidate) -> bool:
        """Whether the candidate's own metadata contradicts its declared version.

        A filename can claim one release while the wheel's METADATA declares
        another (a malformed or mislabeled artifact). Installing it would
        silently give the user something other than what they asked for, so
        treat it the same as an incompatible interpreter: reject it and let
        the resolver fall back to the next candidate.
        """
        declared_version = getattr(candidate, "version", None)
        if declared_version is None or declared_version == ZERO_VERSION:
            return False

        metadata_version = getattr(candidate, "metadata_version", None)
        if metadata_version is None:
            return False

        return metadata_version != declared_version

    def _alternative_for_inconsistent_metadata(
        self,
        candidate_requirement: Requirement,
        selected: Version,
        *,
        matching: list[Version],
    ) -> tuple[Version, WheelCandidate] | None:
        """Walk back to the newest release whose metadata matches its filename."""
        for fallback in sorted(matching, reverse=True):
            if fallback == selected:
                continue
            alternatives = tuple(
                self.provider.find_candidates(
                    candidate_requirement,
                    allowed_versions=frozenset({fallback}),
                )
            )
            if alternatives and not self._inconsistent_metadata_rejects(
                alternatives[0],
            ):
                return fallback, alternatives[0]
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        return any(
            version in version_range for version in self._eligible_versions(package)
        )

    def get_dependencies(
        self, package: str, version: Version
    ) -> Mapping[str, Range[Version]]:
        if self.no_deps:
            return {}
        cache_key = (package, version, self.requirements[package].extras)
        cached = self._dependency_cache.get(cache_key)
        if cached is not None:
            return cached
        record = self.records.get((package, version))
        if record is None:
            raise RuntimeError(
                f"NAB requested dependencies for unselected candidate {package}=={version}"
            )
        record_dependencies = record.dependencies
        if not record_dependencies:
            result: dict[str, Range[Version]] = {}
            self._dependency_cache[cache_key] = result
            return result
        while True:
            # The requirement changes only between passes of this loop.
            extras = self.requirements[package].extras
            normalized_dependencies = []
            for dependency in record_dependencies:
                if not marker_applies(dependency.marker, extras=extras):
                    continue
                if dependency.name.startswith(("file://", "http://", "https://")):
                    name = urlsplit(dependency.name).path.rstrip("/").rsplit("/", 1)[-1]
                    dependency = Requirement(
                        name=name or dependency.name,
                        specifier=dependency.specifier,
                        extras=dependency.extras,
                        url=dependency.url or dependency.name,
                        marker=dependency.marker,
                        raw=dependency.raw,
                    )
                normalized_dependencies.append(dependency)
            dependencies_records = tuple(normalized_dependencies)
            self_dependencies = [
                dependency
                for dependency in dependencies_records
                if _key(dependency) == package
            ]
            if not self_dependencies:
                break
            if any(
                dependency.url is None
                and not dependency.specifier.contains(
                    version,
                    allow_prereleases=True,
                )
                for dependency in self_dependencies
            ):
                result = {package: Range.empty()}
                self._dependency_cache[cache_key] = result
                return result
            merged_extras = frozenset(
                self.requirements[package].extras
                | frozenset(
                    extra
                    for dependency in self_dependencies
                    for extra in dependency.extras
                )
            )
            current = self.requirements[package]
            if merged_extras == current.extras:
                break
            self.requirements[package] = Requirement(
                name=current.name,
                specifier=current.specifier,
                extras=merged_extras,
                url=current.url,
                marker=current.marker,
                raw=current.raw,
            )
            selected = self.choose_version(package, Range.singleton(version))
            if selected != version:
                result = {package: Range.empty()}
                cache_key = (package, version, merged_extras)
                self._dependency_cache[cache_key] = result
                return result
            record = self.records[(package, version)]
            record_dependencies = record.dependencies
        cache_key = (package, version, self.requirements[package].extras)
        prefetch_dependencies = dependencies_records
        if self_dependencies:
            prefetch_dependencies = tuple(
                dependency
                for dependency in dependencies_records
                if _key(dependency) != package
            )
        self._prefetch_available_versions(prefetch_dependencies)
        dependencies: dict[str, Range[Version]] = {}
        for dependency in dependencies_records:
            if dependency.url is None and dependency.name.startswith(
                ("file://", "http://", "https://")
            ):
                dependency = Requirement(
                    name=dependency.name,
                    specifier=dependency.specifier,
                    extras=dependency.extras,
                    url=dependency.name,
                    marker=dependency.marker,
                    raw=dependency.raw,
                )
            dependency_key = _key(dependency)
            self.display_requirements.setdefault(dependency_key, dependency)
            if dependency.url is not None and dependency.name.startswith(
                ("file://", "http://", "https://")
            ):
                name = url_name(dependency.url)
                if name is None and dependency.raw and "@" in dependency.raw:
                    raw_name = dependency.raw.split("@", 1)[0].strip()
                    if raw_name and not raw_name.startswith(
                        ("file://", "http://", "https://")
                    ):
                        name = raw_name
                if name is None:
                    name = dependency.url.rstrip("/").rsplit("/", 1)[-1]
                if name is None:
                    try:
                        candidates = tuple(
                            self.provider.find_candidates(
                                dependency,
                                allowed_versions=None,
                            ),
                        )
                    except (AttributeError, TypeError):
                        candidates = ()
                    if candidates:
                        name = candidates[0].name
                if name:
                    dependency = Requirement(
                        name=name,
                        specifier=dependency.specifier,
                        extras=dependency.extras,
                        url=dependency.url,
                        marker=dependency.marker,
                        raw=dependency.raw,
                    )
                    dependency_key = _key(dependency)
            dependency_constraints = self._constraint_for(dependency_key)
            dependency_url = dependency.url
            if dependency_constraints:
                dependency_url = next(
                    (
                        constraint.url
                        for constraint in dependency_constraints
                        if constraint.url is not None
                    ),
                    dependency.url,
                )
            if dependency_url != dependency.url:
                dependency = Requirement(
                    name=dependency.name,
                    specifier=dependency.specifier,
                    extras=dependency.extras,
                    url=dependency_url,
                    marker=dependency.marker,
                    raw=dependency.raw,
                )
            existing_requirement = self.requirements.get(dependency_key)
            if (
                existing_requirement is not None
                and existing_requirement.url is not None
                and dependency.url is not None
                and canonical_url(existing_requirement.url)
                != canonical_url(dependency.url)
            ):
                dependencies[dependency_key] = Range.empty()
                continue
            if existing_requirement is None:
                self.requirements[dependency_key] = dependency
            elif dependency.extras - existing_requirement.extras:
                self.requirements[dependency_key] = Requirement(
                    name=existing_requirement.name,
                    specifier=existing_requirement.specifier,
                    extras=frozenset(existing_requirement.extras | dependency.extras),
                    url=existing_requirement.url,
                    marker=existing_requirement.marker,
                    raw=existing_requirement.raw,
                )
                selected_dependency_version = self._active_decisions.get(dependency_key)
                if selected_dependency_version is not None:
                    self.choose_version(
                        dependency_key,
                        Range.singleton(selected_dependency_version),
                    )
                    self._dependency_invalidations.setdefault(dependency_key, None)
            pinned = dependency.specifier.exact_version
            if pinned is not None:
                dependency_range = Range.singleton(pinned)
                previous = dependencies.get(dependency_key)
                dependencies[dependency_key] = (
                    dependency_range
                    if previous is None
                    else previous & dependency_range
                )
                continue
            dependency_range = self._edge_range(
                dependency_key, dependency, dependency_constraints
            )
            previous = dependencies.get(dependency_key)
            dependencies[dependency_key] = (
                dependency_range if previous is None else previous & dependency_range
            )
        result = dict(dependencies)
        self._dependency_cache[cache_key] = result
        return result

    def _edge_range(
        self,
        dependency_key: str,
        dependency: Requirement,
        dependency_constraints: tuple[Requirement, ...],
    ) -> Range[Version]:
        """The releases of ``dependency_key`` one edge admits, as a range.

        Free of side effects: ``get_dependencies`` registers and merges the
        requirement before calling this, and a rejection clause for a release
        the resolver never decides must not.
        """
        allowed = self._versions(dependency_key)
        # The same specifier on the same catalog recurs across parents
        # and across re-decisions of one parent; each evaluation scans
        # every release.  ``allowed`` is replaced when the dependency's
        # requirement changes, so its identity covers what the scan reads.
        memo_key = (dependency_key, dependency.specifier.text)
        memo = self._dependency_range_memo.get(memo_key)
        if (
            memo is not None
            and memo[0] is allowed
            and memo[1] == dependency_constraints
        ):
            return memo[2]
        selected = self._selected_by_order(
            dependency_key, allowed, dependency, dependency_constraints
        )
        if selected is not None:
            dependency_range = self._finite_range(selected)
            self._dependency_range_memo[memo_key] = (
                allowed,
                dependency_constraints,
                dependency_range,
            )
            return dependency_range
        window = self._bounded_versions(dependency_key, allowed, dependency.specifier)
        contains = dependency.specifier.contains
        if dependency_constraints:
            # A loop rather than ``all()`` over a generator, which would build
            # a generator for every release in the window.
            constraint_contains = [
                constraint.specifier.contains for constraint in dependency_constraints
            ]
            selected = []
            for candidate in window:
                if not contains(candidate, allow_prereleases=True):
                    continue
                for constraint_contain in constraint_contains:
                    if not constraint_contain(candidate, allow_prereleases=True):
                        break
                else:
                    selected.append(candidate)
        else:
            selected = [
                candidate
                for candidate in window
                if contains(candidate, allow_prereleases=True)
            ]
        dependency_range = self._finite_range(selected)
        self._dependency_range_memo[memo_key] = (
            allowed,
            dependency_constraints,
            dependency_range,
        )
        return dependency_range

    def _queue_rejection_clause(
        self, package: str, version: Version, dependency: Requirement
    ) -> None:
        """Hand the resolver the dependency fact a rejection rests on.

        The forward check rejects a release, and the resolver, told only
        which release to decide instead, learns the same fact one conflict
        at a time when a backjump brings it back to that package: snowflake
        snowpark-python's 47 newest releases each require cloudpickle<=3.1.1,
        and airflow's warm lock decided each of them, conflicted with the
        cloudpickle already chosen, and backjumped 425 decisions -- 47 times
        -- before the accumulated clauses moved cloudpickle.  The clause a
        decision on the release would add, ``{release, not dependency in
        range}``, is queued instead and absorbed before the next decision;
        the clause index merges it with its neighbours' into one clause over
        the whole run of releases, which then propagates in one step.
        """
        key = (package, version, _key(dependency))
        if key in self._rejection_clauses_queued:
            return
        self._rejection_clauses_queued.add(key)
        dependency_key = _key(dependency)
        if dependency_key not in self.requirements or dependency.url is not None:
            return
        pinned = dependency.specifier.exact_version
        if pinned is not None:
            dependency_range: Range[Version] = Range.singleton(pinned)
        else:
            dependency_range = self._edge_range(
                dependency_key, dependency, self._constraint_for(dependency_key)
            )
        self._pending_clauses.append(
            Incompatibility(
                [
                    Term(package, Range.singleton(version), positive=True),
                    Term(dependency_key, dependency_range, positive=False),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
        )

    def _sorted_catalog(
        self, package: str, allowed: tuple[Version, ...]
    ) -> list[Version]:
        """``allowed`` in order, kept while ``allowed`` is the same tuple."""
        memo = self._sorted_versions_memo.get(package)
        if memo is None or memo[0] is not allowed:
            memo = (allowed, sorted(allowed))
            self._sorted_versions_memo[package] = memo
        return memo[1]

    def _selected_by_order(
        self,
        package: str,
        allowed: tuple[Version, ...],
        dependency: Requirement,
        constraints: tuple[Requirement, ...],
    ) -> list[Version] | None:
        """The releases of ``allowed`` an edge admits, found by bisection.

        Each specifier admits runs of the sorted catalog
        (``SpecifierSet.select_indices``), so a resolve asks ``contains`` of
        the releases at their edges instead of every release in the window:
        airflow's asked it 58,000 times. None for a specifier that compares
        spellings, which is left to ``contains``.
        """
        ordered = self._sorted_catalog(package, allowed)
        runs = dependency.specifier.select_indices(ordered)
        for constraint in constraints or ():
            if runs is None or not runs:
                break
            more = constraint.specifier.select_indices(ordered)
            runs = None if more is None else intersect_runs(runs, more)
        if runs is None:
            return None
        selected: list[Version] = []
        for start, stop in runs:
            selected.extend(ordered[start:stop])
        return selected

    def _bounded_versions(
        self,
        package: str,
        allowed: tuple[Version, ...],
        specifier: SpecifierSet,
    ) -> Sequence[Version]:
        """The releases of ``allowed`` a specifier can admit, by bisection.

        A dependency edge scanned every release of its target with
        ``contains``: on boto3's graph each of 1,400 boto3 releases names
        a different botocore window, so the memo above never hit and each
        edge cost 1,900 containment checks for the 20 it admits.  The
        specifier's conservative bounds cut a sorted copy of the catalog
        down to the window that can satisfy it; ``contains`` still decides
        within the window, so ``!=``, wildcards and pre-release rules are
        untouched.  ``allowed`` is replaced, never mutated, when the
        requirement changes, so its identity keys the sorted copy.
        """
        lower, upper = specifier.bounds
        if lower is None and upper is None:
            return allowed
        ordered = self._sorted_catalog(package, allowed)
        start = 0
        stop = len(ordered)
        if lower is not None:
            version, inclusive = lower
            start = (
                bisect_left(ordered, version)
                if inclusive
                else bisect_right(ordered, version)
            )
        if upper is not None:
            version, inclusive = upper
            stop = (
                bisect_right(ordered, version)
                if inclusive
                else bisect_left(ordered, version)
            )
            # ``==V`` and ``<=V`` admit V's local versions, which sort just
            # past V; ``bounds`` reads V itself as the edge, so take them in.
            public = version[:3]  # epoch, release, suffix: everything but local
            count = len(ordered)
            while stop < count and ordered[stop][:3] == public:
                stop += 1
        return ordered[start:stop]

    @staticmethod
    def _finite_range(versions: tuple[Version, ...] | list[Version]) -> Range[Version]:
        """A range holding exactly ``versions``, built in one pass.

        Unioning singletons one at a time re-sorts and re-merges the whole
        interval list on every step, so a package with many releases pays
        O(n^2 log n) to describe its own catalog -- and this runs for every
        dependency edge.  Distinct versions give disjoint, non-touching
        singletons, so sorting once produces exactly what ``Range`` wants.
        """
        return Range.from_versions(versions)

    def begin_decision_scan(self) -> None:
        return None

    def consume_priority_invalidations(self) -> list[str]:
        """Report packages whose priority may have moved, and reset.

        ``is_ready`` is constant here, so a package's priority moves only
        with its requirement -- which is replaced, never mutated in place.
        """
        touched = self.requirements.touched
        if not touched:
            return []
        self.requirements.touched = set()
        return list(touched)

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int, str]:
        conflicts = conflict_counts.get(package, 0)
        requirement = self.requirements[package]
        memo = self._priority_memo.get(package)
        if memo is not None and memo[0] is requirement and memo[1] == conflicts:
            return memo[2]

        # A package that has already caused a backjump is more valuable than
        # an unrelated package with a smaller catalog.  Keeping catalog size
        # first makes a deep backjump replay every one-release package before
        # returning to the decision that can actually advance the solve.
        priority = (-conflicts, len(self._versions(package)), package)
        self._priority_memo[package] = (requirement, conflicts, priority)
        return priority

    def is_ready(self, package: str) -> bool:
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        # The resolver supplies pinned, read-only snapshots. Retain those
        # views directly: materializing them here routed every key through
        # Python-level Mapping iteration and dominated marker-heavy resolves.
        self._active_positive_ranges = positive_ranges
        self._active_decisions = decisions
        self._partial_preflight_cache.clear()
        self._active_candidate_conflict_cache.clear()
        self._start_undecided_source_metadata(positive_ranges, decisions)

    def _start_undecided_source_metadata(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        """Begin metadata for source distributions the solve already needs.

        A package with a positive range is one the solve has committed to
        including, so reading its metadata is work that will be needed
        whatever version wins. For a source distribution that reading is a
        build, and the resolver asks for builds strictly one at a time:
        starting them from here is what lets more than one run at once.

        Only a range that has come down to a single release is started, so
        nothing is built for a version the solve is still choosing between.

        The hint arrives on every propagation round, so reading it is only
        worth doing for a resolve that pays for builds at all.
        """
        if not isinstance(self.provider, CandidateProvider):
            return

        materializer = self.provider.get_materializer_internal()

        # Nothing is looked for until the resolve has actually had to read a
        # source distribution the hard way. A graph served entirely by wheels
        # never blocks on a build, and scanning its frontier for builds to
        # start cost half the resolve.
        if not materializer.prepares_source_metadata:
            return

        for package, positive_range in positive_ranges.items():
            if package in decisions or package in self._source_metadata_started:
                continue

            requirement = self.requirements.get(package)

            if requirement is None:
                continue

            matching = [
                version
                for version in self._versions(package)
                if version in positive_range
            ]

            if len(matching) != 1:
                continue

            self._source_metadata_started.add(package)

            try:
                records = self._release_records_for(requirement, matching[0])

            except (KpipError, OSError, ValueError):
                continue

            if not records:
                continue

            materializer.start_source_metadata(records[0], requirement)

    def consume_dependency_invalidations(self) -> list[str]:
        """Return selected packages whose extras changed after expansion."""
        invalidated = list(self._dependency_invalidations)
        self._dependency_invalidations.clear()
        return invalidated

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        if not self._pending_clauses:
            return []
        clauses = self._pending_clauses
        self._pending_clauses = []
        return clauses

    def consume_force_backtrack_targets(self) -> list[str]:
        return []

    def widen_decision(self, package: str, version: Version) -> Range[Version] | None:
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[Version]
    ) -> RangeProtocol[Version]:
        return constraint

    def add_root(self, requirement: Requirement) -> tuple[str, Range[Version]]:
        if requirement.name.startswith(("file://", "http://", "https://")):
            name = urlsplit(requirement.name).path.rstrip("/").rsplit("/", 1)[-1]
            requirement = Requirement(
                name=name or requirement.name,
                specifier=requirement.specifier,
                extras=requirement.extras,
                url=requirement.url or requirement.name,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        package = _key(requirement)
        previous = self.requirements.get(package)
        if (
            previous is not None
            and previous.url is not None
            and requirement.url is not None
            and canonical_url(previous.url) != canonical_url(requirement.url)
        ):
            return package, Range.empty()
        if previous is not None and previous.extras != requirement.extras:
            requirement = Requirement(
                name=requirement.name,
                specifier=requirement.specifier,
                extras=frozenset(previous.extras | requirement.extras),
                url=requirement.url or previous.url,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        elif (
            previous is not None
            and previous.url is not None
            and requirement.url is None
        ):
            requirement = Requirement(
                name=requirement.name,
                specifier=requirement.specifier,
                extras=frozenset(previous.extras | requirement.extras),
                url=previous.url,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        self.requirements[package] = requirement
        versions = self._eligible_versions(package)
        return package, self._finite_range(versions)

    def add_roots(self, requirements: list[Requirement]) -> dict[str, Range[Version]]:
        """Register roots without speculatively materializing their candidates."""
        merged = list(requirements)
        conflict = _conflicting_exact_root(merged)
        if conflict is not None:
            package, requirement = conflict
            self.requirements[package] = requirement
            self._root_packages = {package}
            self._constrained_root_packages = {package}
            return {package: Range.empty()}

        if self._preferences and isinstance(self.provider, CandidateProvider):
            # Before any prefetch starts: a root's worker warms the release
            # the resolve will choose only if it can already see the pins.
            self.provider.preferred_versions = self._preferences
        self._prefetch_available_versions(tuple(merged))
        self._prefetch_preferred_catalogs()
        roots: dict[str, Range[Version]] = {}
        for requirement in merged:
            package, requirement_range = self.add_root(requirement)
            existing = roots.get(package)
            roots[package] = (
                requirement_range if existing is None else existing & requirement_range
            )
        self._root_packages = set(roots)
        self._constrained_root_packages = {
            _key(requirement)
            for requirement in merged
            if requirement.url is not None
            or bool(requirement.specifier.text)
            or bool(self._constraint_for(_key(requirement)))
        }
        return roots
