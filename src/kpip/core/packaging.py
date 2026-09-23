from __future__ import annotations

import os
import re
import sys

from kpip.core.caches import bounded_put, clear_all, memoized, register_table
from kpip.core.names import canonicalize_name
from kpip.core.versions import FINAL_SUFFIX, InvalidVersion, Version, version_of

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any


REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


EMPTY_FROZENSET: frozenset[str] = frozenset()


def safe_extra(extra: str) -> str:
    return canonicalize_name(extra)


def implementation_version_text() -> str:
    """``implementation_version`` as PEP 508 defines it.

    This is the *implementation's* version, not the language version: on PyPy
    it is 7.3.x, not 3.10.x, and markers that gate on a PyPy release are
    written against that number. Non-final builds carry the release level, so
    a CPython alpha reports ``3.15.0a1`` rather than ``3.15.0``.
    """
    info = sys.implementation.version
    version = f"{info.major}.{info.minor}.{info.micro}"
    kind = info.releaselevel
    if kind != "final":
        version += kind[0] + str(info.serial)
    return version


_TARGET_PYTHON: str | None = None


def target_python_version() -> str | None:
    """The interpreter a resolve is *for*, when it is not the one running it.

    ``None`` -- the usual case -- means the running interpreter.
    """
    return _TARGET_PYTHON


def set_target_python_version(version: str | None) -> None:
    """Resolve for ``version``: markers, Requires-Python and wheel tags.

    Process-global on purpose. The marker environment already is, and the
    alternative is threading one string through the thirty-odd
    ``marker_applies`` call sites that have no context object to carry it.
    A command sets this once, before any resolution work, and restores it
    afterwards. Every cached answer that may have been computed against the
    previous target is dropped here, since a memoized marker result or
    Requires-Python verdict is only valid for the interpreter it was asked
    about.
    """
    global _TARGET_PYTHON

    if version == _TARGET_PYTHON:
        return

    _TARGET_PYTHON = version

    clear_all()


def normalize_python_version(value: str) -> str:
    """A ``--python-version`` operand as a full PEP 440 release.

    ``"3.8"`` and ``"38"`` both mean 3.8.0: the flag names a minor series,
    and comparing a two-part version against a ``>=3.8.1`` bound would
    otherwise depend on how the operand happened to be spelled. A caller
    that means a specific patch release spells all three parts.

    A compact operand is read the way wheel tags are written, all of the
    digits after the first being the minor number: ``"310"`` is 3.10, not
    version 310, which is what comparing it as written would have meant.
    """
    if "." in value:
        parts = value.split(".")

        return f"{parts[0]}.{parts[1]}.0" if len(parts) == 2 else value

    if value.isdigit():
        if len(value) == 1:
            return f"{value}.0.0"

        return f"{value[0]}.{value[1:]}.0"

    return value


@memoized(8)
def default_environment(extra: str | None = None) -> dict[str, str]:
    import platform

    impl = platform.python_implementation()

    version = platform.python_version()

    if version.endswith("+"):
        # A build from an untagged checkout reports "3.15.0+", which is not a
        # PEP 440 version; the reference environment repairs it the same way.
        version += "local"

    implementation_version = implementation_version_text()

    if _TARGET_PYTHON is not None:
        # Only the language version moves. A cross-version resolve still runs
        # on this machine, so the platform fields stay as they are;
        # `--python-version` is not `--platform`. On CPython the two version
        # fields are the same number, so retargeting one and not the other
        # would describe an interpreter that does not exist.
        version = _TARGET_PYTHON

        if sys.implementation.name == "cpython":
            implementation_version = _TARGET_PYTHON

    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": implementation_version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": impl,
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": version,
        "python_version": ".".join(version.split(".")[:2]),
        "sys_platform": sys.platform,
        "extra": extra or "",
    }


class InvalidSpecifier(ValueError):
    """The text is not a version specifier PEP 440 permits.

    A ``ValueError`` so that callers written against the older behaviour --
    where a malformed clause surfaced as ``InvalidVersion`` or a bare
    ``ValueError`` -- keep catching it.
    """


