"""Narrow command fast paths and the argv shapes they recognize.

Each module here handles one deliberately small command shape: ``install``
(``fast_install``), ``lock``, and ``list``. A fast path is a
conservative recognizer, never separate command semantics; it returns ``None``
when an argument, target state, source shape, or feature falls outside its
subset, and normal command dispatch stays available after every decline.

This module must stay import-light. It is loaded on every startup, so the
heavier CLI dependencies are imported only once their shape matches.
"""

from __future__ import annotations

from kpip.core.utils import versioned_bucket

import marshal
import os
import sys

from kpip.cli.lock_format import render_wheel_lock, write_lock_output
from kpip.core.appdirs import command_cache_dir
from kpip.core.code_identity import code_identity
from kpip.core.names import canonicalize_name

FAST_LOCK_PLAN_BUCKET = versioned_bucket("fast-lock-plan", 2)
"""Directory under the cache directory holding rendered lock plans."""

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


FORMATS = frozenset(("columns", "json", "freeze"))
LIST_VALUE_OPTIONS = ("--path", "--format", "--exclude")
STDLIB_NAMES = frozenset(("python", "wsgiref", "argparse"))
"""``core.light_metadata.stdlib_pkgs``, which ``list`` always skips."""


class ListOptions:
    __slots__ = ("excludes", "format", "paths", "verbose")

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.format = "columns"
        self.verbose = 0
        self.excludes: set[str] = set()


def parse_list_arguments(args: list[str]) -> ListOptions | None:
    options = ListOptions()
    index = 0
    while index < len(args):
        consumed = consume_option(args, index, LIST_VALUE_OPTIONS)
        if consumed is not None:
            name, value, index = consumed
            if name == "--path":
                options.paths.append(value)
            elif name == "--format":
                if value not in FORMATS:
                    return None
                options.format = value
            else:
                options.excludes.add(canonicalize_name(value))
            continue
        token = args[index]
        if token in ("-v", "--verbose"):
            options.verbose += 1
        elif token.startswith("-") and len(token) > 1 and set(token[1:]) == {"v"}:
            options.verbose += len(token) - 1
        else:
            return None
        index += 1
    if "pip" in options.excludes:
        from kpip.core.kpip_version import KPIP_DISTRIBUTION_NAMES

        options.excludes.update(
            canonicalize_name(name) for name in KPIP_DISTRIBUTION_NAMES
        )
    return options


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


def canonical_release(value: str) -> bool:
    """Whether ``Version(value)`` renders back as ``value``: dotted integers
    without leading zeros, the one spelling that needs no normalization."""
    parts = value.split(".")
    return all(
        part.isdigit() and part.isascii() and str(int(part)) == part for part in parts
    )


def json_string(value: str) -> str:
    """``json.dumps(value)``: ASCII output with the same escapes."""
    parts = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            parts.append('\\"')
        elif char == "\\":
            parts.append("\\\\")
        elif char == "\n":
            parts.append("\\n")
        elif char == "\r":
            parts.append("\\r")
        elif char == "\t":
            parts.append("\\t")
        elif char == "\b":
            parts.append("\\b")
        elif char == "\f":
            parts.append("\\f")
        elif code < 0x20 or code > 0x7E:
            if code > 0xFFFF:
                code -= 0x10000
                parts.append(
                    f"\\u{0xD800 | (code >> 10):04x}\\u{0xDC00 | (code & 0x3FF):04x}"
                )
            else:
                parts.append(f"\\u{code:04x}")
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def _editable_location(entry: InstalledEntry) -> str | None:
    """``editable_project_location`` for an entry with a direct_url.json."""
    from kpip.core.direct_url import DirectUrl
    from kpip.core.urls import url_to_path

    try:
        with open(
            os.path.join(entry.info_path, "direct_url.json"), encoding="utf-8"
        ) as file:
            direct_url = DirectUrl.from_json(file.read())
    except (OSError, ValueError):
        return None
    if direct_url.is_local_editable():
        return url_to_path(direct_url.url)
    return None


