"""pip's resolver, solved by nab-resolver.

pip's resolvelib layer -- its factory, candidates, requirements, provider and
error reporting -- is kept as pip has it; this replaces only resolvelib's
``Resolver``, with nab's PubGrub solver (``kpip._vendor.nab_resolver``).
``NabResolver`` asks pip's ``PipProvider`` what resolvelib would ask it and
hands the problem to nab: each of pip's identifiers is a nab package, and
each of pip's requirements the range of the versions ``find_matches`` gives
for it alone. It answers as resolvelib does -- a ``Result`` of the chosen
candidates and their graph, or ``ResolutionImpossible`` naming the
requirements in conflict -- so pip turns either into what it reports.
"""

from __future__ import annotations

import functools
import logging
import operator
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from kpip._vendor.nab_resolver.errors import ResolutionError as NabResolutionError
from kpip._vendor.nab_resolver.ranges import Range
from kpip._vendor.nab_resolver.resolver import BaseProvider
from kpip._vendor.nab_resolver.resolver import Resolver as NabSolver
from kpip._vendor.nab_resolver.types import RootRequirement
from kpip._vendor.packaging.version import Version
from kpip._vendor.resolvelib import (
    BaseReporter,
    ResolutionImpossible,
    ResolutionTooDeep,
)
from kpip._vendor.resolvelib.resolvers import RequirementInformation, Result
from kpip._vendor.resolvelib.structs import DirectedGraph

from kpip._internal.exceptions import MetadataInvalid
from kpip._internal.resolution.resolvelib.candidates import (
    REQUIRES_PYTHON_IDENTIFIER,
)
from kpip._internal.resolution.resolvelib.found_candidates import FoundCandidates

logger = logging.getLogger(__name__)


def _base_identifier(identifier: str) -> str:
    return identifier.partition("[")[0]


def _unbuilt(
    matches: Iterable[Any],
) -> list[tuple[Any, Callable[[], Any], bool]]:
    """Each version ``find_matches`` offers, with how to build its candidate
    and whether it is the installed one pip keeps first, none built.

    Building a candidate prepares it -- a metadata download, or a build -- so
    resolvelib takes pip's ``FoundCandidates`` lazily; this reads the
    versions it would yield, in its ``__iter__``'s order, without building
    any.
    """
    if not isinstance(matches, FoundCandidates):
        return [
            (candidate.version, lambda c=candidate: c, False) for candidate in matches
        ]
    entries = {}
    for version, build in matches._get_infos():
        entries.setdefault(version, (version, build, False))
    installed = matches._installed
    if installed is not None:
        entries[installed.version] = (
            installed.version,
            lambda: installed,
            matches._prefers_installed,
        )
    return list(entries.values())


