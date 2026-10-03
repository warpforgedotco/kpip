from __future__ import annotations

import logging
import os
import platform
import re
import sys
import zipfile
from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Protocol

from kpip.core.archive import WheelArchive, WheelhouseUnavailable

from .caches import bounded_put, memoized, register_table
from .errors import InstallationError, InvalidWheelFilename, UnsupportedWheel
from .light_metadata import LightMetadata, parse_metadata_text
from .packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from kpip.host import interpreter_facts
from .versions import InvalidVersion, Version
from .wheel_metadata import (
    metadata_paths,
    parse_metadata_member,
)

if TYPE_CHECKING:
    import email.parser
    from email.message import Message
    from typing import IO, Any, NoReturn

    class ZipEntryInfo(Protocol):
        """The subset of ``zipfile.ZipInfo`` these functions read.

        Satisfied structurally by both ``zipfile.ZipInfo`` and lighter adapters
        over faster archive readers, so this module never needs to know such an
        adapter exists. Declared as read-only properties (rather than plain
        attributes) so an immutable adapter -- e.g. a ``NamedTuple`` -- also
        satisfies it: a plain attribute requires write support too, which a
        read-only field doesn't have.
        """

        @property
        def CRC(self) -> int: ...

        @property
        def compress_type(self) -> int: ...

        @property
        def external_attr(self) -> int: ...

        @property
        def compress_size(self) -> int: ...

        @property
        def file_size(self) -> int: ...

        @property
        def header_offset(self) -> int: ...

    class ZipArchiveSource(Protocol):
        """The subset of ``zipfile.ZipFile`` these functions read from."""

        @property
        def NameToInfo(self) -> Mapping[str, ZipEntryInfo]: ...

        def getinfo(self, name: str) -> ZipEntryInfo: ...

        def read(self, name: str) -> bytes: ...

        def namelist(self) -> list[str]: ...

        def open(self, name: str) -> IO[bytes]: ...

    class MetadataArchiveSource(Protocol):
        """The smaller archive surface needed for core metadata."""

        def read(self, name: str) -> bytes: ...

        def namelist(self) -> list[str]: ...

    class MetadataCache(Protocol):
        """Minimal cache contract needed by wheel parsing."""

        def get_reference(
            self,
            identity: tuple[str, int, int],
        ) -> dict[str, list[str]] | None: ...

        def put(
            self,
            identity: tuple[str, int, int],
            headers: dict[str, list[str]],
        ) -> None: ...


class PureWheelCandidate:
    """What an installer reads from a resolved wheel candidate."""

    __slots__ = ()

    canonical_name: str

    path: str


MACOS_COMPATIBLE_ARCHES = {
    "x86_64": frozenset(
        ("x86_64", "intel", "fat64", "fat32", "universal2", "universal")
    ),
    "i386": frozenset(("i386", "intel", "fat32", "fat", "universal")),
    "intel": frozenset(("intel", "fat64", "fat32", "universal")),
    "arm64": frozenset(("arm64", "universal2")),
    "aarch64": frozenset(("aarch64", "universal2")),
    "ppc": frozenset(("ppc", "fat32", "fat", "universal")),
    "ppc64": frozenset(("ppc64", "fat64", "universal")),
    "universal": frozenset(("universal",)),
    "universal2": frozenset(("universal2",)),
}

_LEGACY_MANYLINUX_GLIBC = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}


def linux_platform_parts(platform_tag: str) -> tuple[str, int, int, str] | None:
    """``(family, major, minor, arch)`` for a manylinux or musllinux tag.

    Both the PEP 600 spelling (``manylinux_2_17_x86_64``) and the three
    pre-600 aliases (``manylinux2014_x86_64``) reduce to the same shape, so
    the matcher never has to know which spelling a wheel happened to use.
    Returns None for anything that is not one of those tags.
    """
    if platform_tag.startswith("manylinux_") or platform_tag.startswith("musllinux_"):
        fields = platform_tag.split("_", 3)
        if len(fields) != 4:
            return None
        family, major, minor, arch = fields
        if not major.isdigit() or not minor.isdigit() or not arch:
            return None
        return (family, int(major), int(minor), arch)

    prefix, _, arch = platform_tag.partition("_")
    glibc = _LEGACY_MANYLINUX_GLIBC.get(prefix)
    if glibc is None or not arch:
        return None
    return ("manylinux", glibc[0], glibc[1], arch)


def Parser() -> email.parser.Parser:
    """Lazily construct the legacy email parser.

    The import is deferred as well: few installs parse a METADATA file this
    way.
    """
    import email.parser

    return email.parser.Parser()


_UNRESOLVED = object()


class LazyWheelLayout:
    """A wheel layout computed on first use.

    The resolver already holds a local wheel's metadata, and a warm install
    finds its unpacked tree in the archive cache, so the layout -- which
    costs opening the wheel and parsing its directory -- is only needed by
    the paths that extract or copy members. The computed value is memoized
    on this object, so copies of the candidate share one read.
    """

    __slots__ = ("_compute", "_value")

    def __init__(self, compute: Callable[[], object | None]) -> None:
        self._compute: Callable[[], object | None] | None = compute
        self._value: object = _UNRESOLVED

    def resolve(self) -> object | None:
        if self._value is _UNRESOLVED:
            assert self._compute is not None
            self._value = self._compute()
            self._compute = None
        return self._value


class WheelCandidate(PureWheelCandidate):
    __slots__ = (
        "_wheel_layout",
        "dependencies",
        "from_cache",
        "name",
        "path",
        "provided_extras",
        "requires_python",
        "source_hashes",
        "source_kind",
        "source_url",
        "source_vcs",
        "version",
        "yanked_reason",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        path: str,
        dependencies: tuple[Requirement, ...],
        provided_extras: frozenset[str] = frozenset(),
        requires_python: str | None = None,
        source_url: str | None = None,
        source_hashes: dict[str, str] | None = None,
        source_kind: str | None = None,
        source_vcs: str | None = None,
        from_cache: bool = False,
        yanked_reason: str | None = None,
        wheel_layout: object | None = None,
    ) -> None:
        self.name = name

        self.version = version

        self.path = os.fspath(path)

        self.dependencies = dependencies

        self.provided_extras = provided_extras

        self.requires_python = requires_python

        self.source_url = source_url

        self.source_hashes = source_hashes

        self.source_kind = source_kind

        self.source_vcs = source_vcs

        self.from_cache = from_cache

        self.yanked_reason = yanked_reason

        self._wheel_layout = wheel_layout

    @property
    def wheel_layout(self) -> object | None:
        """The layout, computing a :class:`LazyWheelLayout` on first access."""

        layout = self._wheel_layout

        if isinstance(layout, LazyWheelLayout):
            layout = layout.resolve()

            self._wheel_layout = layout

        return layout

    @wheel_layout.setter
    def wheel_layout(self, value: object | None) -> None:
        self._wheel_layout = value

    @property
    def stored_wheel_layout(self) -> object | None:
        """The layout as stored -- possibly still a :class:`LazyWheelLayout`
        -- for a caller rebuilding the candidate without reading the wheel."""

        return self._wheel_layout

    @property
    def wheel_layout_if_loaded(self) -> object | None:
        """The layout only if it is already known; never reads the wheel."""

        layout = self._wheel_layout

        return None if isinstance(layout, LazyWheelLayout) else layout

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WheelCandidate) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def copy_with(self, **changes: object) -> WheelCandidate:
        values = {name: getattr(self, name) for name in self.__slots__}

        values["wheel_layout"] = values.pop("_wheel_layout")

        values.update(changes)

        return type(self)(**values)

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)


