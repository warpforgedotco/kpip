"""What kpip knows about a Python it installs for.

pip works for the interpreter it runs under. kpip may not: ``--python``
names another, and the compiled kpip has no Python of its own. So the facts
come from the interpreter itself: read live in this process when it is the
one running kpip (:class:`OwnInterpreter`), otherwise by running the probe
with it (:class:`ProbedInterpreter`), whose answer is kept on disk until the
interpreter or its search path changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import sysconfig
import tempfile
import time
from typing import Any

from kpip._internal.exceptions import CommandError
from kpip._internal.interpreters.probe import OLDEST_TARGET, probe_digest, probe_source
from kpip._internal.utils.compiled import is_compiled
from kpip._vendor.packaging import markers as _markers
from kpip._vendor.packaging.tags import Tag, parse_tag

# packaging's own: this process's marker environment. The module's
# default_environment answers for the target interpreter (see __init__).
_process_environment = _markers.default_environment

logger = logging.getLogger(__name__)


class Interpreter:
    """A Python kpip installs for, and the facts kpip decides from."""

    executable: str
    prefix: str
    base_prefix: str
    exec_prefix: str
    version: tuple[int, int, int]
    implementation: str
    cache_tag: str | None
    magic: bytes
    stdlib: str
    platlibdir: str
    platform: str
    pointer_bits: int
    user_site: str
    user_base: str
    user_site_enabled: bool
    has_venv: bool

    @property
    def is_own(self) -> bool:
        """Whether this is the Python running kpip."""
        return False

    @property
    def is_bundled(self) -> bool:
        """Whether this is the CPython inside the compiled kpip, which no one
        installs into: it stands in only for resolving, with no other found."""
        return False

    @property
    def major_minor(self) -> str:
        return "%d.%d" % self.version[:2]

    @property
    def in_virtualenv(self) -> bool:
        """Whether this is a PEP 405 virtual environment's interpreter."""
        return self.prefix != self.base_prefix

    @property
    def path(self) -> list[str]:
        """Where it imports from: its ``sys.path``."""
        raise NotImplementedError

    @property
    def markers(self) -> dict[str, str]:
        """Its environment markers (PEP 508)."""
        raise NotImplementedError

    @property
    def tags(self) -> list[Tag]:
        """The wheel tags it supports, most preferred first."""
        raise NotImplementedError

    @property
    def platforms(self) -> list[str]:
        """Its platform tags, most preferred first."""
        raise NotImplementedError

    @property
    def interpreter_name(self) -> str:
        """Its short implementation name in wheel tags, such as ``cp``."""
        raise NotImplementedError

    @property
    def interpreter_version(self) -> str:
        """Its version in wheel tags, such as ``312``."""
        raise NotImplementedError

    @property
    def abis(self) -> list[str]:
        """The ABI tags it supports, most preferred first."""
        raise NotImplementedError

    @property
    def system_sites(self) -> list[str]:
        """``site.getsitepackages()``: its system site directories."""
        raise NotImplementedError

    def scheme_names(self) -> tuple[str, ...]:
        raise NotImplementedError

    def preferred_scheme(self, key: str) -> str:
        """``sysconfig.get_preferred_scheme(key)``, as it would answer."""
        raise NotImplementedError

    def get_paths(
        self, scheme: str, variables: dict[str, str] | None = None
    ) -> dict[str, str]:
        """``sysconfig.get_paths(scheme, variables)``, as it would answer."""
        raise NotImplementedError

    def get_config_var(self, name: str) -> Any:
        """``sysconfig.get_config_var(name)``, as it would answer."""
        raise NotImplementedError