def _installer(entry: InstalledEntry) -> str:
    try:
        with open(os.path.join(entry.info_path, "INSTALLER"), encoding="utf-8") as file:
            return next(
                (line.strip() for line in file.read().splitlines() if line.strip()), ""
            )
    except OSError:
        return ""


def run_list(args: list[str]) -> int | None:
    """``kpip list`` over the interpreter's own search path or ``--path``
    directories, rendered as the normal path renders it."""
    options = parse_list_arguments(args)
    if options is None:
        return None
    if options.paths:
        roots = options.paths
    else:
        if os.environ.get("KPIP_TARGET_PREFIX") or _other_distribution_finders():
            return None
        roots = [os.fspath(entry) for entry in sys.path]
    entries = scan_installed(roots)
    if entries is None:
        return None
    entries = [
        entry
        for entry in entries
        if entry.name not in STDLIB_NAMES and entry.name not in options.excludes
    ]
    entries.sort(key=lambda entry: entry.name)

    if any(entry.info_path.lower().endswith(".egg-info") for entry in entries):
        return None
    editable: dict[int, str | None] = {}
    for entry in entries:
        if os.path.exists(os.path.join(entry.info_path, "direct_url.json")):
            editable[id(entry)] = _editable_location(entry)

    verbose = options.verbose > 0
    if options.format in ("json", "freeze"):
        if not all(canonical_release(entry.headers["version"]) for entry in entries):
            return None

    if options.format == "json":
        items = []
        for entry in entries:
            fields = [
                f'"name": {json_string(entry.headers["name"])}',
                f'"version": {json_string(entry.headers["version"])}',
            ]
            if verbose:
                fields.append(f'"location": {json_string(entry.location)}')
                fields.append(f'"installer": {json_string(_installer(entry))}')
            location = editable.get(id(entry))
            if location:
                fields.append(f'"editable_project_location": {json_string(location)}')
            items.append("{" + ", ".join(fields) + "}")
        print("[" + ", ".join(items) + "]")
        return 0

    if options.format == "freeze":
        for entry in entries:
            line = f"{entry.headers['name']}=={entry.headers['version']}"
            if verbose:
                line = f"{line} ({entry.location})"
            print(line)
        return 0

    build_tags = []
    for entry in entries:
        wheel = _read_headers(os.path.join(entry.info_path, "WHEEL"))
        build_tags.append(wheel.get("build") if wheel else None)
    header = ["Package", "Version"]
    if any(build_tags):
        header.append("Build")
    has_editables = any(editable.values())
    if has_editables:
        header.append("Editable project location")
    if verbose:
        header.extend(("Location", "Installer"))
    rows = [header]
    for index, entry in enumerate(entries):
        row = [entry.headers["name"], entry.headers["version"]]
        if any(build_tags):
            row.append(build_tags[index] or "")
        if has_editables:
            row.append(editable.get(id(entry)) or "")
        if verbose:
            row.extend((entry.location, _installer(entry)))
        rows.append(row)
    widths = [
        max(len(str(row[i])) if i < len(row) else 0 for row in rows)
        for i in range(len(rows[0]))
    ]
    print(
        "\n".join(
            " ".join(
                str(value).ljust(widths[i]) for i, value in enumerate(row)
            ).rstrip()
            for row in rows
        ),
    )
    return 0


FREEZE_FLAGS = frozenset(("--all", "--exclude-editable"))
FREEZE_VALUE_OPTIONS = ("--exclude", "--path")


class FreezeOptions:
    __slots__ = ("all", "exclude_editable", "excludes", "paths")

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.excludes: set[str] = set()
        self.all = False
        self.exclude_editable = False


def parse_freeze_arguments(args: list[str]) -> FreezeOptions | None:
    options = FreezeOptions()
    index = 0
    while index < len(args):
        consumed = consume_option(args, index, FREEZE_VALUE_OPTIONS)
        if consumed is not None:
            name, value, index = consumed
            if name == "--path":
                options.paths.append(os.path.normpath(value))
            else:
                options.excludes.add(canonicalize_name(value))
            continue
        token = args[index]
        if token == "--all":
            options.all = True
        elif token == "--exclude-editable":
            options.exclude_editable = True
        else:
            return None
        index += 1
    if "kpip" in options.excludes:
        from kpip.core.kpip_version import KPIP_DISTRIBUTION_NAMES

        options.excludes.update(
            canonicalize_name(name) for name in KPIP_DISTRIBUTION_NAMES
        )
    return options