class WheelTag:
    __slots__ = (
        "_abi_lower",
        "_hash",
        "_interpreter_lower",
        "_platform_lower",
        "_platform_parts",
        "abi",
        "interpreter",
        "platform",
        "triple",
    )

    def __init__(self, interpreter: str, abi: str, platform: str) -> None:
        setter = object.__setattr__

        setter(self, "interpreter", interpreter)

        setter(self, "abi", abi)

        setter(self, "platform", platform)

        setter(self, "_interpreter_lower", interpreter.lower())

        setter(self, "_abi_lower", abi.lower())

        platform_lower = platform.lower()

        setter(self, "_platform_lower", platform_lower)

        # The tag as a plain tuple, which the hash needs anyway and every
        # serializer wants: a catalog build asks for it once per artifact.
        triple = (interpreter, abi, platform)

        setter(self, "triple", triple)

        setter(self, "_hash", hash(triple))

        if platform_lower.startswith(("macosx_", "android_")):
            parts = tuple(platform_lower.split("_", 3))

        elif platform_lower.startswith("ios_"):
            parts = tuple(platform_lower.split("_", 4))

        elif platform_lower.startswith(("manylinux", "musllinux")):
            parts = linux_platform_parts(platform_lower)

        else:
            parts = None

        setter(self, "_platform_parts", parts)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        """Refuse mutation; the hash and lowercase forms are cached."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot set {name!r}",
        )

    def __delattr__(self, name: str) -> NoReturn:
        """Refuse deletion for the same reason as :meth:`__setattr__`."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot delete {name!r}",
        )

    interpreter: str

    abi: str

    platform: str

    _interpreter_lower: str

    _abi_lower: str

    _platform_lower: str

    _platform_parts: tuple[Any, ...] | None

    _hash: int

    triple: tuple[str, str, str]

    def __str__(self) -> str:
        return f"{self.interpreter}-{self.abi}-{self.platform}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WheelTag):
            return NotImplemented

        return (
            self.interpreter == other.interpreter
            and self.abi == other.abi
            and self.platform == other.platform
        )

    def __hash__(self) -> int:
        return self._hash


class WheelFile:
    __slots__ = ("build_tag", "name", "tags", "version")

    def __init__(
        self,
        name: str,
        version: Version,
        build_tag: str | None,
        tags: tuple[WheelTag, ...],
    ) -> None:
        self.name = name

        self.version = version

        self.build_tag = build_tag

        self.tags = tags

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WheelFile) and (
            self.name,
            self.version,
            self.build_tag,
            self.tags,
        ) == (other.name, other.version, other.build_tag, other.tags)

    def __hash__(self) -> int:
        return hash((self.name, self.version, self.build_tag, self.tags))

    name: str

    version: Version

    build_tag: str | None

    tags: tuple[WheelTag, ...]


class Wheel:
    __slots__ = ("build_tag", "file_tags", "filename", "name", "version")

    def __init__(self, filename: str) -> None:
        self.filename = str(filename)

        wheel = parse_wheel_file(filename)

        if wheel is None:
            raise InvalidWheelFilename(f"Invalid wheel filename: {filename}")

        self.name = wheel.name

        self.version = str(wheel.version)

        self.build_tag = legacy_build_tag(wheel.build_tag)

        self.file_tags = frozenset(wheel.tags)


class TargetContext:
    __slots__ = ("_hash", "abis", "implementation", "platforms", "python_version")

    def __init__(
        self,
        platforms: tuple[str, ...] = (),
        implementation: str | None = None,
        python_version: str | None = None,
        abis: tuple[str, ...] = (),
    ) -> None:
        setter = object.__setattr__

        setter(self, "platforms", platforms)

        setter(self, "implementation", implementation)

        setter(self, "python_version", python_version)

        setter(self, "abis", abis)

        setter(self, "_hash", hash((platforms, implementation, python_version, abis)))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        """Refuse mutation; the hash is cached and used as a cache key."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot set {name!r}",
        )

    def __delattr__(self, name: str) -> NoReturn:
        """Refuse deletion for the same reason as :meth:`__setattr__`."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot delete {name!r}",
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TargetContext) and (
            self.platforms,
            self.implementation,
            self.python_version,
            self.abis,
        ) == (
            other.platforms,
            other.implementation,
            other.python_version,
            other.abis,
        )

    def __hash__(self) -> int:
        return self._hash

    platforms: tuple[str, ...]

    implementation: str | None

    python_version: str | None

    abis: tuple[str, ...]

    _hash: int


VERSION_COMPATIBLE = (1, 0)


WHEEL_METADATA_CACHE_SIZE = 1024

_NO_HEADERS: list[str] = []


class CandidateMetadata:
    __slots__ = (
        "dependencies",
        "name",
        "provided_extras",
        "requires_python",
        "version",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        dependencies: tuple[Requirement, ...],
        provided_extras: frozenset[str],
        requires_python: str | None,
    ) -> None:
        self.name = name

        self.version = version

        self.dependencies = dependencies

        self.provided_extras = provided_extras

        self.requires_python = requires_python

    name: str

    version: Version

    dependencies: tuple[Requirement, ...]

    provided_extras: frozenset[str]

    requires_python: str | None


# Compatibility for existing internal callers while ``CandidateMetadata`` is
# the canonical shared name used by discovery and wheel parsing.
WheelResolutionMetadata = CandidateMetadata


wheel_metadata_cache: dict[tuple[str, int, int], CandidateMetadata] = register_table({})

wheel_dependency_cache: dict[
    tuple[tuple[str, int, int], frozenset[str]],
    tuple[Requirement, ...],
] = register_table({})

no_layout_candidate_cache: dict[
    tuple[tuple[str, int, int], frozenset[str]],
    tuple[str, Version, tuple[Requirement, ...], frozenset[str], str | None],
] = register_table({})


def parse_wheel_file(path: str) -> WheelFile | None:
    name = os.fspath(path)
    if "/" in name or "\\" in name or ":" in name:
        name = os.path.basename(name)
    return _parse_wheel_filename(name)


def parse_wheel_file_once(path: str) -> WheelFile | None:
    """``parse_wheel_file`` for a name parsed once, past the memo.

    Compiling an index page parses each of its wheel names exactly once,
    and a cold airflow lock lists 211,000 of them, every one distinct: the
    memo only ever missed, evicted and stored, half again the cost of the
    parse itself.
    """
    name = os.fspath(path)
    if "/" in name or "\\" in name or ":" in name:
        name = os.path.basename(name)
    return _parse_wheel_filename.__wrapped__(name)


_BUILD_TAG_RE = re.compile(r"^(\d+)(.*)$", re.ASCII)


def _is_escaped_name(field: str) -> bool:
    """Whether ``field`` could have come out of the PEP 427 escaping rule.

    That rule replaces every run of non-word characters with a single
    underscore, so a doubled underscore or a character outside
    ``[\\w\\d._]`` means the name this decodes to is a guess.
    """
    if "__" in field:
        return False
    return field.isalnum() or field.replace(".", "").replace("_", "").isalnum()