class OwnInterpreter(Interpreter):
    """The Python running kpip, read live: nothing is copied, so what this
    process changes (and tests patch) is what kpip sees."""

    @property
    def is_own(self) -> bool:
        return True

    @property
    def is_bundled(self) -> bool:
        return is_compiled()

    executable = property(lambda self: sys.executable)  # type: ignore[assignment]
    prefix = property(lambda self: sys.prefix)  # type: ignore[assignment]
    base_prefix = property(lambda self: getattr(sys, "base_prefix", sys.prefix))  # type: ignore[assignment]
    exec_prefix = property(lambda self: sys.exec_prefix)  # type: ignore[assignment]
    version = property(lambda self: tuple(sys.version_info[:3]))  # type: ignore[assignment]
    implementation = property(lambda self: sys.implementation.name)  # type: ignore[assignment]
    cache_tag = property(lambda self: sys.implementation.cache_tag)  # type: ignore[assignment]
    platlibdir = property(lambda self: getattr(sys, "platlibdir", "lib"))  # type: ignore[assignment]
    platform = property(lambda self: sysconfig.get_platform())  # type: ignore[assignment]
    pointer_bits = property(lambda self: sys.maxsize.bit_length() + 1)  # type: ignore[assignment]

    @property  # type: ignore[override]
    def magic(self) -> bytes:
        import importlib.util

        return importlib.util.MAGIC_NUMBER

    @property  # type: ignore[override]
    def stdlib(self) -> str:
        return sysconfig.get_path("stdlib")

    @property  # type: ignore[override]
    def user_site(self) -> str:
        import site

        return site.getusersitepackages()

    @property  # type: ignore[override]
    def user_base(self) -> str:
        import site

        return site.getuserbase()

    @property  # type: ignore[override]
    def user_site_enabled(self) -> bool:
        import site

        return bool(site.ENABLE_USER_SITE)

    @property  # type: ignore[override]
    def has_venv(self) -> bool:
        import importlib.util

        return importlib.util.find_spec("venv") is not None

    @property
    def path(self) -> list[str]:
        return list(sys.path)

    @property
    def markers(self) -> dict[str, str]:
        return dict(_process_environment())

    @property
    def tags(self) -> list[Tag]:
        from kpip._vendor.packaging.tags import sys_tags

        return list(sys_tags())

    @property
    def platforms(self) -> list[str]:
        from kpip._vendor.packaging.tags import platform_tags

        return list(platform_tags())

    @property
    def interpreter_name(self) -> str:
        from kpip._vendor.packaging.tags import interpreter_name

        return interpreter_name()

    @property
    def interpreter_version(self) -> str:
        from kpip._vendor.packaging.tags import interpreter_version

        return interpreter_version()

    @property
    def abis(self) -> list[str]:
        from kpip._vendor.packaging import tags

        if tags.interpreter_name() == "cp":
            return tags._cpython_abis(sys.version_info[:2])
        return tags._generic_abi()

    @property
    def system_sites(self) -> list[str]:
        import site

        return site.getsitepackages()

    def scheme_names(self) -> tuple[str, ...]:
        return sysconfig.get_scheme_names()

    def preferred_scheme(self, key: str) -> str:
        return sysconfig.get_preferred_scheme(key)  # type: ignore[arg-type]

    def get_paths(
        self, scheme: str, variables: dict[str, str] | None = None
    ) -> dict[str, str]:
        return sysconfig.get_paths(scheme, vars=variables)

    def get_config_var(self, name: str) -> Any:
        return sysconfig.get_config_var(name)