# Operators whose operand PEP 440 restricts to a public version: no local
# label, and no `.*` prefix match. `==`/`!=` allow both; `===` is arbitrary
# text and is not parsed at all.
_PUBLIC_ONLY_OPERATORS = frozenset(("<", "<=", ">", ">=", "~="))


class Specifier:
    """One clause of a version specifier, frozen at construction.

    ``parsed_version`` is the operand as a Version -- the prefix for a
    wildcard clause, ``None`` only for ``===`` whose operand is arbitrary
    text. ``contains`` implements PEP 440's per-operator rules, including
    the ones plain ordering gets wrong: ``==``/``!=`` ignore the candidate's
    local label unless the clause names one, ``<V`` excludes V's own
    prereleases and ``>V`` excludes V's post-releases and local versions,
    wildcards and ``~=`` match release segments rather than text.
    """

    __slots__ = ("is_wildcard", "operator", "parsed_version", "version")

    operator: str
    version: str
    parsed_version: Version | None
    is_wildcard: bool

    def __init__(self, operator: str, version: str) -> None:
        wildcard = version.endswith(".*")
        _write_operator(self, operator)
        _write_version(self, version)
        if operator == "===":
            _write_is_wildcard(self, False)
            _write_parsed_version(self, None)
            return
        if wildcard and operator in _PUBLIC_ONLY_OPERATORS:
            raise InvalidSpecifier(
                f"prefix matching is only valid with == and !=: {operator}{version}",
            )
        try:
            parsed = Version(version[:-2] if wildcard else version)
        except InvalidVersion as error:
            raise InvalidSpecifier(
                f"invalid version in specifier: {operator}{version}",
            ) from error
        if wildcard:
            if parsed[2] != FINAL_SUFFIX or parsed[3]:
                raise InvalidSpecifier(
                    f"prefix matching cannot follow a pre, post, dev or local "
                    f"segment: {operator}{version}",
                )
        elif parsed[3] and operator in _PUBLIC_ONLY_OPERATORS:
            raise InvalidSpecifier(
                f"a local version label is not permitted with {operator}: "
                f"{operator}{version}",
            )
        if operator == "~=" and len(parsed.release) < 2:
            raise InvalidSpecifier(
                f"~= needs at least two release segments to have an upper "
                f"bound: ~={version}",
            )
        _write_is_wildcard(self, wildcard)
        _write_parsed_version(self, parsed)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Specifier is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Specifier is immutable (tried to delete {name!r})")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Specifier) and (
            self.operator,
            self.version,
        ) == (other.operator, other.version)

    def __hash__(self) -> int:
        return hash((self.operator, self.version))

    def __str__(self) -> str:
        return f"{self.operator}{self.version}"

    def __repr__(self) -> str:
        return f"<Specifier({self.operator!r}, {self.version!r})>"

    def contains(self, version: Version) -> bool:
        operator = self.operator
        other = self.parsed_version

        if other is None:
            return version.public == self.version

        if operator == "==":
            if self.is_wildcard:
                return _prefix_matches(version, other)
            return version == other or (
                bool(version[3]) and not other[3] and version[:3] == other[:3]
            )

        if operator == "!=":
            if self.is_wildcard:
                return not _prefix_matches(version, other)
            return version != other and not (
                bool(version[3]) and not other[3] and version[:3] == other[:3]
            )

        if operator == ">=":
            # No local strip needed: dropping the candidate's label only ever
            # lowers it, and it can only cross an inclusive lower bound where
            # the release parts are already equal -- which answers True either
            # way. `<=` is not symmetric here, so it does strip.
            return version >= other

        if operator == "<=":
            if version[3]:
                return version[:3] <= other[:3]
            return version <= other

        if operator == ">":
            if not version > other:
                return False
            suffix = version[2]
            if suffix[2] == 1 and other[2][2] == 0:
                pre_rank, pre_number = suffix[0], suffix[1]
                base_suffix = (
                    FINAL_SUFFIX
                    if pre_rank == 3
                    else (pre_rank, pre_number, 0, 0, 1, 0)
                )
                if other == (version[0], version[1], base_suffix, ()):
                    return False
            return not (version[3] and not other[3] and version[:3] == other[:3])

        if operator == "<":
            if not version < other:
                return False
            if version.is_prerelease and not other.is_prerelease:
                other_suffix = other[2]
                earliest_suffix = (
                    (-1, 0, 0, 0, 0, 0)
                    if other_suffix == FINAL_SUFFIX
                    else (3, 0, other_suffix[2], other_suffix[3], 0, 0)
                )
                if version >= (other[0], other[1], earliest_suffix, ()):
                    return False
            return True

        if operator == "~=":
            return version >= other and _prefix_matches(version, other, compatible=True)

        raise ValueError(f"unknown specifier operator: {operator}")