# Sized for the largest index pages: a page's wheels are parsed once to
# build the catalog record and again to rank them, and grpcio alone lists
# ten thousand.  A memo the second pass overflows re-parses every one.
@memoized(32768)
def _parse_wheel_filename(name: str) -> WheelFile | None:
    if not name.endswith(".whl"):
        return None

    stem = name[:-4]

    parts = stem.split("-")

    if len(parts) == 5:
        distribution, version, python_tags, abi_tags, platform_tags = parts

        build_tag = None

    elif len(parts) == 6:
        distribution, version, build_tag, python_tags, abi_tags, platform_tags = parts

    else:
        return None

    if not _is_escaped_name(distribution):
        return None

    if build_tag is not None and not ("0" <= build_tag[:1] <= "9"):
        return None

    try:
        parsed_version = Version(version)

    except InvalidVersion:
        return None

    tags = parsed_wheel_tags(python_tags, abi_tags, platform_tags)

    if not tags:
        return None

    return WheelFile(
        name=canonicalize_name(distribution),
        version=parsed_version,
        build_tag=build_tag,
        tags=tags,
    )


@memoized(8192)
def wheel_tag(interpreter: str, abi: str, platform: str) -> WheelTag:
    """The one :class:`WheelTag` for a triple.

    A catalog holds the same handful of tags over and over -- every pure
    Python wheel in it is ``py3-none-any`` -- and rebuilding one is three
    ``lower()`` calls, a platform split and a hash. Reconstructing a wheel
    from its cached identity built 145,000 of them for a graph the size of
    Airflow's, where a few hundred distinct tags exist. The class refuses
    mutation and compares by value, so one instance stands for all of them.
    """
    return WheelTag(interpreter, abi, platform)


@memoized(1024)
def parsed_wheel_tags(
    python_tags: str,
    abi_tags: str,
    platform_tags: str,
) -> tuple[WheelTag, ...]:
    return tuple(
        wheel_tag(interpreter, abi, platform)
        for interpreter in python_tags.split(".")
        for abi in abi_tags.split(".")
        for platform in platform_tags.split(".")
    )


def parse_wheel_filename(path: str) -> tuple[str, str] | None:
    wheel = parse_wheel_file(path)

    if wheel is None:
        return None

    return wheel.name, str(wheel.version)


class _HashCachedTags(tuple):
    """A tuple of WheelTags that memoizes its own hash.

    supported_wheel_tags() results are used as half of wheel_tag_rank()'s
    lru_cache key, and a plain tuple recomputes its hash on every lookup --
    a Python-level WheelTag.__hash__ dispatch per element, times the
    couple-dozen supported tags, per link evaluated. Since the @cache on
    supported_wheel_tags hands out one long-lived instance per target,
    caching the hash on the instance pays those element hashes once per
    process instead of once per link.
    """

    _cached_hash: int

    def __hash__(self) -> int:
        try:
            return self._cached_hash

        except AttributeError:
            value = tuple.__hash__(self)

            self._cached_hash = value

            return value


def supported_wheel_tags(target: TargetContext | None = None) -> tuple[WheelTag, ...]:
    """The tags a wheel may carry to install into the target interpreter.

    packaging's ``sys_tags``, for the Python kpip installs for -- which need
    not be the one running it -- or for ``target`` where it names a version,
    implementation, platform or ABI.
    """
    return _supported_wheel_tags(
        target, interpreter_facts.target_interpreter(installing=False)
    )


@memoized(1024)
def _supported_wheel_tags(
    target: TargetContext | None, interpreter: Any
) -> tuple[WheelTag, ...]:
    version = interpreter.version[:2]
    implementation = INTERPRETER_SHORT_NAMES.get(
        interpreter.implementation, interpreter.implementation
    )
    platform_tags: tuple[str, ...] = ()
    abi_tags: tuple[str, ...] = ()

    if target is not None:
        if target.python_version:
            digits = target.python_version.replace(".", "")
            # "3" names a major version alone; "38" and "3.8" a minor one.
            version = (int(digits[0]),) + ((int(digits[1:]),) if digits[1:] else ())
        implementation = target.implementation or implementation
        platform_tags = tuple(target.platforms)
        abi_tags = tuple(target.abis)

    version_digits = "".join(str(part) for part in version)

    impl_tag = f"{implementation}{version_digits}"

    if not abi_tags:
        abis = interpreter_abis(interpreter, implementation, version)
    elif implementation == "cp":
        # As packaging's cpython_tags: the ABIs given, then the stable ABI
        # their threading takes, then none.
        own = tuple(abi for abi in abi_tags if abi not in ("abi3", "abi3t", "none"))
        stable = ()
        if version >= (3, 2):
            flags = re.match(r"cp\d+(.*)", own[0]) if own else None
            threaded = flags is not None and "t" in flags.group(1)
            stable = ("abi3t",) if threaded else ("abi3",)
        abis = (*own, *stable, "none")
    else:
        abis = (*(abi for abi in abi_tags if abi != "none"), "none")

    platforms = platform_tags or current_platform_tags()

    # packaging's order, best first: the interpreter's own ABIs, the stable
    # ABI and none for each platform; then pure-Python tags for each
    # platform; then the platform-independent ones. Older versions --
    # cp312-abi3, py38-none -- match through interpreter_matches.
    python_tags = tuple(dict.fromkeys((f"py{version_digits}", f"py{version[0]}")))
    tags = [
        WheelTag(impl_tag, abi, platform)
        for abi in (*(abi for abi in abis if abi != "none"), "none")
        for platform in platforms
    ]
    tags += [
        WheelTag(python, "none", platform)
        for python in python_tags
        for platform in platforms
    ]
    tags.append(WheelTag(impl_tag, "none", "any"))
    tags += [WheelTag(python, "none", "any") for python in python_tags]
    if implementation == "pp":
        tags.append(WheelTag("pp3", "none", "any"))
    return _HashCachedTags(tags)


INTERPRETER_SHORT_NAMES = {
    "python": "py",
    "cpython": "cp",
    "pypy": "pp",
    "ironpython": "ip",
    "jython": "jy",
}


def interpreter_abis(
    interpreter: Any, implementation: str, version: tuple[int, ...]
) -> tuple[str, ...]:
    """The ABIs packaging lists for ``interpreter`` at ``version``.

    A CPython's own ABI -- ``t`` when free-threaded, ``d`` for a debug build,
    which also loads extensions built without it -- then the stable ABI,
    ``abi3t`` for a free-threaded build and ``abi3`` otherwise, then
    ``none``. Other implementations name theirs in ``EXT_SUFFIX``.
    """
    if implementation != "cp":
        return (*_generic_abis(interpreter), "none")
    if len(version) < 2:
        # A major version alone names no ABI of its own.
        return ("none",)

    config = interpreter.config
    version_digits = f"{version[0]}{version[1]}"
    debug = ""
    with_debug = config.get("Py_DEBUG")
    if with_debug or (
        with_debug is None
        and (interpreter.debug_refcount or "_d.pyd" in interpreter.extension_suffixes)
    ):
        debug = "d"
    threading = "t" if version >= (3, 13) and config.get("Py_GIL_DISABLED") else ""
    pymalloc = ""
    if version < (3, 8):
        with_pymalloc = config.get("WITH_PYMALLOC")
        if with_pymalloc or with_pymalloc is None:
            pymalloc = "m"

    abis = [f"cp{version_digits}{threading}{debug}{pymalloc}"]
    if debug and version >= (3, 8):
        abis.append(f"cp{version_digits}{threading}")
    if version >= (3, 2):
        abis.append("abi3t" if threading else "abi3")
    abis.append("none")
    return tuple(abis)