class ProbedInterpreter(Interpreter):
    """Another Python, as the probe found it."""

    def __init__(self, facts: dict[str, Any]) -> None:
        if "tags" not in facts:
            version = ".".join(str(part) for part in facts["version"])
            oldest = ".".join(str(part) for part in OLDEST_TARGET)
            raise CommandError(
                f"The Python interpreter {facts['executable']} is Python {version}; "
                f"kpip installs for Python {oldest} or newer."
            )
        self.facts = facts
        self.executable = facts["executable"]
        self.prefix = facts["prefix"]
        self.base_prefix = facts["base_prefix"]
        self.exec_prefix = facts["exec_prefix"]
        self.version = tuple(facts["version"])  # type: ignore[assignment]
        self.implementation = facts["implementation"]
        self.cache_tag = facts["cache_tag"]
        self.magic = bytes.fromhex(facts["magic"])
        self.stdlib = facts["stdlib"]
        self.platlibdir = facts["platlibdir"]
        self.platform = facts["platform"]
        self.pointer_bits = facts["pointer_bits"]
        self.user_site = facts["user_site"]
        self.user_base = facts["user_base"]
        self.user_site_enabled = facts["user_site_enabled"]
        self.has_venv = facts["venv"]
        self._tags = [tag for text in facts["tags"] for tag in parse_tag(text)]

    @property
    def path(self) -> list[str]:
        return list(self.facts["path"])

    @property
    def markers(self) -> dict[str, str]:
        return dict(self.facts["markers"])

    @property
    def tags(self) -> list[Tag]:
        return list(self._tags)

    @property
    def platforms(self) -> list[str]:
        return list(self.facts["platforms"])

    @property
    def interpreter_name(self) -> str:
        return self.facts["interpreter_name"]

    @property
    def interpreter_version(self) -> str:
        return self.facts["interpreter_version"]

    @property
    def abis(self) -> list[str]:
        return list(self.facts["abis"])

    @property
    def system_sites(self) -> list[str]:
        return list(self.facts["site_packages"])

    def scheme_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.facts["schemes"]))

    def preferred_scheme(self, key: str) -> str:
        try:
            return self.facts["preferred"][key]
        except KeyError:
            raise KeyError(key) from None

    def get_config_var(self, name: str) -> Any:
        return self.facts["config"].get(name)

    def get_paths(
        self, scheme: str, variables: dict[str, str] | None = None
    ) -> dict[str, str]:
        # sysconfig's _expand_vars, with this interpreter's config.
        values: dict[str, Any] = dict(variables or {})
        for name, value in self.facts["config"].items():
            values.setdefault(name, value)
        if os.name == "nt":
            values = values | {"platlibdir": "lib"}
        result = {}
        for key, template in self.facts["schemes"][scheme].items():
            if os.name in ("posix", "nt"):
                template = os.path.expanduser(template)
            try:
                text = template.format(**values)
            except KeyError:
                try:
                    text = template.format(**os.environ)
                except KeyError as missing:
                    raise AttributeError(str(missing)) from None
            result[key] = os.path.normpath(text)
        return result


# Where answers are kept between runs; None keeps none. Set from the
# command's options (``--cache-dir``, ``--no-cache-dir``).
_cache_dir: str | None = None

_probed: dict[str, ProbedInterpreter] = {}

# What changes the facts without changing the interpreter's files: where it
# looks for modules and for its user site.
_PROBE_ENVIRONMENT = (
    "PYTHONHOME",
    "PYTHONNOUSERSITE",
    "PYTHONPATH",
    "PYTHONPLATLIBDIR",
    "PYTHONSAFEPATH",
    "PYTHONUSERBASE",
)

_CACHE_ENTRIES = 256
"""Answers kept: each build environment probed leaves one."""


def set_cache_dir(cache_dir: str | None) -> None:
    global _cache_dir
    _cache_dir = os.path.join(cache_dir, "interpreters-v1") if cache_dir else None


def probe(executable: str) -> ProbedInterpreter:
    """The Python at ``executable``, read by running the probe with it.

    About 40 ms, unless an earlier run left its answer in the cache. The
    probe goes on its standard input rather than with ``-c``: a ``.bat``
    shim, as pyenv-win puts on ``PATH``, runs through ``cmd.exe``, which
    ends an argument at its first newline. Kept by the path as given, not
    resolved: a virtual environment's ``python`` links to its base's.
    """
    key = os.path.abspath(executable)
    found = _probed.get(key)
    if found is None:
        facts, store = _cached_facts(key)
        if facts is None:
            started = time.time_ns()
            facts = _run_probe(executable)
            if store is not None:
                _store_facts(store, facts, key, started)
        found = _probed[key] = ProbedInterpreter(facts)
    return found


