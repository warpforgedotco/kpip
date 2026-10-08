"""pip's resolver, solved by nab-resolver.

pip's resolvelib layer -- its factory, candidates, requirements, provider and
error reporting -- is kept as pip has it; this replaces only resolvelib's
``Resolver``, with nab's PubGrub solver (``kpip._vendor.nab_resolver``).

``NabResolver`` asks pip's ``PipProvider`` what resolvelib would ask it and
hands the problem to nab. Each of pip's identifiers is a nab package. A nab
version is a candidate's key: its version, and for a candidate a requirement
names directly -- a URL, a path, an editable -- its link too, so two
different links of one version are two choices, as they are to resolvelib.
Each of pip's requirements is the range of the keys ``find_matches`` gives
for it, asked as resolvelib would ask it: beside the user's own requirements
on that identifier, which are always in play. It answers as resolvelib does
-- a ``Result`` of the chosen candidates and their graph, or
``ResolutionImpossible`` naming the requirements in conflict -- so pip turns
either into what it reports.
"""

from __future__ import annotations

import functools
import logging
import operator
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, NamedTuple

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
from kpip._internal.models.link import _clean_link
from kpip._internal.resolution.resolvelib.candidates import (
    REQUIRES_PYTHON_IDENTIFIER,
)
from kpip._internal.resolution.resolvelib.found_candidates import FoundCandidates

logger = logging.getLogger(__name__)

# How many times a resolution starts again, having learnt something no range
# it had computed knew: a direct candidate a dependency named, or that the
# installed versions it kept were not enough.
_MAX_ATTEMPTS = 20


class _Key(NamedTuple):
    """A nab version: a candidate's version, and the link of a candidate a
    requirement names directly ("" for the index's and the installed)."""

    version: Version
    link: str

    def __str__(self) -> str:
        return str(self.version) if not self.link else f"{self.version} ({self.link})"


_ROOT_KEY = _Key(Version("0"), "")


class _Restart(Exception):
    """A resolution must start again, knowing what it now knows."""


