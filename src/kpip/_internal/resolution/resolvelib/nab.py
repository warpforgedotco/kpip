"""pip's resolver, solved by nab-resolver.

pip's resolvelib layer -- its factory, candidates, requirements, provider and
error reporting -- is kept as pip has it; this replaces only resolvelib's
``Resolver``, with nab's PubGrub solver (``kpip._vendor.nab_resolver``).

``NabResolver`` asks pip's ``PipProvider`` what resolvelib would ask it and
hands the problem to nab. Each of pip's identifiers is a nab package. A nab
version is a candidate's key: its version, and for a candidate a requirement
names directly -- a URL, a path, an editable -- its link too, so two
different links of one version are two choices, as they are to resolvelib.
Each of pip's requirements is the range of the keys that could ever be
chosen while it is in play: what ``find_matches`` gives for it beside the
user's own requirements, with pre-releases admitted, and beside each
requirement seen that admits more (a direct URL, a pinned yanked release).
Which of them is eligible is pip's to say, as it depends on everything in
play: nab chooses a version by asking ``find_matches`` about the requirements
of the user and of the candidates it has decided on, as resolvelib asks it,
and taking pip's first candidate in nab's range. It answers as resolvelib does
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

from kpip._internal.exceptions import MetadataInvalid, UnsupportedWheel
from kpip._internal.models.link import _clean_link
from kpip._internal.resolution.resolvelib.candidates import (
    REQUIRES_PYTHON_IDENTIFIER,
)
from kpip._internal.resolution.resolvelib.found_candidates import FoundCandidates
from kpip._internal.resolution.resolvelib.requirements import UnsatisfiableRequirement

logger = logging.getLogger(__name__)

# pip's budget counts resolvelib's rounds. nab spends its iterations far
# faster -- each decision a backjump undoes is made again; about 70 to a round
# on airflow -- so it is given this many for each.
_ITERATIONS_PER_ROUND = 100


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


def _is_direct(candidate: Any) -> bool:
    """Whether a requirement names the candidate's file -- a URL, a path, an
    editable -- rather than pip finding it: pip makes an index candidate from
    a requirement with no link of its own."""
    candidate = getattr(candidate, "base", candidate)  # an extras candidate's
    ireq = candidate.get_install_requirement()
    return ireq is not None and ireq.original_link is not None


def _candidate_key(candidate: Any) -> _Key:
    """A built candidate's key: the installed distribution's, a named file's,
    or, for one pip found, its version's -- whichever of its files built."""
    if candidate.is_installed:
        return _Key(candidate.version, _INSTALLED)
    if _is_direct(candidate):
        link = candidate.source_link
        return _Key(
            candidate.version, _link_identity(link) if link else repr(candidate)
        )
    return _Key(candidate.version, "")


def _file(build: Callable[[], Any]) -> Any:
    """The link of the file an index candidate is built from."""
    return getattr(build, "keywords", {}).get("link")