def _run_probe(executable: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [executable, "-"],
            input=probe_source(),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CommandError(
            f"Could not run the Python interpreter {executable}: {exc}"
        ) from exc
    if result.returncode != 0:
        said = result.stderr.strip().splitlines()
        raise CommandError(
            f"Could not run the Python interpreter {executable} "
            f"(exit status {result.returncode})"
            + (f": {said[-1]}" if said else "")
            + ". Name a working one with --python."
        )
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise CommandError(
            f"The Python interpreter {executable} did not answer as one; "
            "name a working one with --python."
        ) from None


def _stamp(path: str) -> list[int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def _cached_facts(
    path: str,
) -> tuple[dict[str, Any] | None, tuple[str, list[Any]] | None]:
    """An earlier answer for the interpreter at ``path``, if nothing it
    depends on changed; and where to keep this one's, or None.

    The facts hold while the interpreter's file, its environment's
    ``pyvenv.cfg``, the variables that move its search path, and the probe
    are what they were; and while every directory on its ``sys.path``, and
    its user site, has the modification time it had: adding or removing a
    ``.pth`` file changes the directory's.
    """
    if _cache_dir is None:
        return None, None
    real = os.path.realpath(path)
    executable_stamp = _stamp(real)
    if executable_stamp is None:
        return None, None
    key = [
        path,
        real,
        executable_stamp,
        _stamp(os.path.join(os.path.dirname(os.path.dirname(path)), "pyvenv.cfg")),
        [os.environ.get(name) for name in _PROBE_ENVIRONMENT],
        probe_digest(),
    ]
    name = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
    store = os.path.join(_cache_dir, f"{name}.json")
    try:
        with open(store, encoding="utf-8") as file:
            cached = json.load(file)
    except (OSError, ValueError):
        return None, (store, key)
    if not isinstance(cached, dict) or cached.get("key") != key:
        return None, (store, key)
    for directory, stamp in cached.get("directories", ()):
        if _stamp(directory) != stamp:
            return None, (store, key)
    return cached.get("facts"), (store, key)


def _store_facts(
    store: tuple[str, list[Any]], facts: dict[str, Any], path: str, started: int
) -> None:
    """Keep ``facts`` for the next run, unless they may not hold for it.

    Not when the file run is not the interpreter that answered -- a pyenv,
    asdf or mise shim, which never changes while the version it picks does
    -- nor when a directory on its search path changed while it was probed.
    """
    location, key = store
    if os.path.realpath(facts.get("executable") or "") != os.path.realpath(path):
        return
    directories = [*facts.get("path", ()), facts.get("user_site")]
    for directory in directories:
        stamp = _stamp(directory) if directory else None
        if stamp is not None and stamp[0] >= started:
            return
    entry = {
        "key": key,
        "directories": [
            [directory, _stamp(directory)] for directory in directories if directory
        ],
        "facts": facts,
    }
    try:
        os.makedirs(os.path.dirname(location), exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(location), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(entry, file)
        os.replace(temporary, location)
        _prune(os.path.dirname(location))
    except OSError:
        logger.debug("Could not keep the facts of %s", path, exc_info=True)


def _prune(directory: str) -> None:
    """Keep the newest :data:`_CACHE_ENTRIES` answers."""
    with os.scandir(directory) as entries:
        kept = [entry for entry in entries if entry.name.endswith(".json")]
    if len(kept) <= _CACHE_ENTRIES:
        return
    kept.sort(key=lambda entry: entry.stat().st_mtime_ns)
    for entry in kept[: len(kept) - _CACHE_ENTRIES]:
        try:
            os.unlink(entry.path)
        except OSError:
            pass
