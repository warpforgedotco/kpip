"""Narrow command fast paths and the argv shapes they recognize.

Each module here handles one deliberately small command shape: ``install``
(``fast_install``). A fast path is a
conservative recognizer, never separate command semantics; it returns ``None``
when an argument, target state, source shape, or feature falls outside its
subset, and normal command dispatch stays available after every decline.

This module must stay import-light. It is loaded on every startup, so the
heavier CLI dependencies are imported only once their shape matches.
"""

from __future__ import annotations


import os
import sys

from kpip.core.names import canonicalize_name


REMOTE_EXACT_OPTIONS = ("--ignore-installed", "--no-compile", "--target")
LOCAL_WHEELHOUSE_OPTIONS = (
    "--no-index",
    "--ignore-installed",
    "--no-compile",
    "--target",
)
LOCAL_UPGRADE_OPTIONS = ("--no-index", "--upgrade", "--no-compile", "--target")
LOCAL_INSTALL_OPTIONS = ("--no-index", "--no-compile", "--target")

SATISFIED_FLAGS = frozenset(
    (
        "--no-index",
        "--pre",
        "--dry-run",
        "--no-deps",
        "--no-compile",
        "--no-cache-dir",
        "-q",
        "--quiet",
        "--disable-kpip-version-check",
        "--no-input",
        "--no-warn-conflicts",
        "--no-warn-script-location",
    )
)
SATISFIED_VALUE_OPTIONS = (
    "-f",
    "--find-links",
    "-i",
    "--index-url",
    "--extra-index-url",
    "--cache-dir",
    "--trusted-host",
)
_PLAIN_OPERATORS = ("===", "~=", "!=", "==", ">=", "<=", ">", "<")


def option_value(args: list[str], index: int) -> str | None:
    """Return a following option value, or ``None`` for an invalid option."""
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    return None if value.startswith("-") else value


def consume_option(
    args: list[str],
    index: int,
    names: tuple[str, ...],
) -> tuple[str, str, int] | None:
    """Consume a value option in either separated or ``--name=value`` form."""
    token = args[index]
    for name in names:
        if token == name:
            value = option_value(args, index)
            return None if value is None else (name, value, index + 2)
        prefix = name + "="
        if token.startswith(prefix):
            value = token[len(prefix) :]
            return None if not value else (name, value, index + 1)
    return None