_write_operator = Specifier.__dict__["operator"].__set__
_write_version = Specifier.__dict__["version"].__set__
_write_is_wildcard = Specifier.__dict__["is_wildcard"].__set__
_write_parsed_version = Specifier.__dict__["parsed_version"].__set__


def _prefix_matches(
    version: Version, prefix: Version, *, compatible: bool = False
) -> bool:
    """PEP 440 prefix matching: the candidate's epoch and leading release
    segments equal the prefix's, zero-padded, ignoring its local label.

    For ``~=`` the prefix is the operand's release minus its last segment --
    the ``==X.Y.*`` half of the compatible-release clause. A single-segment
    operand (which the reference grammar rejects) reads as ``==X.*``.
    """
    if version[0] != prefix[0]:
        return False
    release = prefix.release
    if compatible:
        release = release[:-1] or release
    elif prefix[2] != FINAL_SUFFIX and version[2] != prefix[2]:
        return False
    width = len(release)
    candidate = version.release
    if len(candidate) < width:
        candidate = candidate + (0,) * (width - len(candidate))
    return candidate[:width] == release


def compatible_upper_bound_internal(version: Version) -> Version:
    """The exclusive upper bound ``~=version`` implies, epoch included."""
    release = list(version.release)
    if len(release) == 1:
        release[0] += 1
    else:
        release[-2] += 1
        release = release[:-1]
    text = ".".join(str(part) for part in release)
    return Version(f"{version[0]}!{text}" if version[0] else text)


def _bounds_of(
    specifiers: tuple[Specifier, ...],
) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]:
    """Conservative lower and upper bounds with inclusive flags.

    Drops ``!=``, ``===`` and wildcards and reads ``~=`` as its half-open
    interval; every one of those is a widening, which is what the callers
    (interval intersection in the resolver, bisection over sorted catalog
    summaries) need.
    """
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None

    for specifier in specifiers:
        operator = specifier.operator
        parsed = specifier.parsed_version
        if parsed is None or specifier.is_wildcard:
            continue
        if operator in ("==", ">=", ">", "~="):
            inclusive = operator != ">"
            if lower is None or parsed > lower[0]:
                lower = (parsed, inclusive)
            elif parsed == lower[0]:
                lower = (parsed, lower[1] and inclusive)
        if operator in ("==", "<=", "<", "~="):
            bound = (
                compatible_upper_bound_internal(parsed) if operator == "~=" else parsed
            )
            inclusive = operator in ("==", "<=")
            if upper is None or bound < upper[0]:
                upper = (bound, inclusive)
            elif bound == upper[0]:
                upper = (bound, upper[1] and inclusive)

    return lower, upper


_CONTAINS_CACHE_SIZE = 4096

_SPECIFIER_SET_CACHE_SIZE = 4096
_specifier_sets: dict[str, SpecifierSet] = register_table({})