def _generic_abis(interpreter: Any) -> tuple[str, ...]:
    """packaging's ABI from ``EXT_SUFFIX``, for an interpreter not CPython."""
    ext_suffix = interpreter.config.get("EXT_SUFFIX")
    if not isinstance(ext_suffix, str) or not ext_suffix.startswith("."):
        return ()
    parts = ext_suffix.split(".")
    if len(parts) < 3:
        return ()
    soabi = parts[1]
    if soabi.startswith("cpython"):
        abi = "cp" + soabi.split("-")[1]
    elif soabi.startswith("cp"):
        abi = soabi.split("-")[0]
    elif soabi.startswith("pypy"):
        abi = "-".join(soabi.split("-")[:2])
    elif soabi.startswith("graalpy"):
        abi = "-".join(soabi.split("-")[:3])
    elif soabi:
        abi = soabi
    else:
        return ()
    return (abi.replace(".", "_").replace("-", "_"),)


_MACOS_VERSION_PLIST = "/System/Library/CoreServices/SystemVersion.plist"


def macos_product_version() -> str | None:
    """``platform.mac_ver()[0]``, without ``plistlib`` behind it.

    ``mac_ver`` pulls one string out of a 600-byte XML plist and pays
    ``plistlib`` -- and ``xml.parsers.expat``, ``datetime``, ``struct`` and
    ``binascii`` behind that -- to do it, on every command that has to name
    the running platform. Scanning for the one key is exact for the file macOS
    actually ships and returns ``None`` for anything it does not recognize, so
    a binary plist, a relocated file or a changed layout falls back to the
    stdlib reader rather than guessing.
    """

    try:
        with open(_MACOS_VERSION_PLIST, "rb") as handle:
            text = handle.read(65536)

    except OSError:
        return None

    if not text.startswith(b"<?xml"):
        return None

    key = text.find(b"<key>ProductVersion</key>")

    if key < 0:
        return None

    start = text.find(b"<string>", key)

    if start < 0 or text.find(b"<key>", key + 1, start) >= 0:
        return None

    start += len(b"<string>")

    end = text.find(b"</string>", start)

    if end < 0:
        return None

    try:
        return text[start:end].decode("ascii")

    except UnicodeDecodeError:
        return None


def current_platform_tag() -> str:
    """The target interpreter's platform, as a wheel tag names it."""
    interpreter = interpreter_facts.target_interpreter(installing=False)
    if sys.platform == "darwin":
        release = macos_product_version()

        if release is None:
            release = platform.mac_ver()[0]

        mac_version = release.split(".")

        if len(mac_version) >= 2 and all(part.isdigit() for part in mac_version[:2]):
            major = int(mac_version[0])
            minor = 0 if major >= 11 else int(mac_version[1])
            # The target's: an x86_64 Python runs under Rosetta on arm64.
            machine = interpreter.markers["platform_machine"]
            machine = machine.replace("-", "_").replace(".", "_")
            return f"macosx_{major}_{minor}_{machine}"

    platform_tag = interpreter.platform.replace("-", "_").replace(".", "_")

    if interpreter.pointer_bits == 32:
        # sysconfig names the kernel's machine, which for a 32-bit Python on
        # a 64-bit kernel is the wrong one. packaging corrects it the same way.
        platform_tag = _LINUX_32_BIT_PLATFORMS.get(platform_tag, platform_tag)

    return platform_tag


_LINUX_32_BIT_PLATFORMS = {
    "linux_x86_64": "linux_i686",
    "linux_aarch64": "linux_armv8l",
}
"""What a 32-bit Python on a 64-bit Linux kernel runs as, after packaging's
``_linux_platforms``."""


@memoized(1)
def current_platform_tags() -> tuple[str, ...]:
    """The platforms this interpreter can install a wheel for, best first.

    On most systems this is one tag. On Linux it is two: the libc the
    interpreter is linked against, expressed as the newest manylinux or
    musllinux tag it satisfies, followed by the bare ``linux_<arch>`` that
    ``sysconfig`` reports (each twice on armv8l, which also runs armv7l
    wheels). Without the first entry no manylinux wheel is ever
    compatible, and effectively every binary package on PyPI falls back to
    building from source.

    Only the *newest* tag of the family is listed rather than every older one
    the host also satisfies: :func:`platform_matches` compares libc versions,
    so one entry stands for the whole range. That is the same reason the
    macOS entry is a single tag rather than one per deployment target.
    """
    platform_tag = current_platform_tag()

    if not platform_tag.startswith("linux_"):
        return (platform_tag,)

    arch = platform_tag[len("linux_") :]
    # armv8l runs armv7l code too, and packaging lists both, its own first.
    archs = ("armv8l", "armv7l") if arch == "armv8l" else (arch,)
    plain = tuple(f"linux_{arch}" for arch in archs)
    from kpip.core.libc import GLIBC, MUSL, detect, manylinux_arch_supported

    libc = detect()

    if libc is None:
        return plain

    kind, major, minor = libc

    # As packaging, the ELF header decides for armv7l whenever it is listed.
    if kind == GLIBC and manylinux_arch_supported(archs[-1]):
        return (*(f"manylinux_{major}_{minor}_{arch}" for arch in archs), *plain)

    if kind == MUSL:
        return (*(f"musllinux_{major}_{minor}_{arch}" for arch in archs), *plain)

    return plain


@memoized(4096)
def wheel_tag_rank(
    tags: tuple[WheelTag, ...],
    supported_tags: tuple[WheelTag, ...] | None = None,
) -> int | None:
    """How good a fit ``tags`` are here: lower is better, None if none fits.

    The position packaging's ``sys_tags`` would give the best of them. One
    supported tag stands for a range -- a glibc and every older one, a macOS
    version and every older one with each compatible architecture, an
    interpreter and every older abi3 or pure-Python minor -- and pip ranks
    the newest and most specific member of each range first: manylinux_2_28
    over manylinux_2_17, arm64 over universal2, cp311-abi3 over cp39-abi3.
    Older interpreters are not ranked with their supported tag, though:
    packaging lists cp39-abi3 after cp312-none, and py38-none-any after
    py3-none-any, so their matches rank where :func:`_rank_layout` puts them.
    """
    supported = supported_wheel_tags() if supported_tags is None else supported_tags
    layout = None
    best = None

    for index, supported_tag in enumerate(supported):
        # Every later tag ranks from its base up, and bases only grow; so a
        # match of a tag's own interpreter ends the search at the next one.
        if best is not None and layout is not None and layout[index][0] >= best:
            break
        for tag in tags:
            if tag_matches(supported_tag, tag):
                if layout is None:
                    layout = _layout_for(supported)
                base, older_base, stride, platform_index, older = layout[index]
                distance, platform = _match_reach(supported_tag, tag)
                if distance and older_base >= 0:
                    rank = (
                        older_base
                        + (min(distance, older) - 1) * stride
                        + platform_index * _PLATFORM_SCALE
                        + platform
                    )
                else:
                    rank = base + platform
                if best is None or rank < best:
                    best = rank

    return best


