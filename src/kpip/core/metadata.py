from __future__ import annotations

import os
import site
import sys
from collections.abc import Collection, Iterable

from .packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from .names import installed_name_might_match
from .versions import Version, version_of
from .wheel_metadata import parse_metadata_headers

TYPE_CHECKING = False

if TYPE_CHECKING:
    import importlib.metadata
    import pathlib
    from email.message import Message
    from typing import Protocol, TypeGuard

    HeaderIdentity = tuple[str, int, int]

    class HeaderCache(Protocol):
        """What the installed-state scan asks of a persistent header store.

        ``index/metadata_cache.py:WheelMetadataCache`` is the implementation;
        the identity is that module's ``metadata_identity`` shape -- absolute
        path, size, mtime in nanoseconds -- so one table serves both local
        wheels and installed METADATA files.
        """

        def prefetch(self, identities: Iterable[HeaderIdentity]) -> None: ...

        def get_reference(
            self, identity: HeaderIdentity
        ) -> dict[str, list[str]] | None: ...

        def put(
            self, identity: HeaderIdentity, headers: dict[str, list[str]]
        ) -> None: ...


stdlib_pkgs = {"python", "wsgiref", "argparse"}


def _read_text_file(target: str) -> str | None:
    try:
        with open(target, encoding="utf-8") as file:
            return file.read()

    except (
        FileNotFoundError,
        IsADirectoryError,
        NotADirectoryError,
        PermissionError,
    ):
        return None


def is_loaded_path_internal(value: object) -> TypeGuard[pathlib.Path]:
    """Whether ``value`` is a ``pathlib.Path``, asked without importing one.

    ``sys.modules`` is the whole test: a ``Path`` cannot exist unless
    ``pathlib`` has been imported, so an absent module is a definite no and
    costs nothing to establish. That matters because ``pathlib`` is not a
    small import -- ``zipfile`` reaches it too -- and the distributions kpip
    builds itself carry plain strings, so the answer here is almost always
    reached by the cheap half of the ``or``.
    """
    module = sys.modules.get("pathlib")

    return module is not None and isinstance(value, module.Path)


def _read_raw_metadata_text(
    raw: RawDistribution,
) -> str | None:
    """Read the metadata file text through the same fallback chain as
    ``importlib.metadata.Distribution.metadata``: ``METADATA``, then
    ``PKG-INFO`` for sdist-built distributions, then the bare dist-info path
    for old egg-info installs that have neither.

    ``Distribution.read_text`` goes through ``pathlib.Path.joinpath`` and
    ``Path.read_text`` for every candidate filename, which is real overhead
    across a whole-environment scan. Plain filesystem paths read through
    ``open()`` equivalently and more cheaply. Anything else (a zipped egg,
    or a custom finder's own path type) keeps the general chain unchanged.
    """
    path = getattr(raw, "_path", None)

    if isinstance(path, str) or is_loaded_path_internal(path):
        base = os.fspath(path)

        for filename in ("METADATA", "PKG-INFO", ""):
            target = base if not filename else os.path.join(base, filename)

            text = _read_text_file(target)

            if text:
                return text

        return None

    return raw.read_text("METADATA") or raw.read_text("PKG-INFO") or raw.read_text("")


class PathDistribution:
    """A ``*.dist-info`` or ``*.egg-info`` entry found under a search path.

    The cheap stand-in for ``importlib.metadata.PathDistribution``, answering
    the same ``_path``, ``read_text`` and ``locate_file`` that kpip reads
    itself; the parsed ``metadata`` message and ``files`` are delegated to
    the real one, built on first use, so they behave identically while the
    commands that never touch them skip that module's import.
    """

    __slots__ = ("_path", "_stdlib")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)

        self._stdlib: importlib.metadata.PathDistribution | None = None

    def read_text(self, filename: str) -> str | None:
        return _read_text_file(os.path.join(self._path, filename))

    def locate_file(self, path: str | os.PathLike[str]) -> pathlib.Path:
        import pathlib

        return pathlib.Path(os.path.dirname(self._path), path)

    @property
    def stdlib(self) -> importlib.metadata.PathDistribution:
        if self._stdlib is None:
            import importlib.metadata
            import pathlib

            self._stdlib = importlib.metadata.PathDistribution(pathlib.Path(self._path))

        return self._stdlib

    @property
    def metadata(self) -> importlib.metadata.PackageMetadata:
        return self.stdlib.metadata

    @property
    def files(self) -> list[importlib.metadata.PackagePath] | None:
        return self.stdlib.files


if TYPE_CHECKING:
    RawDistribution = importlib.metadata.Distribution | PathDistribution