def _valid_project_name(name: str) -> bool:
    """``cli.freeze.VALID_NAME``: alphanumerics, dots, underscores and
    hyphens, starting and ending with an alphanumeric."""
    return (
        name.isascii()
        and name[:1].isalnum()
        and name[-1:].isalnum()
        and name.replace(".", "").replace("_", "").replace("-", "").isalnum()
    )


def run_freeze(args: list[str]) -> int | None:
    """``kpip freeze`` over the interpreter's search path or ``--path``
    directories, one ``name==version`` (or ``name @ url``) line per
    distribution, as the normal path prints it.

    The normal path reads installed metadata through core.light_metadata:
    per directory, entries in sorted name order, the first distribution of
    a name across the directories wins. Anything it would answer with a
    warning, a VCS command or the full version parser is declined: an
    invalid distribution name, an editable install (unless excluded), an
    egg-info entry, a version spelled other than as the parser renders it.
    """
    options = parse_freeze_arguments(args)
    if options is None:
        return None
    if options.paths:
        roots = options.paths
    else:
        if os.environ.get("KPIP_TARGET_PREFIX") or _other_distribution_finders():
            return None
        roots = [os.fspath(entry) for entry in sys.path]

    entries = scan_installed(roots)
    if entries is None:
        return None
    ordered: list[InstalledEntry] = []
    for root in roots:
        here = sorted(
            (entry for entry in entries if entry.root == root),
            key=lambda entry: os.path.basename(entry.info_path),
        )
        ordered.extend(here)
    chosen: dict[str, InstalledEntry] = {}
    for entry in ordered:
        info = entry.info_path
        if info.lower().endswith(".egg-info"):
            return None
        if not os.path.isdir(info):
            return None
        metadata = os.path.join(info, "METADATA")
        if not os.path.isfile(metadata) or _read_headers(metadata) != entry.headers:
            return None
        chosen.setdefault(entry.name, entry)

    skip = set(STDLIB_NAMES)
    if not options.all:
        from kpip.core.kpip_version import KPIP_DISTRIBUTION_NAMES

        skip.update(canonicalize_name(name) for name in KPIP_DISTRIBUTION_NAMES)
        if sys.version_info < (3, 12):
            skip.add("setuptools")

    lines: list[tuple[str, str]] = []
    for entry in chosen.values():
        name = entry.headers["name"]
        version = entry.headers["version"]
        if not _valid_project_name(name):
            return None
        if entry.name in options.excludes or entry.name in skip:
            continue
        requirement = None
        if os.path.exists(os.path.join(entry.info_path, "direct_url.json")):
            from kpip.core.direct_url import DirectUrl

            try:
                with open(
                    os.path.join(entry.info_path, "direct_url.json"), encoding="utf-8"
                ) as file:
                    direct_url = DirectUrl.from_json(file.read())
            except (OSError, ValueError):
                direct_url = None
            if direct_url is not None:
                if direct_url.is_local_editable():
                    if options.exclude_editable:
                        continue
                    return None
                requirement = direct_url.as_pep440_direct_reference(name)
        if requirement is None:
            if not canonical_release(version):
                return None
            requirement = f"{name}=={version}"
        lines.append((name.lower(), requirement))

    for _, requirement in sorted(lines, key=lambda item: item[0]):
        print(requirement)
    return 0


class LockOptions:
    __slots__ = (
        "cache_dir",
        "constraint_files",
        "find_links",
        "no_binary",
        "no_build_isolation",
        "no_cache_dir",
        "no_index",
        "output",
        "python_version",
        "requirement_args",
        "requirement_files",
        "requirements",
    )

    def __init__(
        self,
        requirements: list[str],
        find_links: list[str],
        no_index: bool,
        output: str,
        cache_dir: str | None = None,
        no_cache_dir: bool = False,
    ) -> None:
        # Every requirement, with requirement files expanded in place.
        self.requirements = requirements
        self.find_links = find_links
        self.no_index = no_index
        self.output = output
        self.cache_dir = cache_dir
        self.no_cache_dir = no_cache_dir
        # The command line as given, which is what a replayed lock keys on.
        self.requirement_args: list[str] = []
        self.requirement_files: list[str] = []
        self.constraint_files: list[str] = []
        self.no_binary: list[str] = []
        self.no_build_isolation = False
        self.python_version: str | None = None