class _IteratorMapping(Mapping[str, Iterator[Any]]):
    """Requirements by identifier, as resolvelib hands them to
    ``find_matches``: a fresh iterator each time one is asked for, since
    pip reads them more than once."""

    def __init__(self, values: Mapping[str, list[Any]]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> Iterator[Any]:
        return iter(self._values[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _Criterion(NamedTuple):
    """What pip's reporter reads of resolvelib's criterion."""

    information: list[RequirementInformation[Any, Any]]


def _base_identifier(identifier: str) -> str:
    return identifier.partition("[")[0]


def _is_explicit(requirement: Any) -> bool:
    return requirement.get_candidate_lookup()[0] is not None


_INSTALLED = "<installed>"


def _link_identity(link: Any) -> str:
    """A link as pip tells links apart (``links_equivalent``)."""
    clean = _clean_link(link)
    return "|".join(
        (
            clean.parsed.geturl(),
            repr(sorted(clean.query.items())),
            clean.subdirectory,
            repr(sorted(clean.hashes.items())),
        )
    )


def _candidate_key(candidate: Any) -> _Key:
    """A built candidate's key: the installed distribution's, or its file's.
    A candidate reached through an explicit requirement -- a URL, or the base
    an extras candidate depends on exactly -- has the key the index gave the
    same file."""
    if candidate.is_installed:
        return _Key(candidate.version, _INSTALLED)
    link = candidate.source_link
    return _Key(candidate.version, _link_identity(link) if link else repr(candidate))


def _index_key(version: Version, build: Callable[[], Any]) -> _Key:
    """An index candidate's key, from the link pip will build it from,
    without building it."""
    link = getattr(build, "keywords", {}).get("link")
    return _Key(version, _link_identity(link) if link is not None else "")


class _NabProvider(BaseProvider[str, _Key]):
    """pip's provider, as nab asks it."""

    def __init__(
        self,
        provider: Any,
        reporter: BaseReporter[Any, Any, Any],
        seeds: dict[str, list[Any]],
        installed_first: bool,
    ) -> None:
        self._provider = provider
        self._reporter = reporter
        # Explicit requirements earlier attempts met, by identifier: each is
        # asked about beside every requirement on its identifier.
        self._seeds = seeds
        # Whether a requirement pip would meet with the installed version
        # sees only that version, so the index is not read for it. Set
        # again for the next attempt when that was not enough.
        self._installed_first = installed_first
        self.kept_installed = False
        # Every requirement seen on each identifier, with what required it.
        self.information: dict[str, list[RequirementInformation[Any, Any]]] = {}
        # The user's own requirements, by identifier: always in play.
        self._roots: dict[str, list[Any]] = {}
        # Identifiers with a range computed without an explicit candidate.
        self._index_ranged: set[str] = set()
        # Per identifier: each key's candidate, built or how to build it.
        self._pool: dict[str, dict[_Key, Callable[[], Any]]] = {}
        # Per identifier: its keys in pip's order, best first. Every list
        # ``find_matches`` gives is a slice of one order, so they merge.
        self._order: dict[str, list[_Key]] = {}
        self._built: dict[tuple[str, _Key], Any] = {}
        self._decided: dict[str, _Key] = {}
        # Keyed by id(): requirements are kept alive in _requirements.
        self._ranges: dict[int, Range[_Key]] = {}
        self._requirements: list[Any] = []
        # Requirements compare by what they say: equal ones on one identifier
        # have the same range.
        self._ranges_by_content: dict[tuple[str, Any], Range[_Key]] = {}
        self._dependencies: dict[tuple[str, _Key], dict[str, Range[_Key]]] = {}

    def add_roots(
        self, requirements: Iterable[Any]
    ) -> list[RootRequirement[str, _Key]]:
        """The user's requirements, each recorded before any range is asked
        for, so every range is asked for beside all of them."""
        requirements = list(requirements)
        identified = []
        for requirement in requirements:
            identifier = self._provider.identify(requirement)
            self._roots.setdefault(identifier, []).append(requirement)
            identified.append((identifier, requirement))
        return [
            RootRequirement(identifier, self.require(requirement, None)[1], requirement)
            for identifier, requirement in identified
        ]

    def require(self, requirement: Any, parent: Any) -> tuple[str, Range[_Key]]:
        """Record ``requirement`` and return its identifier and range."""
        identifier = self._provider.identify(requirement)
        if parent is not None and _is_explicit(requirement):
            self._learn_explicit(identifier, requirement)
        self.information.setdefault(identifier, []).append(
            RequirementInformation(requirement, parent)
        )
        self._reporter.adding_requirement(requirement, parent)
        return identifier, self._range(identifier, requirement)

    def _learn_explicit(self, identifier: str, requirement: Any) -> None:
        """A dependency names a candidate directly. pip then considers only
        such candidates for its identifier (and, for one with extras, its
        base): a range computed from the index for either is wrong, and the
        resolution starts again knowing of it."""
        candidate = requirement.get_candidate_lookup()[0]
        if _candidate_key(candidate) in self._pool.get(identifier, {}):
            # A candidate already in play -- the base an extras candidate
            # depends on, say: nothing new.
            return
        known = self._seeds.setdefault(identifier, [])
        if requirement in known:
            return
        known.append(requirement)
        if identifier in self._index_ranged or (
            _base_identifier(identifier) in self._index_ranged
        ):
            raise _Restart

    def _context(self, identifier: str, requirement: Any) -> dict[str, list[Any]]:
        """What ``find_matches`` is asked with for ``requirement``: it, the
        user's requirements on its identifier and, for one with extras, on
        its base, and every explicit requirement met on them."""
        base = _base_identifier(identifier)
        context = {identifier: [requirement, *self._roots.get(identifier, ())]}
        context[identifier] += [
            seed for seed in self._seeds.get(identifier, ()) if seed is not requirement
        ]
        if base != identifier:
            context[base] = [
                *self._roots.get(base, ()),
                *self._seeds.get(base, ()),
            ]
        return context

    def _range(self, identifier: str, requirement: Any) -> Range[_Key]:
        cached = self._ranges.get(id(requirement))
        if cached is not None:
            return cached
        content_key = (identifier, requirement)
        found = self._ranges_by_content.get(content_key)
        if found is None:
            context = self._context(identifier, requirement)
            matches = self._provider.find_matches(
                identifier,
                _IteratorMapping(context),
                _IteratorMapping({identifier: []}),
            )
            keys = self._pool_matches(identifier, matches)
            found = Range.from_versions(keys)
            self._ranges_by_content[content_key] = found
        self._ranges[id(requirement)] = found
        self._requirements.append(requirement)
        return found

    def _pool_matches(self, identifier: str, matches: Iterable[Any]) -> list[_Key]:
        """The keys of ``matches``, in pip's order, recorded with how to
        build each; none is built."""
        entries: list[tuple[_Key, Callable[[], Any]]]
        if isinstance(matches, FoundCandidates):
            self._index_ranged.add(identifier)
            installed = matches._installed
            entries = []
            if installed is not None:
                entries.append((_Key(installed.version, _INSTALLED), lambda: installed))
            if (
                installed is None
                or not matches._prefers_installed
                or not self._installed_first
            ):
                # One candidate per version: the installed one, if any.
                versions = {installed.version} if installed is not None else set()
                index = []
                for version, build in matches._get_infos():
                    if version not in versions:
                        versions.add(version)
                        index.append((_index_key(version, build), build))
                if installed is not None and not matches._prefers_installed:
                    # pip places the installed version among the index's.
                    position = next(
                        (
                            i
                            for i, (key, _) in enumerate(index)
                            if installed.version >= key.version
                        ),
                        len(index),
                    )
                    index.insert(position, entries.pop())
                entries += index
            else:
                self.kept_installed = True
        else:
            entries = [
                (_candidate_key(candidate), lambda c=candidate: c)
                for candidate in matches
            ]
        self._merge(identifier, entries)
        return [key for key, _ in entries]

    def _merge(
        self, identifier: str, entries: list[tuple[_Key, Callable[[], Any]]]
    ) -> None:
        """Merge a slice of pip's order into the identifier's order. A key the
        slice places after a known one goes after it, and after any keys the
        slice does not hold that are newer: where pip's order is by version,
        that is where it belongs."""
        pool = self._pool.setdefault(identifier, {})
        order = self._order.setdefault(identifier, [])
        in_slice = {key for key, _ in entries}
        position = -1
        for key, build in entries:
            if key in pool:
                position = order.index(key)
                continue
            while (
                position + 1 < len(order)
                and order[position + 1] not in in_slice
                and order[position + 1].version > key.version
            ):
                position += 1
            pool[key] = build
            position += 1
            order.insert(position, key)

    def _build(self, identifier: str, key: _Key) -> Any:
        """The candidate for ``key``, built once; None, and the key dropped,
        when its metadata is invalid, as ``FoundCandidates`` skips it."""
        if (identifier, key) in self._built:
            return self._built[identifier, key]
        try:
            candidate = self._pool[identifier][key]()
        except MetadataInvalid as e:
            logger.warning(
                "Ignoring version %s of %s since it has invalid metadata:\n"
                "%s\n"
                "Please use pip<24.1 if you need to use this version.",
                key.version,
                e.ireq.name,
                e,
            )
            candidate = None
        self._built[identifier, key] = candidate
        if candidate is None:
            del self._pool[identifier][key]
            self._order[identifier].remove(key)
        return candidate

    def meets_none_alone(self, requirement: Any) -> bool:
        """Whether no candidate meets ``requirement`` even asked about on its
        own, without the user's other requirements on its identifier."""
        identifier = self._provider.identify(requirement)
        matches = self._provider.find_matches(
            identifier,
            _IteratorMapping({identifier: [requirement]}),
            _IteratorMapping({identifier: []}),
        )
        if isinstance(matches, FoundCandidates):
            # Without building one: iterating would prepare the first.
            return matches._installed is None and not any(
                True for _ in matches._get_infos()
            )
        return not any(True for _ in matches)

    def range_of(self, requirement: Any) -> Range[_Key]:
        return self._ranges[id(requirement)]

    def candidate(self, identifier: str, key: _Key) -> Any:
        return self._built[identifier, key]

    def choose_version(self, package: str, version_range: Range[_Key]) -> _Key | None:
        # pip's best key in the range that builds. Building can drop a key
        # from the order: walk a copy.
        for key in list(self._order.get(package, ())):
            if key not in version_range:
                continue
            candidate = self._build(package, key)
            if candidate is None:
                continue
            previous = self._decided.get(package)
            if previous is not None and previous != key:
                # resolvelib's word for nab undoing a decision.
                self._reporter.rejecting_candidate(
                    _Criterion(self.information.get(package, [])),
                    self.candidate(package, previous),
                )
            self._decided[package] = key
            self._reporter.pinning(candidate)
            return key
        return None

    def has_satisfying_version(self, package: str, version_range: Range[_Key]) -> bool:
        return any(key in version_range for key in self._pool.get(package, {}))

    def get_dependencies(
        self, package: str, version: _Key
    ) -> Mapping[str, Range[_Key]]:
        known = self._dependencies.get((package, version))
        if known is not None:
            return known
        candidate = self.candidate(package, version)
        dependencies: dict[str, Range[_Key]] = {}
        for requirement in self._provider.get_dependencies(candidate):
            identifier, found = self.require(requirement, candidate)
            if identifier in dependencies:
                found = dependencies[identifier] & found
            dependencies[identifier] = found
            if found.is_empty:
                # This version cannot be chosen, whatever else it needs. The
                # rest are not read, as resolvelib does not: reading one can
                # build it (a URL) -- and Requires-Python comes first.
                break
        self._dependencies[package, version] = dependencies
        return dependencies

    def prioritize(
        self,
        package: str,
        version_range: Range[_Key],
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

    def widen_decision(self, package: str, version: _Key) -> None:
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
        requirements = list(requirements)
        self.reporter.starting()
        seeds: dict[str, list[Any]] = {}
        installed_first = True
        for _ in range(_MAX_ATTEMPTS):
            adapter = _NabProvider(self.provider, self.reporter, seeds, installed_first)
            try:
                roots = adapter.add_roots(requirements)
                solution = NabSolver(
                    adapter, max_iterations=max_rounds, root_version=_ROOT_KEY
                ).solve(roots)
            except _Restart:
                continue
            except NabResolutionError as error:
                if error.incompatibility is not None and adapter.kept_installed:
                    # The installed versions kept were not enough: look at
                    # the index too, as resolvelib would on backtracking.
                    installed_first = False
                    continue
                if error.incompatibility is None:
                    raise ResolutionTooDeep(max_rounds) from error
                logger.debug("nab's derivation of the failure:\n%s", error)
                raise ResolutionImpossible(self._causes(adapter, error)) from error
            break
        else:
            raise ResolutionTooDeep(max_rounds)

        mapping = {
            identifier: adapter.candidate(identifier, key)
            for identifier, key in solution.pins.items()
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
            informations = relevant[package]
            # A requirement no candidate meets even on its own is the whole
            # story for its package, as resolvelib tells it.
            alone = [
                information
                for information in informations
                if adapter.range_of(information.requirement).is_empty
                and adapter.meets_none_alone(information.requirement)
            ]
            for information in alone or informations:
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