def _layout_for(
    supported: tuple[WheelTag, ...],
) -> tuple[tuple[int, int, int, int, int], ...]:
    """:func:`_rank_layout`, for the tuple nearly every call ranks against
    without hashing it: a resolve holds one, and it is compared by identity."""
    global _last_layout
    last = _last_layout
    if last[0] is supported:
        return last[1]
    layout = _rank_layout(supported)
    _last_layout = (supported, layout)
    return layout


_last_layout: tuple[Any, tuple[tuple[int, int, int, int, int], ...]] = (None, ())
"""The supported tuple last ranked against, and its :func:`_rank_layout`."""

_PLATFORM_SCALE = 1_000
"""Room within one supported tag's rank for how far a platform match reaches;
the largest rank stays well below the 1,000,000 callers use for "no rank"."""


def _older_range(tag: WheelTag) -> int:
    """How many older minors ``tag`` also stands for: every abi3 CPython and
    pure-Python ``py3X`` down to the first, as packaging lists them; 0 for a
    tag that matches its own interpreter alone."""
    interpreter = tag._interpreter_lower
    abi = tag._abi_lower
    if len(interpreter) < 4 or not interpreter[3:].isdigit():
        return 0
    if (abi in ("abi3", "abi3t") and interpreter.startswith("cp")) or (
        abi == "none" and interpreter.startswith("py")
    ):
        return int(interpreter[3:])
    return 0


@memoized(64)
def _rank_layout(
    supported: tuple[WheelTag, ...],
) -> tuple[tuple[int, int, int, int, int], ...]:
    """Where each supported tag's matches rank, as packaging orders them.

    For each tag ``(base, older_base, stride, platform_index, older)``: a
    match of its own interpreter ranks from ``base``; one of an older
    interpreter -- cp39-abi3 under cp312-abi3, at most ``older`` minors down
    -- from ``older_base``, a ``stride`` per minor, then by platform. packaging lists those older ones after every
    platform of the interpreter's last ABI (cp312-none-<platform>), or, for
    ``py3X-none``, after ``py3-none``, minor by minor and each over every
    platform, so that is where their room is made.
    """
    count = len(supported)
    layout: list[tuple[int, int, int, int, int]] = [(0, -1, 0, 0, 0)] * count
    groups: list[tuple[int, int]] = []
    start = 0
    while start < count:
        key = (supported[start]._interpreter_lower, supported[start]._abi_lower)
        end = start + 1
        while (
            end < count
            and (supported[end]._interpreter_lower, supported[end]._abi_lower) == key
        ):
            end += 1
        groups.append((start, end))
        start = end

    cursor = 0
    # Groups with older minors whose room is not made yet, with the
    # interpreters that may still come before it.
    pending: list[tuple[int, int, tuple[str, ...]]] = []

    def make_room(group_start: int, group_end: int) -> None:
        nonlocal cursor
        size = group_end - group_start
        stride = size * _PLATFORM_SCALE
        older = _older_range(supported[group_start])
        for offset, index in enumerate(range(group_start, group_end)):
            base = layout[index][0]
            layout[index] = (base, cursor, stride, offset, older)
        cursor += older * stride

    for group_start, group_end in groups:
        interpreter = supported[group_start]._interpreter_lower
        waiting = []
        for entry in pending:
            if interpreter in entry[2]:
                waiting.append(entry)
            else:
                make_room(entry[0], entry[1])
        pending = waiting
        for index in range(group_start, group_end):
            layout[index] = (cursor, -1, 0, 0, 0)
            cursor += _PLATFORM_SCALE
        if _older_range(supported[group_start]):
            followers = (
                (interpreter, f"py{interpreter[2]}")
                if interpreter.startswith("py")
                else (interpreter,)
            )
            pending.append((group_start, group_end, followers))

    for entry in pending:
        make_room(entry[0], entry[1])

    return tuple(layout)


_MACOS_ARCH_PREFERENCE = {
    "x86_64": ("x86_64", "intel", "fat64", "fat32", "universal2", "universal"),
    "i386": ("i386", "intel", "fat32", "fat", "universal"),
    "intel": ("intel", "fat64", "fat32", "universal"),
    "arm64": ("arm64", "universal2"),
    "aarch64": ("aarch64", "universal2"),
    "ppc": ("ppc", "fat32", "fat", "universal"),
    "ppc64": ("ppc64", "fat64", "universal"),
}
"""pip's order for macOS architectures: the native one first."""


def _minor_distance(runtime: str, wheel: str) -> int:
    """How many minors older ``wheel``'s interpreter is (``cp311`` against
    ``cp312`` is 1); 0 when they are the same or not comparable."""
    if runtime == wheel or len(runtime) < 4 or len(wheel) < 4:
        return 0
    try:
        return max(0, int(runtime[3:]) - int(wheel[3:]))
    except ValueError:
        return 0


def _macos_steps(runtime: tuple[int, int], wheel: tuple[int, int]) -> int:
    """How many macOS versions down from ``runtime`` ``wheel`` is, counting
    as pip lists them: 14.0, 13.0, 12.0, 11.0, then 10.16 down to 10.0."""

    def position(version: tuple[int, int]) -> int:
        major, minor = version
        return major - 11 + 17 if major >= 11 else minor

    return max(0, position(runtime) - position(wheel))


def _match_reach(supported: WheelTag, candidate: WheelTag) -> tuple[int, int]:
    """How far below the supported tag a matching ``candidate`` sits: how
    many minors older its interpreter is, and how far its platform reaches
    -- 0 for the tag itself, more for an older libc or macOS version, or a
    less specific architecture."""
    interpreter = _minor_distance(
        supported._interpreter_lower, candidate._interpreter_lower
    )
    platform = 0
    runtime_parts = supported._platform_parts
    wheel_parts = candidate._platform_parts
    if (
        runtime_parts is not None
        and wheel_parts is not None
        and supported._platform_lower != candidate._platform_lower
    ):
        family = runtime_parts[0]
        if family in ("manylinux", "musllinux"):
            _, runtime_major, runtime_minor, _ = runtime_parts
            _, wheel_major, wheel_minor, _ = wheel_parts
            platform = (runtime_major - wheel_major) * 100 + (
                runtime_minor - wheel_minor
            )
        elif family == "macosx" and len(runtime_parts) == 4 and len(wheel_parts) == 4:
            try:
                versions = _macos_steps(
                    (int(runtime_parts[1]), int(runtime_parts[2])),
                    (int(wheel_parts[1]), int(wheel_parts[2])),
                )
            except ValueError:
                versions = 0
            arches = _MACOS_ARCH_PREFERENCE.get(runtime_parts[3], ())
            arch = arches.index(wheel_parts[3]) if wheel_parts[3] in arches else 0
            platform = versions * 8 + arch
    return interpreter, min(max(platform, 0), _PLATFORM_SCALE - 1)