class SpecifierSet:
    """A comma-separated list of clauses, frozen, interned by text.

    ``SpecifierSet(text)`` returns the instance already built for that text
    (whitespace-stripped) while it is in the table; ``SpecifierSet()`` is
    the shared empty set. Everything derived from the clauses is computed
    once here:

    * ``text`` -- the canonical spelling (clauses joined by commas);
    * ``bounds`` -- conservative ``(lower, upper)`` with inclusive flags;
    * ``exact_version`` -- the one release a sole ``==`` clause names, or
      None (a wildcard, ``===``, a range or several clauses name no unique
      release);
    * ``is_pinned`` -- some ``==``/``===`` clause without a wildcard, so
      the set admits at most one release (the yank/hash policy question);
    * ``explicitly_allows_prereleases`` -- some clause other than ``!=``
      or ``===`` names a prerelease, which per PEP 440 opts the set in to
      prereleases without the caller asking.
    """

    __slots__ = (
        "_bounds",
        "_contains",
        "_contains_with_prereleases",
        "_exact_version",
        "_explicitly_allows_prereleases",
        "_has_arbitrary",
        "_is_pinned",
        "_text",
        "specifiers",
    )

    specifiers: tuple[Specifier, ...]
    _text: str
    _bounds: tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]
    _exact_version: Version | None
    _has_arbitrary: bool
    _is_pinned: bool
    _explicitly_allows_prereleases: bool
    _contains: dict[Version | str, bool]
    _contains_with_prereleases: dict[Version | str, bool]

    def __new__(cls, value: str = "") -> SpecifierSet:
        key = value.strip()
        cached = _specifier_sets.get(key)
        if cached is not None:
            return cached

        clauses: list[Specifier] = []
        has_arbitrary = False
        for part in key.split(","):
            clause = part.strip()
            if not clause:
                continue
            first = clause[0]
            if first == "=":
                operator = "===" if clause.startswith("===") else "=="
                if operator == "==" and not clause.startswith("=="):
                    raise ValueError(f"invalid version specifier: {value!r}")
            elif first == "!":
                if not clause.startswith("!="):
                    raise ValueError(f"invalid version specifier: {value!r}")
                operator = "!="
            elif first == "~":
                if not clause.startswith("~="):
                    raise ValueError(f"invalid version specifier: {value!r}")
                operator = "~="
            elif first == "<":
                operator = "<=" if clause.startswith("<=") else "<"
            elif first == ">":
                operator = ">=" if clause.startswith(">=") else ">"
            else:
                raise ValueError(f"invalid version specifier: {value!r}")
            version = clause[len(operator) :].strip()
            if not version:
                raise ValueError(f"invalid version specifier: {value!r}")
            if operator == "===":
                has_arbitrary = True
            clauses.append(Specifier(operator, version))
        specifiers = tuple(clauses)
        if key and not specifiers:
            raise ValueError(f"invalid version specifier: {value!r}")

        self = object.__new__(cls)
        _write_specifiers(self, specifiers)
        object.__setattr__(self, "_has_arbitrary", has_arbitrary)
        if len(_specifier_sets) >= _SPECIFIER_SET_CACHE_SIZE:
            _specifier_sets.clear()
        _specifier_sets[key] = self
        return self

    @property
    def text(self) -> str:
        """The canonical spelling: clauses joined by commas."""
        try:
            return self._text
        except AttributeError:
            text = ",".join([f"{s.operator}{s.version}" for s in self.specifiers])
            object.__setattr__(self, "_text", text)
            return text

    @property
    def bounds(self) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]:
        """Conservative ``(lower, upper)`` bounds with inclusive flags."""
        try:
            return self._bounds
        except AttributeError:
            bounds = _bounds_of(self.specifiers)
            object.__setattr__(self, "_bounds", bounds)
            return bounds

    @property
    def exact_version(self) -> Version | None:
        """The one release a sole ``==`` clause names, or None: a wildcard,
        ``===``, a range or several clauses name no unique release."""
        try:
            return self._exact_version
        except AttributeError:
            exact = None
            if len(self.specifiers) == 1:
                only = self.specifiers[0]
                if only.operator == "==" and not only.is_wildcard:
                    exact = only.parsed_version
            object.__setattr__(self, "_exact_version", exact)
            return exact

    @property
    def has_arbitrary_clause(self) -> bool:
        """Some ``===`` clause, whose operand is text rather than a version.

        This decides how the containment memo is keyed. Every other operator
        depends only on the comparison tuple, which is what a Version hashes
        as; ``===`` compares the canonical *spelling*, and 1.0 and 1.0.0 are
        one key but two spellings. Keying everything by text would cost a
        property call and a string hash on the hot path for the sake of an
        operator almost nothing uses.
        """
        return self._has_arbitrary

    @property
    def is_pinned(self) -> bool:
        """Some ``==``/``===`` clause without a wildcard: the set admits at
        most one release (the yank and hash policy question)."""
        try:
            return self._is_pinned
        except AttributeError:
            pinned = False
            for specifier in self.specifiers:
                if specifier.operator in ("==", "===") and not specifier.is_wildcard:
                    pinned = True
                    break
            object.__setattr__(self, "_is_pinned", pinned)
            return pinned

    @property
    def explicitly_allows_prereleases(self) -> bool:
        """Some clause other than ``!=``/``===`` names a prerelease, which
        per PEP 440 opts the set in to prereleases without being asked."""
        try:
            return self._explicitly_allows_prereleases
        except AttributeError:
            allowed = False
            for specifier in self.specifiers:
                if specifier.operator == "!=":
                    continue
                parsed = specifier.parsed_version
                if parsed is None:
                    parsed = version_of(specifier.version)
                if parsed is not None and parsed.is_prerelease:
                    allowed = True
                    break
            object.__setattr__(self, "_explicitly_allows_prereleases", allowed)
            return allowed

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"SpecifierSet is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"SpecifierSet is immutable (tried to delete {name!r})")

    def __reduce__(self) -> tuple[Any, ...]:
        return (SpecifierSet, (self.text,))

    def contains(
        self,
        version: Version | str,
        *,
        allow_prereleases: bool = False,
    ) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)
        specifiers = self.specifiers

        if not specifiers and not parsed.is_prerelease:
            return True

        try:
            cache = (
                self._contains_with_prereleases if allow_prereleases else self._contains
            )
        except AttributeError:
            cache = {}
            object.__setattr__(
                self,
                "_contains_with_prereleases" if allow_prereleases else "_contains",
                cache,
            )
        key: Version | str = parsed.public if self._has_arbitrary else parsed
        cached = cache.get(key)
        if cached is not None:
            return cached

        if (
            parsed.is_prerelease
            and not allow_prereleases
            and not self.explicitly_allows_prereleases
        ):
            result = False
        else:
            result = True
            for specifier in specifiers:
                if not specifier.contains(parsed):
                    result = False
                    break

        bounded_put(cache, key, result, _CONTAINS_CACHE_SIZE)
        return result

    def __bool__(self) -> bool:
        return bool(self.specifiers)

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"<SpecifierSet({self.text!r})>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SpecifierSet):
            return NotImplemented
        return self.specifiers == other.specifiers

    def __hash__(self) -> int:
        return hash(self.specifiers)