PlanCacheKey = tuple[object, ...]


def parse_lock_arguments(args: list[str]) -> LockOptions | None:
    options = LockOptions([], [], False, "pylock.toml")

    index = 0
    while index < len(args):
        token = args[index]
        if token == "--no-index":
            options.no_index = True
            index += 1
            continue
        if token == "--no-cache-dir":
            options.no_cache_dir = True
            index += 1
            continue
        if token == "--no-build-isolation":
            options.no_build_isolation = True
            index += 1
            continue
        if token == "--quiet":
            index += 1
            continue

        option = consume_option(
            args,
            index,
            (
                "-f",
                "--find-links",
                "-r",
                "--requirement",
                "-c",
                "--constraint",
                "--output",
                "--cache-dir",
                "--no-binary",
                "--python-version",
            ),
        )
        if option is not None:
            name, value, index = option
            if name == "--cache-dir":
                options.cache_dir = value
            elif name in ("-f", "--find-links"):
                options.find_links.append(value)
            elif name in ("-r", "--requirement"):
                options.requirement_files.append(value)
                if not extend_requirements(
                    options.requirements,
                    value,
                    reject_pylock=True,
                ):
                    return None
            elif name in ("-c", "--constraint"):
                options.constraint_files.append(value)
            elif name == "--no-binary":
                options.no_binary.append(value)
            elif name == "--python-version":
                options.python_version = value
            else:
                options.output = value
            continue

        if token.startswith("-"):
            return None
        else:
            options.requirement_args.append(token)
            options.requirements.append(token)
        index += 1

    if options.no_index and any(
        ";" in requirement for requirement in options.requirements
    ):
        # A marker means the answer depends on which interpreter the lock is
        # for, which is a question the wheelhouse path has no machinery to
        # ask. Handing it back costs one scan of the lines already in memory
        # and gets the full command, which evaluates markers against the
        # lock's target. A replayed index lock keys on that target instead.
        return None

    return options


def cache_digest(value: bytes) -> str:
    digest = 14695981039346656037
    for byte in value:
        digest = (digest ^ byte) * 1099511628211 & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def plan_cache_key(options: LockOptions) -> PlanCacheKey | None:
    signatures: list[tuple[str, str, int, int]] = []
    for value in options.find_links:
        root = os.path.abspath(value)
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.name.endswith(".whl") or not entry.is_file():
                        continue
                    stat = entry.stat()
                    signatures.append(
                        (root, entry.name, stat.st_mtime_ns, stat.st_size),
                    )
        except NotADirectoryError:
            if not value.endswith(".whl"):
                continue
            try:
                stat = os.stat(value)
            except OSError:
                return None
            signatures.append(
                (os.path.abspath(value), "", stat.st_mtime_ns, stat.st_size),
            )
            continue
        except OSError:
            return None

    return (
        # The rendered lock outlives this kpip in the default cache.
        code_identity(),
        sys.version_info[:3],
        sys.platform,
        tuple(options.requirements),
        tuple(options.find_links),
        tuple(sorted(signatures)),
    )


def cache_path(options: LockOptions) -> str | None:
    root = command_cache_dir(options.cache_dir, options.no_cache_dir)
    if not root:
        return None
    key = (
        sys.version_info[:3],
        sys.platform,
        tuple(options.requirements),
        tuple(options.find_links),
    )
    try:
        serialized = marshal.dumps(key)
        digest = cache_digest(serialized)
    except (OSError, TypeError, ValueError):
        return None
    return os.path.join(root, FAST_LOCK_PLAN_BUCKET, f"{digest}.cache")