def wheel_archive_identity(
    path: str,
    archive: ZipArchiveSource | None,
    dist_info_dir: str | None,
) -> tuple[str, int, int] | None:
    path_text = os.fspath(path)

    try:
        if archive is not None and dist_info_dir is not None:
            metadata = archive.getinfo(f"{dist_info_dir}/METADATA")

            path_key = (
                path_text if os.path.isabs(path_text) else os.path.abspath(path_text)
            )

            return path_key, metadata.CRC, metadata.file_size

        stat = os.stat(path_text)

        path_key = path_text if os.path.isabs(path_text) else os.path.abspath(path_text)

        return path_key, stat.st_size, stat.st_mtime_ns

    except KeyError, OSError:
        return None


def project_wheel_dependencies(
    metadata: CandidateMetadata,
    identity: tuple[str, int, int] | None,
    extras: frozenset[str],
) -> tuple[Requirement, ...]:
    key = (identity, extras) if identity is not None else None

    dependencies = wheel_dependency_cache.get(key) if key is not None else None

    if dependencies is not None:
        return dependencies

    declared = metadata.dependencies

    if all(requirement.marker is None for requirement in declared):
        dependencies = declared
    else:
        dependencies = tuple(
            [
                requirement
                for requirement in declared
                if marker_applies(requirement.marker, extras=extras)
            ],
        )

    if key is not None:
        bounded_put(
            wheel_dependency_cache, key, dependencies, WHEEL_METADATA_CACHE_SIZE
        )

    return dependencies


def wheel_candidate(
    path: str,
    extras: Collection[str] | None = None,
    *,
    archive: ZipArchiveSource | None = None,
    filename_info: tuple[str, str | Version] | None = None,
    dist_info_dir: str | None = None,
    wheel_metadata_text: str | None = None,
    include_layout: bool = True,
    metadata_cache: MetadataCache | None = None,
) -> WheelCandidate:
    wheel_path = os.fspath(path)

    parsed = filename_info or parse_wheel_filename(wheel_path)

    if parsed is None:
        raise InvalidWheelFilename(f"Invalid wheel filename: {wheel_path}")

    name, version = parsed

    identity = wheel_archive_identity(wheel_path, archive, dist_info_dir)

    metadata = wheel_metadata_cache.get(identity) if identity is not None else None

    if metadata is None:
        if archive is not None and dist_info_dir is not None:
            headers = (
                metadata_cache.get_reference(identity)
                if metadata_cache is not None and identity is not None
                else None
            )

            if headers is None:
                headers = read_core_metadata_headers(archive, wheel_path, dist_info_dir)

                if metadata_cache is not None and identity is not None:
                    metadata_cache.put(identity, headers)

            header_get = headers.get

            def get_header(name: str) -> str | None:
                values = header_get(name)

                return values[0] if values else None

            def get_all_headers(name: str) -> list[str]:
                return header_get(name, _NO_HEADERS)

        else:
            message = (
                read_metadata_message_internal(
                    archive,
                    wheel_path,
                    expected_name=name,
                    dist_info_dir=dist_info_dir,
                )
                if archive is not None
                else read_metadata_message(wheel_path)
            )

            def get_header(name: str) -> str | None:
                return message.get(name)

            def get_all_headers(name: str) -> list[str]:
                return message.get_all(name, [])

        metadata_name = get_header("name") or name

        metadata_version = get_header("version") or str(version)

        parsed_metadata_version = (
            version
            if isinstance(version, Version) and metadata_version == str(version)
            else Version(metadata_version)
        )

        metadata = CandidateMetadata(
            name=metadata_name,
            version=parsed_metadata_version,
            dependencies=tuple(
                map(parse_requirement, get_all_headers("requires-dist")),
            ),
            provided_extras=frozenset(
                stripped
                for value in get_all_headers("provides-extra")
                if (stripped := value.strip())
            ),
            requires_python=get_header("requires-python"),
        )

        if identity is not None:
            bounded_put(
                wheel_metadata_cache, identity, metadata, WHEEL_METADATA_CACHE_SIZE
            )

    requested_extras = frozenset(extras or ())

    dependencies = project_wheel_dependencies(metadata, identity, requested_extras)

    wheel_layout = None

    if include_layout and archive is not None and dist_info_dir is not None:
        if wheel_metadata_text is None:
            wheel_metadata_text = archive.read(f"{dist_info_dir}/WHEEL").decode("utf-8")

        wheel_layout = (
            dist_info_dir,
            tuple(
                (
                    name,
                    info.compress_type,
                    info.CRC,
                    info.compress_size,
                    info.file_size,
                    info.header_offset,
                    info.external_attr,
                )
                for name, info in archive.NameToInfo.items()
            ),
            any(
                line.casefold().strip() == "root-is-purelib: true"
                for line in wheel_metadata_text.splitlines()
            ),
        )

    return WheelCandidate(
        name=metadata.name,
        version=metadata.version,
        path=os.fspath(wheel_path),
        dependencies=dependencies,
        provided_extras=metadata.provided_extras,
        requires_python=metadata.requires_python,
        wheel_layout=wheel_layout,
    )


def read_core_metadata_headers(
    archive: ZipArchiveSource,
    path: str,
    dist_info_dir: str,
) -> dict[str, list[str]]:
    """Read core metadata headers needed during candidate resolution."""

    metadata_path = f"{dist_info_dir}/METADATA"

    try:
        return parse_metadata_member(archive.read, metadata_path)

    except KeyError as exc:
        raise InstallationError(f"Wheel has no METADATA: {path}") from exc

    except UnicodeDecodeError as exc:
        raise InstallationError(
            f"Error decoding metadata for {path}: {metadata_path}",
        ) from exc


def read_metadata_message(path: str):
    try:
        with open(path, "rb", buffering=0) as file:
            archive = WheelArchive(file, metadata_only=True)
            return read_metadata_message_internal(archive, path)
    except WheelhouseUnavailable:
        pass

    with zipfile.ZipFile(path) as archive:
        return read_metadata_message_internal(archive, path)


def _metadata_filename_name(path: str) -> str | None:
    """Return the distribution prefix without validating irrelevant fields."""
    filename = os.path.basename(os.fspath(path))
    if not filename.endswith(".whl"):
        return None
    distribution, separator, _ = filename.partition("-")
    return distribution if separator and _is_escaped_name(distribution) else None


def read_metadata_message_internal(
    archive: MetadataArchiveSource,
    path: str,
    *,
    expected_name: str | None = None,
    dist_info_dir: str | None = None,
) -> LightMetadata:
    metadata_names = (
        [f"{dist_info_dir}/METADATA"]
        if dist_info_dir is not None
        else metadata_paths(archive.namelist())
    )

    if not metadata_names:
        raise InstallationError(f"Wheel has no METADATA: {path}")

    if expected_name is None:
        expected_name = _metadata_filename_name(path)

        if expected_name is None:
            parsed = parse_wheel_filename(path)

            expected_name = parsed[0] if parsed is not None else None

    if expected_name is not None:
        expected = canonicalize_name(expected_name).replace("-", "_")

        expected_casefold = expected.casefold()

        matching = [
            name
            for name in metadata_names
            if name.count("/") == 1
            and name.rsplit("/", 1)[0]
            .split(".", 1)[0]
            .casefold()
            .startswith(expected_casefold)
        ]

        if matching:
            metadata_names = matching

    try:
        contents = archive.read(metadata_names[0]).decode("utf-8")

    except KeyError as exc:
        raise InstallationError(f"Wheel has no METADATA: {path}") from exc

    except UnicodeDecodeError as exc:
        raise InstallationError(
            f"Error decoding metadata for {path}: {metadata_names[0]}",
        ) from exc

    return parse_metadata_text(contents)