_write_specifiers = SpecifierSet.__dict__["specifiers"].__set__
_write_is_pinned = SpecifierSet.__dict__["is_pinned"].__set__
_write_exact_version = SpecifierSet.__dict__["exact_version"].__set__
_write_contains = SpecifierSet.__dict__["_contains"].__set__
_write_contains_with_prereleases = SpecifierSet.__dict__[
    "_contains_with_prereleases"
].__set__


class Requirement:
    """A parsed PEP 508 requirement, frozen.

    Identity is ``(canonical_name, specifier, extras, url, marker)``; ``raw``
    is provenance -- the text as read -- and two requirements that differ
    only in spelling or whitespace are equal.
    """

    __slots__ = (
        "_canonical_marker",
        "_canonical_name",
        "_is_unnamed_direct",
        "extras",
        "marker",
        "name",
        "raw",
        "specifier",
        "url",
    )

    name: str
    _canonical_name: str
    _canonical_marker: str
    specifier: SpecifierSet
    extras: frozenset[str]
    url: str | None
    marker: str | None
    raw: str
    _is_unnamed_direct: bool

    def __init__(
        self,
        name: str,
        specifier: SpecifierSet,
        extras: frozenset[str],
        url: str | None = None,
        marker: str | None = None,
        raw: str = "",
    ) -> None:
        _write_name(self, name)
        _write_specifier(self, specifier)
        _write_extras(self, extras)
        _write_url(self, url)
        _write_marker(self, marker)
        _write_raw(self, raw)

    @property
    def canonical_name(self) -> str:
        """PEP 503 normalised name, computed on first read.

        Not computed at construction: parsing a Requires-Dist line must not
        pay a name normalisation the caller may never ask for, and the
        interned Requirement pays it at most once.
        """
        try:
            return self._canonical_name
        except AttributeError:
            canonical = canonicalize_name(self.name)
            object.__setattr__(self, "_canonical_name", canonical)
            return canonical

    @property
    def is_unnamed_direct(self) -> bool:
        """Whether this requirement locates an artifact rather than naming one.

        A URL requirement or a bare local path has no metadata to trust until
        the artifact is fetched, so callers that would otherwise reject on a
        name/version mismatch defer that check. Computed on first read.
        """
        try:
            return self._is_unnamed_direct
        except AttributeError:
            raw = self.raw
            result = (
                self.url is not None
                or raw.startswith(("file:", ".", "/", "~"))
                or is_windows_path(raw)
            )
            object.__setattr__(self, "_is_unnamed_direct", result)
            return result

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Requirement is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Requirement is immutable (tried to delete {name!r})")

    @property
    def canonical_marker(self) -> str:
        """The marker in its canonical spelling, computed on first read.

        Identity has to be about meaning, not about how the metadata happened
        to be typed: ``python_version<'3.7'`` and ``python_version < "3.7"``
        are the same guard, and requirements carrying them must dedupe.
        """
        try:
            return self._canonical_marker
        except AttributeError:
            from kpip.core.markers import canonical_marker

            text = canonical_marker(self.marker) if self.marker else ""
            object.__setattr__(self, "_canonical_marker", text)
            return text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Requirement) and (
            self.canonical_name,
            self.specifier,
            self.extras,
            self.url,
            self.canonical_marker,
        ) == (
            other.canonical_name,
            other.specifier,
            other.extras,
            other.url,
            other.canonical_marker,
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.canonical_name,
                self.specifier,
                self.extras,
                self.url,
                self.canonical_marker,
            )
        )

    def copy_with(self, **changes: object) -> Requirement:
        values = {
            "name": self.name,
            "specifier": self.specifier,
            "extras": self.extras,
            "url": self.url,
            "marker": self.marker,
            "raw": self.raw,
        }
        values.update(changes)
        return type(self)(**values)  # type: ignore[arg-type]

    def is_satisfied_by(
        self,
        version: str | Version,
        *,
        allow_prereleases: bool = True,
    ) -> bool:
        if not self.specifier.specifiers:
            parsed = version if isinstance(version, Version) else Version(version)
            return allow_prereleases or not parsed.is_prerelease
        return self.specifier.contains(version, allow_prereleases=allow_prereleases)

    def __str__(self) -> str:
        parts = [self.name]
        if self.extras:
            parts.append("[" + ",".join(sorted(self.extras)) + "]")
        if self.url is not None:
            parts.append(" @ " + self.url)
        else:
            parts.append(str(self.specifier))
        if self.marker:
            parts.append("; " + self.canonical_marker)
        return "".join(parts)

    def __repr__(self) -> str:
        return f"<Requirement({str(self)!r})>"