def read_requirements(path: str) -> list[str] | None:
    """Read simple requirement files without importing the full parser."""
    try:
        with open(path, encoding="utf-8") as requirement_file:
            return [
                line.strip()
                for line in requirement_file.read().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        return None


def extend_requirements(
    target: list[str],
    path: str,
    *,
    reject_pylock: bool = False,
) -> bool:
    """Read a requirement file and append its entries to ``target``."""
    if (
        reject_pylock
        and os.path.basename(path).startswith("pylock")
        and path.endswith(".toml")
    ):
        return False
    value = read_requirements(path)
    if value is None:
        return False
    target.extend(value)
    return True


def _read_headers(path: str) -> dict[str, str] | None:
    """First value of every header in an RFC 822-style metadata file, the
    way ``parse_metadata_headers`` reads it; None when unreadable or empty."""
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read()
    except OSError:
        return None
    if not text:
        return None
    headers: dict[str, str] = {}
    last = None
    for line in text.splitlines():
        if not line:
            break
        if line[0] in " \t":
            if last is not None:
                headers[last] += "\n" + line
            continue
        key, separator, value = line.partition(":")
        if not separator:
            break
        last = key.strip().lower()
        headers.setdefault(last, value.strip())
    return headers


def _info_headers(path: str) -> dict[str, str] | None:
    """Headers of one dist-info/egg-info entry, read the way the
    installed-state scan reads it: METADATA, then PKG-INFO, then the entry
    itself for a flat egg-info file. None for an entry without a name and
    version, which the scan skips."""
    for filename in ("METADATA", "PKG-INFO", ""):
        headers = _read_headers(os.path.join(path, filename) if filename else path)
        if headers is None:
            continue
        if headers.get("name") and headers.get("version"):
            return headers
        return None
    return None


class InstalledEntry:
    """One ``*.dist-info``/``*.egg-info`` entry a directory listing found."""

    __slots__ = ("headers", "info_path", "location", "name", "root")

    def __init__(self, root: str, child: str, headers: dict[str, str]) -> None:
        self.root = root
        self.info_path = os.path.join(root, child)
        self.headers = headers
        self.name = canonicalize_name(headers["name"])
        self.location = os.path.normpath(root) if root else "."


def scan_installed(
    roots: list[str], names: set[str] | None = None
) -> list[InstalledEntry] | None:
    """Every entry the installed-state scan would report under ``roots``, in
    its order: roots in sequence, directory listing order within a root.

    None means the answer is not this cheap and the full scan must decide:
    a root that is a file (a zip or egg on sys.path), a ``*.egg`` directory,
    a root spelled with ``..`` (whose location pathlib renders differently),
    or -- with ``names`` -- two entries for one requested name in one root.
    With ``names``, only those names are read; entries are matched on the
    directory name (PEP 427) and confirmed against the Name header.
    """
    found: list[InstalledEntry] = []
    for root in roots:
        root = os.fspath(root)
        if ".." in root.split(os.sep):
            return None
        try:
            children = os.listdir(root or ".")
        except OSError:
            if os.path.isfile(root):
                return None
            continue
        if os.path.basename(root).lower().endswith(".egg"):
            return None
        seen: set[str] = set()
        for child in children:
            low = child.lower()
            if low.endswith(".dist-info"):
                stem = child[:-10]
            elif low.endswith(".egg-info"):
                stem = child[:-9]
            else:
                continue
            if names is not None:
                name = canonicalize_name(stem.partition("-")[0])
                if name not in names:
                    continue
                if name in seen:
                    return None
                seen.add(name)
            headers = _info_headers(os.path.join(root, child))
            if headers is None:
                continue
            entry = InstalledEntry(root, child, headers)
            if names is not None and entry.name not in names:
                continue
            found.append(entry)
    return found


def _other_distribution_finders() -> bool:
    from importlib.machinery import PathFinder

    return any(
        finder is not PathFinder and hasattr(finder, "find_distributions")
        for finder in sys.meta_path
    )


def _has_all(options: list[str], names: tuple[str, ...]) -> bool:
    return all(name in options for name in names)


def suppresses_logging(args: list[str], *, log_file: str | None) -> bool:
    """Whether ``args`` names a quiet fast-path shape that must not log."""
    if not args or log_file is not None or "--quiet" not in args:
        return False

    if args[0] != "install":
        return False

    options = args[1:]
    return _has_all(options, LOCAL_INSTALL_OPTIONS) and (
        "--ignore-installed" in options or "--upgrade" in options
    )


def run_before_startup(args: list[str]) -> tuple[int | None, bool]:
    """Try the fast paths that run before CLI initialization."""
    if not args:
        return None, False

    command = args[0]
    options = args[1:]

    if command != "install":
        return None, False

    if (
        "--quiet" in options
        and "--no-index" not in options
        and _has_all(options, REMOTE_EXACT_OPTIONS)
    ):
        from kpip.cli import fast_install

        return fast_install.run_cached_remote(options), False

    if (
        "--quiet" in options
        and "--ignore-installed" not in options
        and _has_all(options, LOCAL_UPGRADE_OPTIONS)
    ):
        from kpip.cli import fast_install

        return fast_install.run_local_fallback(options), True

    if _has_all(options, LOCAL_WHEELHOUSE_OPTIONS):
        from kpip.cli import fast_install

        status = fast_install.run(options)
        if status is not None:
            return status, True
        return fast_install.run_local_fallback(options), True

    return run_satisfied_install(options), False


class SatisfiedOptions:
    __slots__ = ("find_links", "quiet", "requirements")

    def __init__(self) -> None:
        self.requirements: list[tuple[str, str, str, tuple[int, ...] | None]] = []
        self.find_links: list[str] = []
        self.quiet = False


def release_key(value: str) -> tuple[int, ...] | None:
    """A release-only version as a comparable tuple, or None for any other.

    Only dotted integers qualify, so PEP 440 ordering is plain tuple
    ordering once trailing zeros are dropped; anything with a pre-, post-,
    dev- or local segment, or an epoch, stays with the full parser.
    """
    parts = value.split(".")
    if not all(part.isdigit() and part.isascii() for part in parts):
        return None
    result = [int(part) for part in parts]
    while len(result) > 1 and result[-1] == 0:
        result.pop()
    return tuple(result)


def parse_plain_requirement(
    token: str,
) -> tuple[str, str, str, tuple[int, ...] | None] | None:
    """``(raw, canonical name, operator, version key)`` for ``name`` or
    ``name<op>release``; None for any other requirement shape (extras,
    markers, URLs, paths, several specifiers, wildcards, ``~=``/``!=``)."""
    raw = token.strip()
    if not raw or raw[0] in "-.":
        return None
    name, operator, version = raw, "", ""
    for candidate in _PLAIN_OPERATORS:
        if candidate in raw:
            if candidate in ("===", "~=", "!="):
                return None
            name, _, version = raw.partition(candidate)
            operator = candidate
            break
    name = name.strip()
    if (
        not name
        or not name.isascii()
        or not name[0].isalnum()
        or not name[-1].isalnum()
        or not name.replace("-", "").replace("_", "").replace(".", "").isalnum()
    ):
        return None
    key = None
    if operator:
        key = release_key(version.strip())
        if key is None:
            return None
    return raw, canonicalize_name(name), operator, key


def parse_satisfied_arguments(args: list[str]) -> SatisfiedOptions | None:
    options = SatisfiedOptions()
    index = 0
    while index < len(args):
        consumed = consume_option(args, index, SATISFIED_VALUE_OPTIONS)
        if consumed is not None:
            name, value, index = consumed
            if name in ("-f", "--find-links"):
                options.find_links.append(value)
            continue
        token = args[index]
        if token in SATISFIED_FLAGS:
            options.quiet = options.quiet or token in ("-q", "--quiet")
        elif token.startswith("-"):
            return None
        else:
            requirement = parse_plain_requirement(token)
            if requirement is None:
                return None
            options.requirements.append(requirement)
        index += 1
    if not options.requirements:
        return None
    return options


def installed_versions(names: set[str]) -> dict[str, str] | None:
    """The installed version of each requested canonical name, found the way
    the installed-state scan finds it: sys.path in order, the first
    distribution of a name wins; None when the full scan must decide."""
    if _other_distribution_finders():
        return None
    entries = scan_installed([os.fspath(entry) for entry in sys.path], names)
    if entries is None:
        return None
    found: dict[str, str] = {}
    for entry in entries:
        found.setdefault(entry.name, entry.headers["version"])
    return found


def run_satisfied_install(args: list[str]) -> int | None:
    """Report plain requirements that are all already installed, as the
    normal path would, without loading it."""
    options = parse_satisfied_arguments(args)
    if options is None:
        return None
    if os.environ.get("KPIP_TARGET_PREFIX") or os.environ.get("KPIP_RESOLVER_DEBUG"):
        return None
    versions = installed_versions({item[1] for item in options.requirements})
    if versions is None:
        return None
    for _, name, operator, wanted in options.requirements:
        installed = versions.get(name)
        if installed is None:
            return None
        key = release_key(installed)
        if key is None:
            return None
        if not operator:
            continue
        if wanted is None or not (
            (operator == "==" and key == wanted)
            or (operator == ">=" and key >= wanted)
            or (operator == "<=" and key <= wanted)
            or (operator == ">" and key > wanted)
            or (operator == "<" and key < wanted)
        ):
            return None
    from kpip.cli.config import load_source_config

    if load_source_config("install").find_links:
        return None
    if not options.quiet:
        if options.find_links:
            print(f"Looking in links: {', '.join(options.find_links)}")
        for raw, _, _, _ in options.requirements:
            print(f"Requirement already satisfied: {raw}")
    return 0


def run_install_after_startup(args: list[str]) -> int | None:
    """Try the local install fast path once logging has been configured."""
    if not args or args[0] != "install":
        return None

    options = args[1:]

    if not _has_all(options, LOCAL_WHEELHOUSE_OPTIONS):
        return None

    from kpip.cli import fast_install

    return fast_install.run(options)