class InstalledDistribution:
    """An installed distribution as found on disk.

    ``raw_version`` is the text in its METADATA; ``version`` is that text
    as a Version, or None when it is not a PEP 440 version (a legacy
    package), which every comparison reads as "not that version" so that
    inspection and removal keep working for such packages.
    """

    __slots__ = (
        "_fast_headers",
        "location",
        "metadata_location",
        "name",
        "raw",
        "raw_version",
        "version",
    )

    def __init__(
        self,
        name: str,
        version: str,
        location: str,
        metadata_location: str | None,
        raw: RawDistribution,
    ) -> None:
        self.name = name

        self.raw_version = version

        self.version = version_of(version)

        self.location = location

        self.metadata_location = metadata_location

        self.raw = raw

        self._fast_headers: dict[str, list[str]] | None = None

    name: str

    raw_version: str

    version: Version | None

    location: str

    metadata_location: str | None

    raw: RawDistribution

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    @property
    def metadata(self) -> Message | importlib.metadata.PackageMetadata:
        return self.raw.metadata

    def _fast_metadata_headers(self) -> dict[str, list[str]]:
        """Read Requires-Dist/Name/Version through the wheel-metadata fast path.

        ``self.raw.metadata`` parses the file through the full RFC822 email
        machinery the first time it's touched -- expensive, and paid by every
        already-installed distribution ``dependencies()`` inspects during
        resolution. ``parse_metadata_headers`` already does this reliably for
        candidate wheels pulled from PyPI; installed distributions use the
        identical METADATA format, so it's just as trustworthy here.
        """

        headers = self._fast_headers

        if headers is None:
            headers = parse_metadata_headers(_read_raw_metadata_text(self.raw) or "")

            self._fast_headers = headers

        return headers

    def dependencies(self, extras: Iterable[str] = ()) -> list[Requirement]:
        result: list[Requirement] = []

        for value in self._fast_metadata_headers().get("requires-dist", []):
            req = parse_requirement(value)

            if marker_applies(req.marker, extras=extras):
                result.append(req)

        return result

    def read_text(self, name: str) -> str:
        text = self.raw.read_text(name)

        if text is None:
            raise FileNotFoundError(name)

        return text

    def files(self) -> list[str]:
        files = self.raw.files or ()

        return sorted(str(file) for file in files)


def default_lib_path() -> str:
    import sysconfig

    return sysconfig.get_paths()["purelib"]


def user_lib_path() -> str:
    return site.getusersitepackages()


_INFO_SUFFIXES = (".dist-info", ".egg-info")


def _iter_raw_distributions(
    paths: Iterable[str] | None,
    canonical_names: set[str] | None = None,
) -> Iterable[RawDistribution]:
    """Every metadata entry ``importlib.metadata.distributions`` would find.

    That function costs ~12 ms to import (``inspect``, ``email`` and
    friends), paid by every default install just to list ``*.dist-info``
    directories. A plain directory root is listed here the way the stdlib's
    ``FastPath``/``Lookup`` list it -- case-insensitive suffix match on the
    child name, no other filter -- and every other shape keeps going through
    the stdlib so its answer is unchanged: a root that is a file (a zip or
    egg on ``sys.path``), a ``*.egg`` directory (whose ``EGG-INFO`` child
    counts too), and a default scan while any other finder on
    ``sys.meta_path`` offers ``find_distributions``.

    ``canonical_names`` skips directories that cannot be metadata for one of
    them, judged from the child name alone. That is the whole point: the
    caller filters again on the parsed ``Name``, but only after reading and
    parsing a ``METADATA`` file, and in an environment of any size almost all
    of those reads are for distributions nobody asked about. Shapes that do
    not go through a plain directory listing -- a zip on ``sys.path``, an
    ``.egg`` directory, another ``sys.meta_path`` finder -- are yielded
    unfiltered, since there is no name to judge before opening them.
    """

    if paths is None:
        from importlib.machinery import PathFinder

        if any(
            finder is not PathFinder and hasattr(finder, "find_distributions")
            for finder in sys.meta_path
        ):
            import importlib.metadata

            yield from importlib.metadata.distributions()

            return

        roots = [os.fspath(entry) for entry in sys.path]

    else:
        roots = [os.fspath(path) for path in paths]

    for root in roots:
        try:
            children = os.listdir(root or ".")

        except OSError:
            if os.path.isfile(root):
                import importlib.metadata

                yield from importlib.metadata.distributions(path=[root])

            continue

        if os.path.basename(root).lower().endswith(".egg"):
            import importlib.metadata

            yield from importlib.metadata.distributions(path=[root])

            continue

        for child in children:
            lowered = child.lower()

            for suffix in _INFO_SUFFIXES:
                if not lowered.endswith(suffix):
                    continue

                if canonical_names is not None and not installed_name_might_match(
                    child,
                    suffix,
                    canonical_names,
                ):
                    break

                yield PathDistribution(os.path.normpath(os.path.join(root, child)))

                break