_write_name = Requirement.__dict__["name"].__set__
_write_specifier = Requirement.__dict__["specifier"].__set__
_write_extras = Requirement.__dict__["extras"].__set__
_write_url = Requirement.__dict__["url"].__set__
_write_marker = Requirement.__dict__["marker"].__set__
_write_raw = Requirement.__dict__["raw"].__set__


@memoized(16384)
def parse_requirement(value: str) -> Requirement:
    raw = value.strip()

    if not raw:
        raise ValueError("empty requirement")

    if looks_like_direct_reference(raw):
        egg_name, egg_extras = egg_fragment_internal(raw)

        if egg_name is not None:
            inferred = project_from_direct_reference(raw)

            if inferred is not None and raw.split("#", 1)[0].lower().endswith(".whl"):
                name, version = inferred

                specifier = SpecifierSet(f"=={version}") if version else SpecifierSet()

                return Requirement(
                    name=name,
                    extras=egg_extras,
                    specifier=specifier,
                    url=raw if looks_like_url(raw) else None,
                    marker=None,
                    raw=raw,
                )

            return Requirement(
                name=egg_name,
                extras=egg_extras,
                specifier=SpecifierSet(),
                url=raw if looks_like_url(raw) else None,
                marker=None,
                raw=raw,
            )

        inferred = project_from_direct_reference(raw)

        if inferred is not None:
            name, version = inferred

            specifier = SpecifierSet(f"=={version}") if version else SpecifierSet()

            return Requirement(
                name=name,
                extras=EMPTY_FROZENSET,
                specifier=specifier,
                url=raw if looks_like_url(raw) else None,
                marker=None,
                raw=raw,
            )

        name = raw
        if looks_like_url(raw):
            import urllib.parse

            path = urllib.parse.unquote(urllib.parse.urlsplit(raw).path)
            name = path.rstrip("/").rsplit("/", 1)[-1] or raw

        return Requirement(
            name=name,
            extras=EMPTY_FROZENSET,
            specifier=SpecifierSet(),
            url=raw if looks_like_url(raw) else None,
            marker=None,
            raw=raw,
        )

    req_part, marker = split_marker(raw)

    name_match = REQ_NAME_RE.match(req_part)

    if name_match is None:
        raise ValueError(f"invalid requirement: {value!r}")

    name = name_match.group(1)

    rest = req_part[name_match.end() :].strip()

    extras: frozenset[str] = EMPTY_FROZENSET

    first = rest[:1]

    if first == "[":
        end = rest.find("]")

        if end == -1:
            raise ValueError(f"invalid extras in requirement: {value!r}")

        extras = frozenset(
            safe_extra(part.strip()) for part in rest[1:end].split(",") if part.strip()
        )

        rest = rest[end + 1 :].strip()

        first = rest[:1]

    url: str | None = None

    if first == "@":
        url = rest[1:].strip()

        spec = ""

    else:
        if first == "(" and rest.endswith(")"):
            rest = rest[1:-1].strip()

        spec = rest

        if spec and ("[" in spec or "]" in spec):
            raise ValueError(f"invalid version specifier: {value!r}")

    return Requirement(name, SpecifierSet(spec), extras, url, marker, raw)