def load_plan_cache(path: str | None, key: bytes | None) -> str | None:
    if path is None or key is None:
        return None
    try:
        with open(path, "rb") as file:
            size = int.from_bytes(file.read(8), "big")
            if file.read(size) != key:
                return None
            return file.read().decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def save_plan_cache(path: str | None, key: bytes | None, rendered: str) -> None:
    if path is None or key is None:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "wb") as file:
            file.write(len(key).to_bytes(8, "big"))
            file.write(key)
            file.write(rendered.encode("utf-8"))
        os.replace(temporary, path)
    except OSError:
        pass


def replay_lock(options: LockOptions) -> int | None:
    """Write the lock recorded last time, if nothing it depended on changed."""
    root = command_cache_dir(options.cache_dir, options.no_cache_dir)
    if root is None or options.find_links:
        return None

    from kpip.cli import lock_replay
    from kpip.index.config import DEFAULT_INDEX_URL

    key = lock_replay.replay_key(
        requirements=options.requirement_args,
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        index_urls=(DEFAULT_INDEX_URL,),
        no_binary=options.no_binary,
        no_build_isolation=options.no_build_isolation,
        python_version=options.python_version,
    )
    if key is None:
        return None

    record = lock_replay.load_record(root, key)
    if record is None:
        return None

    http_cache = lock_replay.open_http_cache(root)
    if lock_replay.page_state(http_cache, record.pages) != lock_replay.FRESH:
        return None

    write_lock_output(options.output, record.rendered)
    return 0


def replay_lock_arguments(args: list[str]) -> int | None:
    """Replay an index lock, and try nothing else."""
    options = parse_lock_arguments(args)
    if options is None or options.no_index:
        return None
    return replay_lock(options)


def run_lock(args: list[str]) -> int | None:
    options = parse_lock_arguments(args)
    if options is None:
        return None

    if not options.no_index:
        return replay_lock(options)

    if (
        not options.requirements
        or options.constraint_files
        or options.no_binary
        or options.no_build_isolation
        or options.python_version
    ):
        # The wheelhouse path answers none of these.
        return None

    cache_file = cache_path(options)
    plan_key = plan_cache_key(options)
    if plan_key is None:
        return None

    serialized_plan_key = marshal.dumps(plan_key)
    cached_output = load_plan_cache(cache_file, serialized_plan_key)
    if cached_output is not None:
        write_lock_output(options.output, cached_output)
        return 0

    from kpip.resolution.api import ResolutionEngine
    from kpip.core.hashes import file_hashes

    plan = ResolutionEngine.resolve_wheelhouse(options.find_links, options.requirements)
    if plan is None:
        return None

    packages: list[tuple[str, str, str, str, str]] = []
    for candidate in plan.candidates:
        source = candidate.source_url
        if source is None:
            return None
        digest = (candidate.source_hashes or {}).get("sha256")
        if digest is None:
            try:
                digest = file_hashes(candidate.path)["sha256"]
            except OSError:
                return None
        packages.append(
            (
                candidate.name,
                str(candidate.version),
                os.path.basename(candidate.path),
                source,
                digest,
            ),
        )

    rendered = render_wheel_lock(packages)
    save_plan_cache(cache_file, serialized_plan_key, rendered)
    write_lock_output(options.output, rendered)
    return 0


def _has_all(options: list[str], names: tuple[str, ...]) -> bool:
    return all(name in options for name in names)


def suppresses_logging(args: list[str], *, log_file: str | None) -> bool:
    """Whether ``args`` names a quiet fast-path shape that must not log."""
    if not args or log_file is not None or "--quiet" not in args:
        return False

    if args[0] == "lock":
        return True

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

    if command == "lock":
        if "--quiet" not in options:
            # A replayed lock neither prints nor logs, so it does not wait for
            # logging to be configured. The wheelhouse path resolves, and
            # whatever it reports should look like the full command's.
            return replay_lock_arguments(options), False
        return run_lock(options), False

    if command == "list":
        return run_list(options), False

    if command == "freeze":
        return run_freeze(options), False

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


def run_lock_after_startup(args: list[str]) -> int | None:
    """Try the lock fast path for invocations the pre-startup gate declined."""
    if not args or args[0] != "lock":
        return None

    return run_lock(args[1:])