_NAME_SEPARATORS_RE = re.compile(r"[-_.]+")


@memoized(4096)
def _dist_info_match_key(name: str) -> str:
    """Normalized project name for matching against a dist-info directory.

    Every release of a package examined during a resolve calls
    ``wheel_dist_info_dir`` with the same project name, so this is the same
    regex substitution repeated once per candidate wheel with an identical
    result each time.
    """
    return re.sub(r"[-_.]+", "", canonicalize_name(name)).casefold()


def wheel_dist_info_dir(source: ZipArchiveSource, name: str) -> str:
    dist_info_dir: str | None = None

    for filename in source.NameToInfo:
        if not filename.endswith(".dist-info/WHEEL") or filename.count("/") != 1:
            continue

        match = filename.split("/", 1)[0]

        if dist_info_dir is not None:
            raise UnsupportedWheel("multiple .dist-info directories found")

        dist_info_dir = match

    if dist_info_dir is None:
        raise UnsupportedWheel(".dist-info directory not found")

    expected = _dist_info_match_key(name)

    actual = _NAME_SEPARATORS_RE.sub(
        "", dist_info_dir.removesuffix(".dist-info")
    ).casefold()

    if not actual.startswith(expected):
        raise UnsupportedWheel(
            f".dist-info directory {dist_info_dir!r} does not start with {name!r}",
        )

    return dist_info_dir


def read_wheel_archive_member(source: ZipArchiveSource, path: str) -> bytes:
    try:
        return source.read(path)

    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise UnsupportedWheel(f"could not read {path!r} file: {exc!r}") from exc


def read_wheel_format_metadata(source: ZipArchiveSource, dist_info_dir: str) -> Message:
    wheel_path = f"{dist_info_dir}/WHEEL"

    raw = read_wheel_archive_member(source, wheel_path)

    try:
        text = raw.decode()

    except UnicodeDecodeError as exc:
        raise UnsupportedWheel(f"error decoding {wheel_path!r}: {exc!r}") from exc

    return Parser().parsestr(text)


def wheel_version(metadata: Message) -> tuple[int, ...]:
    value = metadata.get("Wheel-Version")

    if value is None:
        raise UnsupportedWheel("WHEEL is missing Wheel-Version")

    version = value.strip()

    try:
        return tuple(map(int, version.split(".")))

    except ValueError as exc:
        raise UnsupportedWheel(f"invalid Wheel-Version: {version!r}") from exc


def wheel_version_from_text(text: str) -> tuple[int, ...]:
    """Read Wheel-Version without constructing an email Message."""

    value: str | None = None

    for line in text.splitlines():
        if not line:
            break

        name, separator, header_value = line.partition(":")

        if separator and name.casefold() == "wheel-version":
            value = header_value.strip()

            break

    if value is None:
        raise UnsupportedWheel("WHEEL is missing Wheel-Version")

    try:
        return tuple(map(int, value.split(".")))

    except ValueError as exc:
        raise UnsupportedWheel(f"invalid Wheel-Version: {value!r}") from exc


def root_is_purelib_from_text(text: str) -> bool:
    """Whether the wheel root unpacks into purelib, from the WHEEL text.

    ``Root-Is-Purelib: false`` means the wheel carries compiled extensions and
    belongs in platlib. In a virtualenv the two schemes are the same directory,
    which is why getting this wrong is invisible in most testing; on a system
    layout that splits them (``/usr/lib`` against ``/usr/lib64``) it puts the
    package in the wrong one. The field is required, and anything other than a
    case-insensitive ``true`` reads as false, as the format specifies.
    """
    for line in text.splitlines():
        if not line:
            break
        name, separator, value = line.partition(":")
        if separator and name.casefold() == "root-is-purelib":
            return value.strip().casefold() == "true"
    raise UnsupportedWheel("WHEEL is missing Root-Is-Purelib")


def check_compatibility(version: tuple[int, ...], name: str) -> None:
    if version[0] > VERSION_COMPATIBLE[0]:
        raise UnsupportedWheel(
            "{}'s Wheel-Version ({}) is not compatible with this version of kpip".format(
                name,
                ".".join(map(str, version)),
            ),
        )

    if version > VERSION_COMPATIBLE:
        logging.getLogger(__name__).warning(
            "Installing from a newer Wheel-Version (%s)",
            ".".join(map(str, version)),
        )


def validate_wheel_with_metadata(
    source: ZipArchiveSource, name: str
) -> tuple[str, str]:
    """Validate a wheel and return its metadata directory and WHEEL text."""

    try:
        info_dir = wheel_dist_info_dir(source, name)

        wheel_path = f"{info_dir}/WHEEL"

        raw = read_wheel_archive_member(source, wheel_path)

        try:
            text = raw.decode()

        except UnicodeDecodeError as exc:
            raise UnsupportedWheel(f"error decoding {wheel_path!r}: {exc!r}") from exc

        version = wheel_version_from_text(text)

    except UnsupportedWheel as exc:
        raise UnsupportedWheel(f"{name} has an invalid wheel, {exc}") from exc

    check_compatibility(version, name)

    return info_dir, text


def validate_wheel(source: ZipArchiveSource, name: str) -> str:
    """Validate a wheel without materializing its WHEEL metadata message."""

    return validate_wheel_with_metadata(source, name)[0]


def wheel_candidate_from_path(
    path: str,
    extras: Collection[str] | None = None,
    *,
    include_layout: bool = True,
) -> WheelCandidate:
    """Build a WheelCandidate for a wheel not already backed by an open archive.

    ``wheel_candidate(path)`` alone reopens the archive with no dist-info
    directory in hand, which sends it through the slow, email-based metadata
    fallback. Validating first and handing the freshly opened archive and
    dist-info directory into ``wheel_candidate`` directly -- the same thing
    candidate materialization does during resolution -- takes the fast path
    instead. Use this instead of a bare ``wheel_candidate(path)`` call
    whenever there's no already-open archive to reuse (a freshly built
    wheel, a batch of paths to validate, an install with no pre-resolved
    candidate).

    ``include_layout`` defaults to True to preserve the resolver-cache
    benefit (a later real install can reuse the captured zip layout instead
    of re-reading the central directory). Callers that build a candidate and
    then immediately extract it themselves -- and so never reuse that cached
    layout -- should pass ``include_layout=False``: a non-None layout on a
    freshly (re)opened archive makes ``open_wheel_archive`` fall back to the
    slower ``zipfile.ZipFile`` reader instead of the fast raw one.

    When ``include_layout=False``, this also checks/populates
    ``no_layout_candidate_cache`` keyed by a cheap ``os.stat()``-based
    identity *before* opening the archive at all. wheel_candidate()'s own
    metadata cache can't help here on its own: it's checked only after this
    function has already paid for opening and fully parsing the archive
    (zipfile.ZipFile eagerly parses every member into a ZipInfo on
    construction) just to hand it in. That parsing is real work independent
    of validate_wheel_with_metadata's own dist-info checks, which the
    caller's later archive open (via open_wheel_archive + validate_wheel,
    once wheel_layout stays None) redundantly repeats anyway -- so a stat
    identity match here means we can skip both without losing any structural
    validation that wasn't already going to happen again downstream.
    """

    requested_extras = frozenset(extras or ())

    if not include_layout:
        identity = wheel_archive_identity(path, None, None)

        if identity is not None:
            cached = no_layout_candidate_cache.get((identity, requested_extras))

            if cached is not None:
                name, version, dependencies, provided_extras, requires_python = cached

                return WheelCandidate(
                    name=name,
                    version=version,
                    path=os.fspath(path),
                    dependencies=dependencies,
                    provided_extras=provided_extras,
                    requires_python=requires_python,
                )

    with (
        open(path, "rb", buffering=32768) as stream,
        zipfile.ZipFile(stream) as archive,
    ):
        dist_info_dir, wheel_metadata_text = validate_wheel_with_metadata(
            archive,
            os.path.basename(path)[:-4].split("-", 1)[0],
        )
        candidate = wheel_candidate(
            path,
            extras,
            archive=archive,
            dist_info_dir=dist_info_dir,
            wheel_metadata_text=wheel_metadata_text,
            include_layout=include_layout,
        )

    if not include_layout:
        identity = wheel_archive_identity(path, None, None)

        if identity is not None:
            bounded_put(
                no_layout_candidate_cache,
                (identity, requested_extras),
                (
                    candidate.name,
                    candidate.version,
                    candidate.dependencies,
                    candidate.provided_extras,
                    candidate.requires_python,
                ),
                WHEEL_METADATA_CACHE_SIZE,
            )

    return candidate


