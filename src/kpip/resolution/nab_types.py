"""Standalone types and pure helpers shared by the NAB provider adapter.

These carry no ``NabProvider`` instance state, so they live apart from the
adapter's stateful decision/candidate-selection logic in nab_provider.py.
"""

from __future__ import annotations

lazy from urllib.parse import urlsplit

lazy from kpip._vendor.nab_resolver.ranges import Range
lazy from kpip.core.errors import KpipError
lazy from kpip.core.metadata import InstalledDistribution
lazy from kpip.core.packaging import Requirement, SpecifierSet, canonicalize_name
lazy from kpip.core.versions import Version


class InstalledCandidate:
    """NAB candidate backed by an already-installed distribution."""

    source_kind = "installed"
    source_url = None
    source_vcs = None
    source_hashes = None
    path = ""
    from_cache = False
    yanked_reason = None
    requires_python = None
    provided_extras = frozenset()

    def __init__(
        self, distribution: InstalledDistribution, extras: frozenset[str]
    ) -> None:
        self.distribution = distribution
        self.name = distribution.name
        version = distribution.version
        if version is None:
            raise ValueError(f"installed {distribution.name} has no PEP 440 version")
        self.version = version
        self.dependencies = tuple(distribution.dependencies(extras))
        self.path = distribution.location

    @property
    def canonical_name(self) -> str:
        return self.distribution.canonical_name


_MIN_PINS_TO_DISAGREE = 2


def _dependencies_or_none(candidate: object) -> tuple[Requirement, ...] | None:
    """A candidate's dependencies, or None when its metadata will not load.

    Reading ``dependencies`` is what pulls metadata, which for a source
    artifact means a build and for a remote one a request.  The forward check
    asks this of releases the resolver may never select, so a release with
    broken or unreachable metadata must read as undecidable rather than take
    the whole resolution down with it -- a different release may well resolve.
    """
    try:
        return tuple(getattr(candidate, "dependencies", ()))
    except (KpipError, OSError, ValueError):
        return None


def _implied_range(specifier: SpecifierSet) -> Range[Version]:
    """Widen a specifier to the interval that contains everything it admits.

    ``bounds`` drops ``!=``, ``===`` and ``==X.*`` and reads ``~=`` as its
    half-open interval, and it is blind to pre-release rules.  Every one of
    those is a widening, which is the direction a rejection needs: an empty
    intersection of intervals that each contain *more* than their specifier is
    empty for the specifiers too.

    Working in intervals rather than over the catalog also keeps the answer
    independent of which releases the active policy happens to admit -- a
    yanked-only release cannot turn a possible fan-out into a rejected one.
    """
    lower, upper = specifier.bounds
    if lower is None:
        if upper is None:
            return Range.full()
        version, inclusive = upper
        return Range.at_most(version) if inclusive else Range.less_than(version)

    lower_version, lower_inclusive = lower
    if upper is None:
        return (
            Range.at_least(lower_version)
            if lower_inclusive
            else Range.greater_than(lower_version)
        )

    upper_version, upper_inclusive = upper
    return Range.between(
        lower_version,
        upper_version,
        lower_inclusive=lower_inclusive,
        upper_inclusive=upper_inclusive,
    )


def _key(requirement: Requirement) -> str:
    name = requirement.name
    if name.startswith(("file://", "http://", "https://")):
        name = urlsplit(name).path.rstrip("/").rsplit("/", 1)[-1] or name
    return canonicalize_name(name)


class _RecordingRequirements(dict):
    """The package -> requirement map, recording every package it replaces.

    ``prioritize`` answers from ``len(_versions(package))``, which follows
    the package's requirement, and the resolver caches sort keys between
    decision scans. Recording in ``__setitem__`` rather than at the handful
    of assignment sites is what keeps that record complete as merging grows
    new ones.
    """

    __slots__ = ("touched",)

    def __init__(self) -> None:
        super().__init__()
        self.touched: set[str] = set()

    def __setitem__(self, key: str, value: Requirement) -> None:
        self.touched.add(key)
        super().__setitem__(key, value)