class _NabProvider(BaseProvider[str, Version]):
    """pip's provider, as nab asks it.

    A requirement's range holds the versions pip's ``find_matches`` offers
    for that requirement alone, so nab's intersection of ranges is pip's
    intersection of requirements. A version's candidate is the one pip ranks
    first for it, a direct requirement's own over the index's, and is built
    only when nab is about to choose it.
    """

    def __init__(self, provider: Any, reporter: BaseReporter[Any, Any, Any]) -> None:
        self._provider = provider
        self._reporter = reporter
        # Every requirement seen on each identifier, with what required it.
        self.information: dict[str, list[RequirementInformation[Any, Any]]] = {}
        # Per identifier: version -> (group, build). pip's order is a direct
        # requirement's own candidate (group 0), then an installed version
        # it keeps (1), then the rest (2); newest first within each.
        self._pool: dict[str, dict[Version, tuple[int, Any]]] = {}
        # Per identifier: its pool's versions, best first; rebuilt on change.
        self._order: dict[str, list[Version]] = {}
        # Requirements compare by what they say; on a package without
        # extras, equal ones have the same range.
        self._ranges_by_content: dict[tuple[str, Any], Range[Version]] = {}
        self._built: dict[tuple[str, Version], Any] = {}
        # Keyed by id(): requirements are kept alive in _requirements.
        self._ranges: dict[int, Range[Version]] = {}
        self._requirements: list[Any] = []
        self._dependencies: dict[tuple[str, Version], dict[str, Range[Version]]] = {}

    def require(self, requirement: Any, parent: Any) -> tuple[str, Range[Version]]:
        """Record ``requirement`` and return its identifier and range."""
        identifier = self._provider.identify(requirement)
        self.information.setdefault(identifier, []).append(
            RequirementInformation(requirement, parent)
        )
        self._reporter.adding_requirement(requirement, parent)
        return identifier, self._range(identifier, requirement)

    def _range(self, identifier: str, requirement: Any) -> Range[Version]:
        cached = self._ranges.get(id(requirement))
        if cached is not None:
            return cached
        base = _base_identifier(identifier)
        if base == identifier:
            cached = self._ranges_by_content.get((identifier, requirement))
            if cached is not None:
                self._ranges[id(requirement)] = cached
                self._requirements.append(requirement)
                return cached
        requirements = {
            base: [info.requirement for info in self.information.get(base, ())],
            identifier: [requirement],
        }
        matches = self._provider.find_matches(
            identifier,
            {key: iter(value) for key, value in requirements.items()},
            {identifier: iter(())},
        )
        explicit = requirement.get_candidate_lookup()[0] is not None
        pool = self._pool.setdefault(identifier, {})
        versions = []
        for version, build, keep_installed in _unbuilt(matches):
            group = 0 if explicit else 1 if keep_installed else 2
            known = pool.get(version)
            if known is None or group < known[0]:
                pool[version] = (group, build)
                self._order.pop(identifier, None)
            versions.append(version)
        found = Range.from_versions(versions)
        self._ranges[id(requirement)] = found
        self._requirements.append(requirement)
        if base == identifier:
            self._ranges_by_content[identifier, requirement] = found
        return found

    def _build(self, identifier: str, version: Version) -> Any:
        """The candidate for ``version``, built once; None, and the version
        dropped, when its metadata is invalid, as ``FoundCandidates`` skips it."""
        key = (identifier, version)
        if key in self._built:
            return self._built[key]
        try:
            candidate = self._pool[identifier][version][1]()
        except MetadataInvalid as e:
            logger.warning(
                "Ignoring version %s of %s since it has invalid metadata:\n"
                "%s\n"
                "Please use pip<24.1 if you need to use this version.",
                version,
                e.ireq.name,
                e,
            )
            candidate = None
        self._built[key] = candidate
        if candidate is None:
            del self._pool[identifier][version]
            self._order.pop(identifier, None)
        return candidate

    def range_of(self, requirement: Any) -> Range[Version]:
        return self._ranges[id(requirement)]

    def candidate(self, identifier: str, version: Version) -> Any:
        return self._built[identifier, version]

    def _ordered(self, package: str) -> list[Version]:
        order = self._order.get(package)
        if order is None:
            pool = self._pool.get(package, {})
            newest_first = sorted(pool, reverse=True)
            order = self._order[package] = sorted(
                newest_first, key=lambda v: pool[v][0]
            )
        return order

    def choose_version(
        self, package: str, version_range: Range[Version]
    ) -> Version | None:
        # pip's best version in the range that builds. Building can drop a
        # version from the pool, and with it this order: walk a copy.
        for version in list(self._ordered(package)):
            if version not in version_range:
                continue
            candidate = self._build(package, version)
            if candidate is not None:
                self._reporter.pinning(candidate)
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: Range[Version]
    ) -> bool:
        return any(version in version_range for version in self._pool.get(package, {}))

    def get_dependencies(
        self, package: str, version: Version
    ) -> Mapping[str, Range[Version]]:
        known = self._dependencies.get((package, version))
        if known is not None:
            return known
        candidate = self.candidate(package, version)
        dependencies: dict[str, Range[Version]] = {}
        for requirement in self._provider.get_dependencies(candidate):
            identifier, found = self.require(requirement, candidate)
            if identifier in dependencies:
                found = dependencies[identifier] & found
            dependencies[identifier] = found
        self._dependencies[package, version] = dependencies
        return dependencies

    def prioritize(
        self,
        package: str,
        version_range: Range[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> Any:
        preference = self._provider.get_preference(
            identifier=package,
            resolutions={},
            candidates={},
            information={package: self.information.get(package, [])},
            backtrack_causes=[],
        )
        # Whether the target Python meets Requires-Python is known up front.
        return (package != REQUIRES_PYTHON_IDENTIFIER, preference)

    def widen_decision(self, package: str, version: Version) -> None:
        return None


def _derivation_packages(incompatibility: Any) -> set[Any]:
    """Every package a failure's derivation names."""
    packages: set[Any] = set()
    stack = [incompatibility]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        packages.update(term.package for term in current.terms)
        stack.extend((current.cause_left, current.cause_right))
    return packages


class NabResolver:
    """resolvelib's ``Resolver``, solving with nab."""

    def __init__(self, provider: Any, reporter: BaseReporter[Any, Any, Any]) -> None:
        self.provider = provider
        self.reporter = reporter

    def resolve(self, requirements: Iterable[Any], max_rounds: int) -> Result:
        adapter = _NabProvider(self.provider, self.reporter)
        roots = []
        for requirement in requirements:
            identifier, found = adapter.require(requirement, None)
            roots.append(RootRequirement(identifier, found, requirement))
        self.reporter.starting()
        solver = NabSolver(
            adapter, max_iterations=max_rounds, root_version=Version("0")
        )
        try:
            solution = solver.solve(roots)
        except NabResolutionError as error:
            if error.incompatibility is None:
                raise ResolutionTooDeep(max_rounds) from error
            logger.debug("nab's derivation of the failure:\n%s", error)
            raise ResolutionImpossible(self._causes(adapter, error)) from error

        mapping = {
            identifier: adapter.candidate(identifier, version)
            for identifier, version in solution.pins.items()
        }
        graph: DirectedGraph[Any] = DirectedGraph()
        graph.add(None)
        for identifier in mapping:
            graph.add(identifier)
        for identifier in solution.roots:
            graph.connect(None, identifier)
        for parent, child in solution.edges:
            if parent in mapping and child in mapping:
                graph.connect(parent, child)
        self.reporter.ending(solution)
        return Result(mapping=mapping, graph=graph, criteria={})

    @staticmethod
    def _causes(
        adapter: _NabProvider, error: NabResolutionError
    ) -> list[RequirementInformation[Any, Any]]:
        """The requirements a failure involves, as pip reports them: those on
        the packages whose requirements no one version meets -- pip's failing
        criterion -- or, where none is, on every package the derivation names;
        each from the user or from a candidate of a package it names."""
        packages = _derivation_packages(error.incompatibility)
        relevant: dict[Any, list[RequirementInformation[Any, Any]]] = {}
        for package in packages:
            relevant[package] = [
                information
                for information in adapter.information.get(package, ())
                if information.parent is None or information.parent.name in packages
            ]
        unmet = {
            package
            for package, informations in relevant.items()
            if informations
            and functools.reduce(
                operator.and_,
                (adapter.range_of(i.requirement) for i in informations),
            ).is_empty
        }
        causes = []
        seen = set()
        for package in unmet or packages:
            for information in relevant[package]:
                parent = information.parent
                key = (
                    information.requirement.format_for_error(),
                    None if parent is None else (parent.name, parent.version),
                )
                if key not in seen:
                    seen.add(key)
                    causes.append(information)
        return causes or [
            information
            for informations in adapter.information.values()
            for information in informations
            if information.parent is None
        ]