_header_cache: HeaderCache | None = None


def use_header_cache(cache: HeaderCache | None) -> None:
    global _header_cache

    _header_cache = cache


def _metadata_file_identity(base: str) -> HeaderIdentity | None:
    """The header-cache key of ``<dist-info>/METADATA`` -- the same shape as
    ``index/metadata_cache.py:metadata_identity`` -- or None without one."""
    target = os.path.join(base, "METADATA")

    try:
        stat = os.stat(target)

    except OSError:
        return None

    return (os.path.abspath(target), stat.st_size, stat.st_mtime_ns)


def _iter_installed_distributions(
    paths: Iterable[str] | None = None,
    names: Collection[str] | None = None,
) -> Iterable[InstalledDistribution]:
    canonical_names = (
        {canonicalize_name(name) for name in names} if names is not None else None
    )

    cache = _header_cache

    found: Iterable[RawDistribution] = _iter_raw_distributions(
        paths,
        canonical_names,
    )

    identities: dict[int, HeaderIdentity] = {}

    if cache is not None:
        found = list(found)

        for dist in found:
            path = getattr(dist, "_path", None)

            if isinstance(path, str) or is_loaded_path_internal(path):
                identity = _metadata_file_identity(os.fspath(path))

                if identity is not None:
                    identities[id(dist)] = identity

        cache.prefetch(identities.values())

    for dist in found:
        headers = None

        identity = identities.get(id(dist))

        if identity is not None and cache is not None:
            headers = cache.get_reference(identity)

            if headers is None:
                text = _read_text_file(identity[0])

                if text:
                    headers = parse_metadata_headers(text)

                    cache.put(identity, headers)

        if headers is None:
            text = _read_raw_metadata_text(dist)

            if text is None:
                continue

            headers = parse_metadata_headers(text)

        name = headers.get("name", [None])[0]

        version = headers.get("version", [None])[0]

        if not name or not version:
            continue

        if (
            canonical_names is not None
            and canonicalize_name(name) not in canonical_names
        ):
            continue

        metadata_location = getattr(dist, "_path", None)

        location = (
            os.path.dirname(dist._path) or os.curdir
            if isinstance(dist, PathDistribution)
            else str(dist.locate_file(""))
        )

        if metadata_location is None or str(location) == "<memory>":
            continue

        distribution = InstalledDistribution(
            name=name,
            version=str(version),
            location=location,
            metadata_location=(str(metadata_location) if metadata_location else None),
            raw=dist,
        )

        distribution._fast_headers = headers

        yield distribution


def iter_installed_distributions(
    paths: Iterable[str] | None = None,
    *,
    names: Collection[str] | None = None,
) -> list[InstalledDistribution]:
    return sorted(
        _iter_installed_distributions(paths, names),
        key=lambda dist: dist.canonical_name,
    )


_InstalledIndex = dict[str, InstalledDistribution]
_installed_index_cache: dict[
    tuple[bool, tuple[str, ...]], tuple[tuple[int | None, ...], _InstalledIndex]
] = {}


def _search_paths(paths: Iterable[str] | None) -> tuple[str, ...]:
    if paths is None:
        return tuple(os.fspath(entry) for entry in sys.path)

    return tuple(os.fspath(path) for path in paths)


def _paths_generation(search_paths: tuple[str, ...]) -> tuple[int | None, ...]:
    generation: list[int | None] = []

    for entry in search_paths:
        try:
            generation.append(os.stat(entry).st_mtime_ns)

        except OSError:
            generation.append(None)

    return tuple(generation)


def installed_index(paths: Iterable[str] | None = None) -> _InstalledIndex:
    """Installed distributions by canonical name, first match on the path wins."""
    search_paths = _search_paths(paths)

    generation = _paths_generation(search_paths)

    cache_key = (paths is None, search_paths)

    cached = _installed_index_cache.get(cache_key)

    if cached is not None and cached[0] == generation:
        return cached[1]

    index: _InstalledIndex = {}

    for dist in _iter_installed_distributions(None if paths is None else search_paths):
        index.setdefault(dist.canonical_name, dist)

    _installed_index_cache[cache_key] = (generation, index)

    return index


def clear_installed_index() -> None:
    """Forget every cached environment scan (tests and in-process installers)."""
    _installed_index_cache.clear()


def find_installed(
    name: str,
    paths: Iterable[str] | None = None,
) -> InstalledDistribution | None:
    return installed_index(paths).get(canonicalize_name(name))