class _NabProvider(BaseProvider[str, _Key]):
    """pip's provider, as nab asks it."""

    def __init__(
        self,
        provider: Any,
        reporter: BaseReporter[Any, Any, Any],
        admitting: dict[str, list[Any]],
        full: set[str],
        strict: set[str],
    ) -> None:
        self._provider = provider
        self._reporter = reporter
        # Requirements from dependencies that make eligible a candidate the
        # other requirements on their identifier would not offer: a direct
        # URL, or a pinned yanked release. Learnt across attempts. Each widens
        # every range on its identifier -- the ranges are what could ever be
        # chosen -- and never narrows one: which candidates are eligible is
        # pip's to say, when one is chosen.
        self._admitting = admitting
        # Identifiers whose index must be read although an installed version
        # pip prefers is there: one of their requirements read it, or nab
        # ruled the installed version out. Learnt across attempts. Until
        # then, as pip, their index is not read, and their ranges hold the
        # installed version only.
        self._full = full
        # Identifiers a version chosen beside a requirement not in play left
        # one pip would not offer in the end: none is chosen so again.
        self._strict = strict
        self.fallbacks: list[tuple[str, _Key]] = []
        self.installed_only: set[str] = set()
        # Every requirement seen on each identifier, with what required it,
        # and that parent's identifier and key (None for the user's).
        self.information: dict[str, list[RequirementInformation[Any, Any]]] = {}
        self._sources: dict[str, list[tuple[Any, str | None, _Key | None]]] = {}
        # The user's own requirements, by identifier: always in play.
        self._roots: dict[str, list[Any]] = {}
        # Per identifier: how to build each key's candidate. pip offers a
        # version it finds once, built from the first of its files, in pip's
        # order, that builds: an index key holds each file's.
        self._pool: dict[str, dict[_Key, dict[str, Callable[[], Any]]]] = {}
        self._built: dict[tuple[str, _Key], Any] = {}
        self._invalid: set[tuple[str, _Key]] = set()
        self._yanked: set[tuple[str, _Key]] = set()
        # Keyed by id(): built candidates are kept alive in _built.
        self._keys: dict[int, _Key] = {}
        # nab's decisions as of the last choice, what is in play, and the
        # ranges it has derived, what still may be.
        self._decisions: Mapping[str, _Key] = {}
        self._positive_ranges: Mapping[str, Range[_Key]] = {}
        # The decision made since nab's last snapshot.
        self._latest: tuple[str, _Key] | None = None
        self._decided_before: dict[str, _Key] = {}
        # Keyed by id(): requirements are kept alive in _requirements.
        self._ranges: dict[int, Range[_Key]] = {}
        self._requirements: list[Any] = []
        # Requirements compare by what they say: equal ones on one identifier
        # have the same range.
        self._ranges_by_content: dict[tuple[str, Any], Range[_Key]] = {}
        # Per identifier: the requirements a range was computed for.
        self._ranged: dict[str, list[Any]] = {}
        self._dependencies: dict[tuple[str, _Key], dict[str, Range[_Key]]] = {}
        self._opt_ins: dict[str, Any] = {}
        # The first error reading a candidate's dependencies raised, for when
        # nothing else resolves.
        self.unreadable: Exception | None = None
        # What pip offers for each set of requirements in play: nab asks for
        # a package's version under the same set again and again.
        self._offers: dict[tuple[Any, ...], list[Any]] = {}

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

    def require(
        self, requirement: Any, parent: Any, source: tuple[str, _Key] | None = None
    ) -> tuple[str, Range[_Key]]:
        """Record ``requirement`` of ``parent``, whose identifier and key
        ``source`` gives, and return its identifier and range."""
        identifier = self._provider.identify(requirement)
        self.information.setdefault(identifier, []).append(
            RequirementInformation(requirement, parent)
        )
        name, key = source or (None, None)
        self._sources.setdefault(identifier, []).append((requirement, name, key))
        self._reporter.adding_requirement(requirement, parent)
        found = self._range(identifier, requirement)
        if parent is not None and self._admits(identifier, requirement, found):
            self._learn_admitting(identifier, requirement)
        return identifier, found

    def _admits(self, identifier: str, requirement: Any, found: Range[_Key]) -> bool:
        """Whether ``requirement`` makes eligible what other requirements on
        its identifier would not offer: a candidate it names, or a yanked
        release it pins."""
        if identifier == REQUIRES_PYTHON_IDENTIFIER:
            return False
        return _is_explicit(requirement) or any(
            (identifier, key) in self._yanked and key in found
            for key in self._pool.get(identifier, ())
        )

    def _learn_admitting(self, identifier: str, requirement: Any) -> None:
        """Widen every range on ``identifier`` -- and, for a base, on its
        extras -- by what ``requirement`` admits. A range already given to nab
        cannot widen: when one would, the resolution starts again."""
        known = self._admitting.setdefault(identifier, [])
        if requirement in known:
            return
        known.append(requirement)
        for ranged, requirements in self._ranged.items():
            if ranged != identifier and _base_identifier(ranged) != identifier:
                continue
            for other in requirements:
                if other == requirement:
                    continue
                if ranged == identifier:
                    matches = self._find(ranged, [other, requirement])
                else:
                    matches = self._find(ranged, [other], [requirement])
                found = self._ranges[id(other)]
                if any(key not in found for key in self._pool_matches(ranged, matches)):
                    raise _Restart

    def _opt_in(self, name: str) -> Any:
        """A requirement on ``name`` that every version meets and that admits
        pre-releases, as pip admits them once any requirement names one."""
        opt_in = self._opt_ins.get(name)
        if opt_in is None:
            opt_in = next(
                iter(
                    self._provider._factory.make_requirements_from_spec(
                        f"{name}>=0.dev0", None
                    )
                )
            )
            self._opt_ins[name] = opt_in
        return opt_in

    def _find(
        self,
        identifier: str,
        requirements: list[Any],
        base_requirements: Iterable[Any] = (),
    ) -> Iterable[Any]:
        """``find_matches`` for ``requirements`` beside the user's, with
        pre-releases admitted: what pip could offer for them, whatever else
        is in play."""
        base = _base_identifier(identifier)
        context = {identifier: [*requirements, *self._roots.get(identifier, ())]}
        if base != identifier:
            context[base] = [*self._roots.get(base, ()), *base_requirements]
        if base != REQUIRES_PYTHON_IDENTIFIER:
            context[base].append(self._opt_in(base))
        return self._provider.find_matches(
            identifier, _IteratorMapping(context), _IteratorMapping({identifier: []})
        )

    def _range(self, identifier: str, requirement: Any) -> Range[_Key]:
        cached = self._ranges.get(id(requirement))
        if cached is not None:
            return cached
        content_key = (identifier, requirement)
        found = self._ranges_by_content.get(content_key)
        if found is None:
            found = Range.from_versions(self._permitted(identifier, requirement))
            self._ranges_by_content[content_key] = found
        self._ranges[id(requirement)] = found
        self._requirements.append(requirement)
        self._ranged.setdefault(identifier, []).append(requirement)
        return found

    def _permitted(self, identifier: str, requirement: Any) -> list[_Key]:
        """The keys that could ever be chosen while ``requirement`` is in
        play: what pip offers for it beside the user's requirements, and
        beside each known requirement that admits more."""
        if isinstance(requirement, UnsatisfiableRequirement):
            # Nothing meets it; find_matches would not say so beside a
            # requirement that names the project.
            return []
        keys = self._pool_matches(identifier, self._find(identifier, [requirement]))
        base = _base_identifier(identifier)
        for admitting in self._admitting.get(identifier, ()):
            if admitting != requirement:
                matches = self._find(identifier, [requirement, admitting])
                keys += self._pool_matches(identifier, matches)
        if base != identifier:
            for admitting in self._admitting.get(base, ()):
                matches = self._find(identifier, [requirement], [admitting])
                keys += self._pool_matches(identifier, matches)
        return keys

    def _add(
        self, identifier: str, key: _Key, build: Callable[[], Any], file: str = ""
    ) -> None:
        builds = self._pool.setdefault(identifier, {}).setdefault(key, {})
        builds.setdefault(file, build)

    def _add_infos(self, identifier: str, matches: FoundCandidates) -> list[Any]:
        """Record each file ``matches`` offers under its version's key;
        returns the files' versions, in pip's order."""
        versions = []
        for version, build in matches._get_infos():
            key = _Key(version, "")
            link = _file(build)
            self._add(identifier, key, build, "" if link is None else link.url)
            if link is not None and link.is_yanked:
                self._yanked.add((identifier, key))
            versions.append(version)
        return versions

    def _pool_matches(self, identifier: str, matches: Iterable[Any]) -> list[_Key]:
        """The keys of every file ``matches`` holds, recorded with how to
        build each; none is built."""
        keys = []
        if isinstance(matches, FoundCandidates):
            installed = matches._installed
            if installed is not None:
                key = _Key(installed.version, _INSTALLED)
                self._add(identifier, key, lambda: installed)
                keys.append(key)
                if matches._prefers_installed and identifier not in self._full:
                    self.installed_only.add(identifier)
                    return keys
            self._read_index(identifier)
            keys += [
                _Key(version, "") for version in self._add_infos(identifier, matches)
            ]
        else:
            for candidate in matches:
                key = _candidate_key(candidate)
                self._add(identifier, key, lambda c=candidate: c)
                keys.append(key)
        return keys

    def _read_index(self, identifier: str) -> None:
        """Note that ``identifier``'s index is read: a range computed without
        it, from its installed version alone, is too narrow."""
        if identifier in self._full:
            return
        self._full.add(identifier)
        if identifier in self.installed_only:
            raise _Restart

    def _build(self, identifier: str, key: _Key) -> Any:
        """The candidate for ``key``, built once, from the first of its files
        that builds; None, and the key dropped, when none does or one's
        metadata is invalid (pip then tries no other file of the version)."""
        if (identifier, key) in self._built:
            return self._built[identifier, key]
        candidate = None
        for build in list(self._pool[identifier][key].values()):
            try:
                candidate = build()
            except MetadataInvalid as e:
                logger.warning(
                    "Ignoring version %s of %s since it has invalid metadata:\n"
                    "%s\n"
                    "Please use pip<24.1 if you need to use this version.",
                    key.version,
                    e.ireq.name,
                    e,
                )
                self._invalid.add((identifier, key))
                candidate = None
                break
            if candidate is not None:
                break
        self._built[identifier, key] = candidate
        if candidate is None:
            del self._pool[identifier][key]
        else:
            self._keys[id(candidate)] = key
        return candidate

    def meets_none_alone(self, requirement: Any) -> bool:
        """Whether no candidate meets ``requirement`` even asked about on its
        own, without the user's other requirements on its identifier."""
        if isinstance(requirement, UnsatisfiableRequirement):
            return True
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

    def key_of(self, candidate: Any) -> _Key | None:
        return self._keys.get(id(candidate))

    def candidate(self, identifier: str, key: _Key) -> Any:
        return self._built[identifier, key]

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, Range[_Key]],
        decisions: Mapping[str, _Key],
    ) -> None:
        self._positive_ranges = positive_ranges
        self._decisions = decisions
        self._latest = None

    def _decided(self, name: str) -> _Key | None:
        if self._latest is not None and self._latest[0] == name:
            return self._latest[1]
        return self._decisions.get(name)

    def _is_active(self, name: str | None, key: _Key | None) -> bool:
        """Whether a requirement of the parent ``name`` at ``key`` is in
        play: the user's (no parent), or one of a version nab decided on."""
        return name is None or self._decided(name) == key

    def _may_be_active(self, name: str | None, key: _Key | None) -> bool:
        """Whether nab has not ruled out the parent ``name`` at ``key``:
        decided another version, or derived a range without it."""
        if name is None:
            return True
        decided = self._decided(name)
        if decided is not None:
            return decided == key
        allowed = self._positive_ranges.get(name)
        return allowed is None or key in allowed

    def _active(self, identifier: str) -> list[Any]:
        """The requirements in play on ``identifier``."""
        return [
            requirement
            for requirement, name, key in self._sources.get(identifier, ())
            if self._is_active(name, key)
        ]

    def choose_version(self, package: str, version_range: Range[_Key]) -> _Key | None:
        key = self._choose(package, version_range)
        if key is None:
            return None
        previous = self._decided_before.get(package)
        if previous is not None and previous != key:
            # resolvelib's word for nab undoing a decision.
            self._reporter.rejecting_candidate(
                _Criterion(self.information.get(package, [])),
                self.candidate(package, previous),
            )
        self._decided_before[package] = key
        self._latest = (package, key)
        self._reporter.pinning(self.candidate(package, key))
        return key

    def _choose(self, package: str, version_range: Range[_Key]) -> _Key | None:
        """pip's first candidate in ``version_range`` for the requirements in
        play -- what resolvelib would pin. nab can know a range before what
        admits its versions is in play: it derives that a dependency must hold
        before deciding the package that has it."""
        base = _base_identifier(package)
        context = {package: self._active(package)}
        if base != package:
            context[base] = self._active(base)
        key = next(self._admitted(package, context, version_range), None)
        if key is not None:
            return key
        # Then a candidate pip offers beside a requirement seen on the
        # package from a version of its parent nab has not ruled out -- what
        # it derived the range from -- and never one no such requirement
        # admits.
        if package in self._strict:
            return None
        for requirement, name, key in self._sources.get(package, ()):
            if self._is_active(name, key) or not self._may_be_active(name, key):
                continue  # in play already, or ruled out
            widened = {**context, package: [requirement, *context[package]]}
            key = next(self._admitted(package, widened, version_range), None)
            if key is not None:
                self.fallbacks.append((package, key))
                return key
        return None

    def offered_in(self, pins: Mapping[str, _Key], package: str, key: _Key) -> bool:
        """Whether pip offers ``key`` for ``package`` beside the requirements
        the user and the ``pins`` have on it."""

        def in_play(identifier: str) -> list[Any]:
            return [
                requirement
                for requirement, name, parent in self._sources.get(identifier, ())
                if name is None or pins.get(name) == parent
            ]

        base = _base_identifier(package)
        context = {package: in_play(package)}
        if base != package:
            context[base] = in_play(base)
        return (
            next(self._admitted(package, context, Range.singleton(key)), None)
            is not None
        )

    def _offered(self, package: str, context: dict[str, list[Any]]) -> list[Any]:
        """What pip offers for the requirements ``context`` holds:
        ``find_matches``'s answer, and the versions it finds in pip's order
        (None until read) -- or, when a requirement names one, the candidates
        it names."""
        base = _base_identifier(package)
        signature = (
            package,
            frozenset(context[package]),
            frozenset(context.get(base, ())),
        )
        offer = self._offers.get(signature)
        if offer is None:
            matches = self._provider.find_matches(
                package, _IteratorMapping(context), _IteratorMapping({package: []})
            )
            # The index's versions are read when first needed: not while an
            # installed version pip prefers will do.
            found = isinstance(matches, FoundCandidates)
            offer = [matches, None if found else list(matches)]
            self._offers[signature] = offer
        return offer

    def _admitted(
        self, package: str, context: dict[str, list[Any]], version_range: Range[_Key]
    ) -> Iterator[_Key]:
        """The keys in ``version_range`` of the candidates pip offers for
        ``context``, in pip's order, each built only when reached:
        ``FoundCandidates``'s iteration, keyed."""
        offer = self._offered(package, context)
        matches = offer[0]
        if not isinstance(matches, FoundCandidates):
            for candidate in offer[1]:
                key = _candidate_key(candidate)
                self._add(package, key, lambda c=candidate: c)
                if key in version_range and self._build(package, key) is not None:
                    yield key
            return

        def index(found: set[Any]) -> Iterator[tuple[Any, _Key]]:
            # Each version once, in pip's order, unbuilt: built by the caller,
            # as pip builds it -- after placing the installed version.
            if offer[1] is None:
                self._read_index(package)
                offer[1] = self._add_infos(package, matches)
            for version in offer[1]:
                if version not in found:
                    found.add(version)
                    yield version, _Key(version, "")

        def builds(key: _Key) -> bool:
            return key in version_range and self._build(package, key) is not None

        installed = matches._installed
        if installed is None:
            for _, key in index(set()):
                if builds(key):
                    yield key
            return
        installed_key = _Key(installed.version, _INSTALLED)
        self._add(package, installed_key, lambda: installed)
        if matches._prefers_installed:
            if builds(installed_key):
                yield installed_key
            for _, key in index({installed.version}):
                if builds(key):
                    yield key
            return
        # The installed version among the index's, before the first that is
        # not newer.
        inserted = False
        for version, key in index(set()):
            if not inserted and installed.version >= version:
                inserted = True
                if builds(installed_key):
                    yield installed_key
            if builds(key):
                yield key
        if not inserted and builds(installed_key):
            yield installed_key

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
        try:
            for requirement in self._provider.get_dependencies(candidate):
                identifier, found = self.require(
                    requirement, candidate, (package, version)
                )
                if identifier in dependencies:
                    found = dependencies[identifier] & found
                dependencies[identifier] = found
                if found.is_empty:
                    # This version cannot be chosen, whatever else it needs.
                    # The rest are not read, as resolvelib does not: reading
                    # one can build it (a URL) -- and Requires-Python comes
                    # first.
                    break
        except (FileNotFoundError, UnsupportedWheel) as error:
            # pip prepares a URL dependency as it reads it: one that is gone,
            # or a wheel for another platform. resolvelib meets it only on a
            # candidate it pins -- it stops reading at the first dependency
            # that conflicts -- and nab reads further, so this version is left
            # instead, and the error raised if nothing else resolves.
            if self.unreadable is None:
                self.unreadable = error
            dependencies[f"<unreadable dependencies of {package}>"] = Range.empty()
        self._dependencies[package, version] = dependencies
        return dependencies

    def prioritize(
        self,
        package: str,
        version_range: Range[_Key],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> Any:
        # pip's preference reads the requirements resolvelib's criterion
        # would hold: not those of parent versions nab has left, and those of
        # parents it has yet to decide but has not ruled out -- it often knows
        # a package is needed before deciding what needs it.
        preference = self._provider.get_preference(
            identifier=package,
            resolutions={},
            candidates={},
            information={
                package: [
                    information
                    for information, (_, name, key) in zip(
                        self.information.get(package, ()),
                        self._sources.get(package, ()),
                    )
                    if self._may_be_active(name, key)
                ]
            },
            backtrack_causes=[],
        )
        # Whether the target Python meets Requires-Python is known up front.
        return (package != REQUIRES_PYTHON_IDENTIFIER, preference)

    def widen_decision(self, package: str, version: _Key) -> None:
        return None


def _derivation_terms(incompatibility: Any) -> dict[Any, list[Any]]:
    """Every package a failure's derivation names, with the constraints its
    terms put on it."""
    terms: dict[Any, list[Any]] = {}
    stack = [incompatibility]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for term in current.terms:
            terms.setdefault(term.package, []).append(term.constraint)
        stack.extend((current.cause_left, current.cause_right))
    return terms


class NabResolver:
    """resolvelib's ``Resolver``, solving with nab."""

    def __init__(self, provider: Any, reporter: BaseReporter[Any, Any, Any]) -> None:
        self.provider = provider
        self.reporter = reporter

    def resolve(self, requirements: Iterable[Any], max_rounds: int) -> Result:
        requirements = list(requirements)
        self.reporter.starting()
        admitting: dict[str, list[Any]] = {}
        full: set[str] = set()
        strict: set[str] = set()
        # Each restart has learnt a requirement that admits more, an
        # identifier whose index must be read, or one to choose strictly, so
        # this ends.
        while True:
            adapter = _NabProvider(
                self.provider, self.reporter, admitting, full, strict
            )
            try:
                roots = adapter.add_roots(requirements)
                solution = NabSolver(
                    adapter,
                    max_iterations=max_rounds * _ITERATIONS_PER_ROUND,
                    root_version=_ROOT_KEY,
                ).solve(roots)
            except _Restart:
                continue
            except NabResolutionError as error:
                if adapter.installed_only - full:
                    # The installed versions kept were not enough: read their
                    # index too, as resolvelib would on backtracking.
                    full |= adapter.installed_only
                    continue
                if adapter.unreadable is not None:
                    raise adapter.unreadable from error
                if error.incompatibility is None:
                    raise ResolutionTooDeep(max_rounds) from error
                logger.debug("nab's derivation of the failure:\n%s", error)
                raise ResolutionImpossible(self._causes(adapter, error)) from error
            # A version chosen beside a requirement that is not in the answer
            # must be one pip offers beside those that are.
            unoffered = {
                package
                for package, key in adapter.fallbacks
                if solution.pins.get(package) == key
                and not adapter.offered_in(solution.pins, package, key)
            }
            if unoffered:
                strict |= unoffered
                continue
            break

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
        the packages whose requirements no choice of their parents' versions
        meets -- pip's failing criterion -- or, where none is, on every
        package the derivation names; each from the user or from a version
        of a parent the derivation names."""
        terms = _derivation_terms(error.incompatibility)
        # In name order: the report reads the same whatever the hash seed.
        packages = sorted(terms, key=str)

        def named(information: RequirementInformation[Any, Any]) -> bool:
            parent = information.parent
            if parent is None:
                return True
            key = adapter.key_of(parent)
            return key is not None and any(
                key in constraint for constraint in terms.get(parent.name, ())
            )

        relevant = {
            package: [i for i in adapter.information.get(package, ()) if named(i)]
            for package in packages
        }

        def unmet(informations: list[RequirementInformation[Any, Any]]) -> bool:
            # One version of each parent is in play at a time: the user's
            # requirements and, for each parent, any one of its versions'.
            by_version: dict[tuple[Any, Any], Range[_Key]] = {}
            for information in informations:
                parent = information.parent
                version = (
                    None if parent is None else (parent.name, adapter.key_of(parent))
                )
                found = adapter.range_of(information.requirement)
                if version in by_version:
                    found = by_version[version] & found
                by_version[version] = found
            by_parent: dict[Any, Range[_Key]] = {}
            for version, found in by_version.items():
                name = None if version is None else version[0]
                if name in by_parent:
                    found = by_parent[name] | found
                by_parent[name] = found
            return functools.reduce(operator.and_, by_parent.values()).is_empty

        failing = [
            package
            for package in packages
            if relevant[package] and unmet(relevant[package])
        ]
        causes = []
        seen = set()
        for package in failing or packages:
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