def parse_wheel(wheel_zip: zipfile.ZipFile, name: str) -> tuple[str, Message]:
    """Validate a wheel archive and return its metadata directory and WHEEL data."""

    try:
        info_dir = wheel_dist_info_dir(wheel_zip, name)

        metadata = read_wheel_format_metadata(wheel_zip, info_dir)

        version = wheel_version(metadata)

    except UnsupportedWheel as exc:
        raise UnsupportedWheel(f"{name} has an invalid wheel, {exc}") from exc

    check_compatibility(version, name)

    return info_dir, metadata


def legacy_build_tag(value: str | None) -> tuple[int, str] | tuple[()]:
    """``(number, suffix)`` for a build tag, or ``()`` when there is none.

    Filenames reach this only after :func:`_parse_wheel_filename` has checked
    that the tag starts with a digit, so the number is always present.
    """
    if value is None:
        return ()

    match = _BUILD_TAG_RE.match(value)

    if match is None:
        return ()

    return (int(match.group(1)), match.group(2))


def tag_matches(supported: WheelTag, candidate: WheelTag) -> bool:
    supported_interpreter = supported._interpreter_lower

    candidate_interpreter = candidate._interpreter_lower

    supported_abi = supported._abi_lower

    candidate_abi = candidate._abi_lower

    return (
        interpreter_matches(
            supported_interpreter,
            candidate_interpreter,
            candidate_abi,
        )
        and supported_abi == candidate_abi
        and platform_matches(
            supported._platform_lower,
            candidate._platform_lower,
            supported._platform_parts,
            candidate._platform_parts,
        )
    )


def interpreter_matches(runtime: str, wheel: str, abi: str) -> bool:
    if runtime == wheel:
        return True

    if abi in ("abi3", "abi3t") and runtime.startswith("cp") and wheel.startswith("cp"):
        try:
            return int(wheel[2:]) <= int(runtime[2:])

        except ValueError:
            return False

    # A pure wheel for an older minor of the same major -- py38-none-any on
    # 3.12 -- is compatible, as packaging's compatible_tags lists it.
    if (
        abi == "none"
        and runtime.startswith("py")
        and wheel.startswith("py")
        and len(runtime) > 3
        and len(wheel) > 3
        and runtime[2] == wheel[2]
    ):
        try:
            return int(wheel[3:]) <= int(runtime[3:])

        except ValueError:
            return False

    return False


def platform_matches(
    runtime: str,
    wheel: str,
    runtime_parts: tuple[Any, ...] | None,
    wheel_parts: tuple[Any, ...] | None,
) -> bool:
    if runtime == wheel:
        return True

    if runtime == "any" or wheel == "any":
        return False

    if runtime_parts is None or wheel_parts is None:
        return False

    family = runtime_parts[0]

    if family != wheel_parts[0]:
        return False

    if family == "macosx":
        return _macos_platform_matches_parts(runtime_parts, wheel_parts)

    if family == "ios":
        return _ios_platform_matches_parts(runtime_parts, wheel_parts)

    if family == "android":
        return _android_platform_matches_parts(runtime_parts, wheel_parts)

    return _linux_platform_matches_parts(runtime_parts, wheel_parts)


def _linux_platform_matches_parts(
    runtime_parts: tuple[Any, ...],
    wheel_parts: tuple[Any, ...],
) -> bool:
    """A libc-tagged wheel runs here if it names the same libc and architecture
    and asks for no newer a version than the one installed.

    Comparing versions rather than enumerating every tag the host satisfies is
    what keeps the supported-tag list at two entries on Linux instead of the
    forty-odd the reference implementation generates. PEP 600 lets us compare
    across major versions directly: a newer glibc satisfies every older one.
    """
    _, runtime_major, runtime_minor, runtime_arch = runtime_parts
    _, wheel_major, wheel_minor, wheel_arch = wheel_parts

    if runtime_arch != wheel_arch:
        return False

    return (wheel_major, wheel_minor) <= (runtime_major, runtime_minor)


def _macos_platform_matches_parts(
    runtime_parts: tuple[str, ...],
    wheel_parts: tuple[str, ...],
) -> bool:
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False

    _, runtime_major, runtime_minor, runtime_arch = runtime_parts

    _, wheel_major, wheel_minor, wheel_arch = wheel_parts

    if (int(wheel_major), int(wheel_minor)) > (int(runtime_major), int(runtime_minor)):
        return False

    compatible_arches = MACOS_COMPATIBLE_ARCHES.get(runtime_arch)

    return (
        wheel_arch == runtime_arch
        if compatible_arches is None
        else wheel_arch in compatible_arches
    )


def _ios_platform_matches_parts(
    runtime_parts: tuple[str, ...],
    wheel_parts: tuple[str, ...],
) -> bool:
    if len(runtime_parts) != 5 or len(wheel_parts) != 5:
        return False

    _, runtime_major, runtime_minor, runtime_arch, runtime_env = runtime_parts

    _, wheel_major, wheel_minor, wheel_arch, wheel_env = wheel_parts

    if runtime_arch != wheel_arch or runtime_env != wheel_env:
        return False

    return (int(wheel_major), int(wheel_minor)) <= (
        int(runtime_major),
        int(runtime_minor),
    )


def _android_platform_matches_parts(
    runtime_parts: tuple[str, ...],
    wheel_parts: tuple[str, ...],
) -> bool:
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False

    _, runtime_api, runtime_arch_a, runtime_arch_b = runtime_parts

    _, wheel_api, wheel_arch_a, wheel_arch_b = wheel_parts

    if (runtime_arch_a, runtime_arch_b) != (wheel_arch_a, wheel_arch_b):
        return False

    return int(wheel_api) <= int(runtime_api)