def canonicalize_requirement(value: str) -> str:
    """Return a stable textual form of a PEP 508 requirement."""

    requirement = parse_requirement(value.strip())

    parts = [requirement.canonical_name]

    if requirement.extras:
        parts.append(f"[{','.join(sorted(requirement.extras))}]")

    if requirement.url:
        parts.append(f" @ {requirement.url}")

    elif requirement.specifier:
        parts.append(requirement.specifier.text)

    if requirement.marker:
        parts.append(f"; {requirement.marker}")

    return "".join(parts)


def looks_like_direct_reference(value: str) -> bool:
    if ":" not in value and value[:1] not in (".", "/", "~"):
        return False
    return (
        looks_like_url(value)
        or value.startswith((".", "/", "~"))
        or is_windows_path(value)
    )


def looks_like_url(value: str) -> bool:
    if is_windows_path(value):
        return False

    if ":" not in value:
        return False

    # Reached only by a value that could be a URL. ``urllib.parse`` pulls
    # ``ipaddress`` behind it, and a requirement that names a project --
    # which is nearly all of them -- has already returned above.
    import urllib.parse

    parsed = urllib.parse.urlparse(value)

    return bool(parsed.scheme and (parsed.netloc or parsed.path))


def is_windows_path(value: str) -> bool:
    return (
        len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in "/\\"
    )


def project_from_direct_reference(value: str) -> tuple[str, str | None] | None:
    import urllib.parse

    parsed = urllib.parse.urlparse(value)

    filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])

    if not filename:
        return None

    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")

        if len(parts) >= 5 and parts[0] and parts[1]:
            return parts[0].replace("_", "-"), parts[1]

        return None

    stem = filename

    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tgz", ".zip"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]

            break

    else:
        return None

    name, separator, version = stem.rpartition("-")

    if not separator or not name or not version:
        return None

    return name.replace("_", "-"), version


def egg_fragment_internal(value: str) -> tuple[str | None, frozenset[str]]:
    import urllib.parse

    fragment = urllib.parse.urlparse(value).fragment

    if not fragment:
        return None, EMPTY_FROZENSET

    for key, raw_name in urllib.parse.parse_qsl(fragment, keep_blank_values=True):
        if key != "egg" or not raw_name:
            continue

        name = urllib.parse.unquote(raw_name)

        extras: frozenset[str] = EMPTY_FROZENSET

        if "[" in name and name.endswith("]"):
            name, extras_text = name[:-1].split("[", 1)

            extras = frozenset(
                safe_extra(part.strip())
                for part in extras_text.split(",")
                if part.strip()
            )

        if REQ_NAME_RE.fullmatch(name):
            return name, extras

    return None, EMPTY_FROZENSET


def split_marker(value: str) -> tuple[str, str | None]:
    semicolon = value.find(";")

    if semicolon == -1:
        return value, None

    head = value[:semicolon]

    if "'" not in head and '"' not in head:
        return head.strip(), value[semicolon + 1 :].strip()

    in_quote: str | None = None

    for index, char in enumerate(value):
        if char in {"'", '"'}:
            in_quote = None if in_quote == char else char

        elif char == ";" and in_quote is None:
            return value[:index].strip(), value[index + 1 :].strip()

    return value, None


def marker_applies(marker: str | None, *, extras: Iterable[str] = ()) -> bool:
    """Whether a requirement guarded by ``marker`` applies here.

    Extras are evaluated the way pip evaluates them: the marker is checked
    once per requested extra with ``extra`` bound to that name, and the
    results are OR-ed. A requirement with no extras requested is checked once
    with ``extra`` bound to the empty string. Binding a *set* instead would
    get ``extra != "x"`` wrong whenever more than one extra is requested.
    """
    if not marker:
        return True

    normalized_extras = tuple(sorted({safe_extra(extra) for extra in extras if extra}))

    return _marker_applies_cached(marker, normalized_extras)


@memoized(4096)
def _marker_applies_cached(marker: str, extras: tuple[str, ...]) -> bool:
    return marker_applies_internal(marker, default_environment(), set(extras))


def marker_applies_internal(
    marker: str,
    env: dict[str, str],
    extras: set[str],
) -> bool:
    """Evaluate ``marker`` against ``env``, once per extra in ``extras``.

    An unparseable marker, or one naming a variable this environment does not
    define, is treated as not applying rather than raising: a single bad
    Requires-Dist line in one package's metadata must not abort a resolve.
    """
    from kpip.core.markers import (
        InvalidMarker,
        UndefinedComparison,
        UndefinedEnvironmentName,
        evaluate_marker,
        parse_marker,
    )

    try:
        tree = parse_marker(marker)
    except InvalidMarker:
        return False

    contexts = [{**env, "extra": extra} for extra in extras] or [{**env, "extra": ""}]

    for context in contexts:
        try:
            if evaluate_marker(tree, context):
                return True
        except (UndefinedEnvironmentName, UndefinedComparison):
            continue

    return False
