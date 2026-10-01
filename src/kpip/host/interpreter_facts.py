"""What kpip needs to know about the Python it installs for.

pip works for the interpreter it runs under. kpip may not: ``--python`` names
another, and a compiled kpip has none of its own. So the facts are read from
the interpreter itself -- in this process when it is the one running kpip,
otherwise by running :data:`PROBE` with it -- and everything kpip decides
from them, it decides the same way for any interpreter.

The probe only gathers. It runs under whatever Python the environment has,
so it is written for old ones too.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from kpip.core import run_options
from kpip.core.appdirs import resolve_cache_dir
from kpip.core.caches import register_table
from kpip.core.errors import CommandError
from kpip.core.utils import versioned_bucket
from kpip.core.compiled import is_compiled, is_own_interpreter

SAFE_PATH = "import sys\ndel sys.path[0]\n"
"""Run first by code given to another interpreter with ``-c``, which puts
the working directory first on its ``sys.path``: a project's own
``platform.py`` or ``json.py`` must not stand in for the standard library."""

PROBE = r"""
import importlib.util
import json
import platform
import site
import struct
import sys
import sysconfig
from importlib.machinery import EXTENSION_SUFFIXES


def full_version(info):
    version = "%d.%d.%d" % (info.major, info.minor, info.micro)
    if info.releaselevel != "final":
        version += info.releaselevel[0] + str(info.serial)
    return version


def facts():
    preferred = {}
    if hasattr(sysconfig, "get_preferred_scheme"):
        for key in ("prefix", "home", "user"):
            try:
                preferred[key] = sysconfig.get_preferred_scheme(key)
            except KeyError:
                pass
    config = {}
    for name, value in sysconfig.get_config_vars().items():
        if value is None or isinstance(value, (str, int, float)):
            config[name] = value
    return {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "version": list(sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "magic": importlib.util.MAGIC_NUMBER.hex(),
        "path": [entry for entry in sys.path if entry],
        "stdlib": sysconfig.get_path("stdlib"),
        "platform": sysconfig.get_platform(),
        "pointer_bits": struct.calcsize("P") * 8,
        "extension_suffixes": list(EXTENSION_SUFFIXES),
        "debug_refcount": hasattr(sys, "gettotalrefcount"),
        "user_site": site.getusersitepackages(),
        "user_site_enabled": bool(site.ENABLE_USER_SITE),
        "venv": importlib.util.find_spec("venv") is not None,
        "schemes": dict(
            (name, sysconfig.get_paths(name, expand=False))
            for name in sysconfig.get_scheme_names()
        ),
        "preferred": preferred,
        "config": config,
        "markers": {
            "implementation_name": sys.implementation.name,
            "implementation_version": full_version(sys.implementation.version),
            "os_name": __import__("os").name,
            "platform_machine": platform.machine(),
            "platform_release": platform.release(),
            "platform_system": platform.system(),
            "platform_version": platform.version(),
            "python_full_version": platform.python_version(),
            "platform_python_implementation": platform.python_implementation(),
            "python_version": ".".join(platform.python_version_tuple()[:2]),
            "sys_platform": sys.platform,
        },
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(facts()))
"""


class Interpreter:
    """One Python installation, as :data:`PROBE` found it."""

    __slots__ = (
        "base_prefix",
        "cache_tag",
        "config",
        "debug_refcount",
        "executable",
        "extension_suffixes",
        "implementation",
        "magic",
        "markers",
        "path",
        "platform",
        "pointer_bits",
        "preferred",
        "prefix",
        "schemes",
        "stdlib",
        "user_site",
        "user_site_enabled",
        "venv",
        "version",
    )

    def __init__(self, facts: dict) -> None:
        self.executable: str = facts["executable"]
        self.prefix: str = facts["prefix"]
        self.base_prefix: str = facts["base_prefix"]
        self.version: tuple[int, int, int] = tuple(facts["version"])
        self.implementation: str = facts["implementation"]
        self.cache_tag: str | None = facts["cache_tag"]
        self.magic: str = facts["magic"]
        self.path: list[str] = facts["path"]
        self.stdlib: str = facts["stdlib"]
        self.platform: str = facts["platform"]
        self.pointer_bits: int = facts["pointer_bits"]
        self.extension_suffixes: list[str] = facts["extension_suffixes"]
        self.debug_refcount: bool = facts["debug_refcount"]
        self.user_site: str = facts["user_site"]
        self.user_site_enabled: bool = facts["user_site_enabled"]
        # Whether it can create the environments builds run in.
        self.venv: bool = facts.get("venv", True)
        self.schemes: dict[str, dict[str, str]] = facts["schemes"]
        self.preferred: dict[str, str] = facts["preferred"]
        self.config: dict[str, object] = facts["config"]
        self.markers: dict[str, str] = facts["markers"]

    @property
    def is_own(self) -> bool:
        """Whether this is the Python running kpip."""
        return _interpreters.get(None) is self

    @property
    def major_minor(self) -> str:
        return "%d.%d" % self.version[:2]

    @property
    def in_virtualenv(self) -> bool:
        """Whether this is a PEP 405 virtual environment's interpreter."""
        return self.prefix != self.base_prefix

    def paths(self, scheme: str, variables: dict[str, str] | None = None) -> dict:
        """``sysconfig.get_paths(scheme, variables)``, as this interpreter
        would answer it."""
        values: dict[str, object] = dict(variables or {})
        for name, value in self.config.items():
            values.setdefault(name, value)
        if os.name == "nt":
            values = values | {"platlibdir": "lib"}
        result = {}
        for key, template in self.schemes[scheme].items():
            if os.name in ("posix", "nt"):
                template = os.path.expanduser(template)
            try:
                text = template.format(**values)
            except KeyError as missing:
                try:
                    text = template.format(**os.environ)
                except KeyError:
                    raise AttributeError(str(missing)) from None
            result[key] = os.path.normpath(text)
        return result


def own_interpreter() -> Interpreter:
    """The Python running this kpip, read in-process."""
    found = _interpreters.get(None)
    if found is None:
        namespace: dict = {"__name__": "kpip_interpreter_probe"}
        exec(PROBE, namespace)  # noqa: S102 - kpip's own constant
        found = Interpreter(namespace["facts"]())
        _interpreters[None] = found
    return found


def probe(executable: str) -> Interpreter:
    """The Python at ``executable``, read by running :data:`PROBE` with it.

    A compiled kpip has no Python of its own, so every command runs one --
    35 to 50 ms -- unless an earlier run left its answer in the cache
    (:func:`_cached_facts`).
    """
    key = os.path.realpath(executable)
    found = _interpreters.get(key)
    if found is None:
        facts, store = _cached_facts(executable)
        if facts is None:
            try:
                result = subprocess.run(
                    [executable, "-c", SAFE_PATH + PROBE],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                facts = json.loads(result.stdout)
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                raise CommandError(
                    f"Could not read the Python interpreter {executable}: {exc}"
                ) from exc
            if store is not None:
                _store_facts(store, facts)
        found = Interpreter(facts)
        _interpreters[key] = found
    return found


_PROBE_CACHE = versioned_bucket("interpreters", 1)

_PROBE_DIGEST = hashlib.sha256((SAFE_PATH + PROBE).encode()).hexdigest()[:16]

# What changes the facts without changing the interpreter's files: where it
# looks for modules and for its user site.
_PROBE_ENVIRONMENT = (
    "PYTHONHOME",
    "PYTHONNOUSERSITE",
    "PYTHONPATH",
    "PYTHONPLATLIBDIR",
    "PYTHONUSERBASE",
)


def _stamp(path: str) -> list[int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def _cached_facts(executable: str) -> tuple[dict | None, tuple[str, list] | None]:
    """An earlier probe of ``executable``, if nothing it depends on changed;
    and where to store this one's, or ``None`` without a cache.

    The facts hold while the interpreter's file, its environment's
    ``pyvenv.cfg``, the variables that move its search path, and this
    probe are what they were; and while every directory on its ``sys.path``,
    and its user site, has the modification time it had -- adding or
    removing a ``.pth`` file changes the directory's.
    """
    general = run_options.current
    if general.no_cache_dir or (
        os.environ.get("KPIP_NO_CACHE_DIR", "").strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        return None, None
    path = os.path.abspath(executable)
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
        _PROBE_DIGEST,
    ]
    name = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
    store = os.path.join(
        resolve_cache_dir(general.cache_dir), _PROBE_CACHE, f"{name}.json"
    )
    try:
        with open(store, encoding="utf-8") as file:
            cached = json.load(file)
    except OSError, ValueError:
        return None, (store, key)
    if not isinstance(cached, dict) or cached.get("key") != key:
        return None, (store, key)
    for directory, stamp in cached.get("directories", ()):
        if _stamp(directory) != stamp:
            return None, (store, key)
    return cached.get("facts"), (store, key)


def _store_facts(store: tuple[str, list], facts: dict) -> None:
    path, key = store
    directories = [*facts.get("path", ()), facts.get("user_site")]
    entry = {
        "key": key,
        "directories": [
            [directory, _stamp(directory)] for directory in directories if directory
        ],
        "facts": facts,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(entry, file)
        os.replace(temporary, path)
    except OSError:
        pass


def search_path() -> list[str]:
    """Where the target interpreter imports from: its ``sys.path``, or this
    process's own, live, when it is the Python running kpip."""
    interpreter = target_interpreter()
    return list(sys.path) if interpreter.is_own else interpreter.path


def interpreter_at(executable: str) -> Interpreter:
    """The Python at ``executable``: this process's own when it is that one."""
    if is_own_interpreter(executable):
        return own_interpreter()
    return probe(executable)


def environment_pythons() -> tuple[str, ...]:
    """Where an environment keeps its interpreter, relative to its prefix."""
    if os.name == "nt":
        return ("Scripts/python.exe", "python.exe", "bin/python")
    return ("bin/python", "Scripts/python.exe")


def identify(python: str) -> str:
    """The interpreter ``--python`` names: the file, or an environment's.

    As pip: a directory is taken for a virtual environment, and its
    ``bin/python`` or ``Scripts/python.exe`` is the interpreter -- or, on
    Windows, the ``python.exe`` a conda environment keeps at its top.
    """
    if os.path.isdir(python):
        for name in environment_pythons():
            candidate = os.path.join(python, name)
            if os.path.exists(candidate):
                return os.path.abspath(candidate)
    elif os.path.exists(python):
        return os.path.abspath(python)
    raise CommandError(f"Could not locate Python interpreter {python}")


def target_interpreter(*, installing: bool = True) -> Interpreter:
    """The Python kpip installs for, and resolves for.

    The one ``--python`` names; otherwise the one running kpip; and for a
    compiled kpip, which has none, the active virtual or conda environment's,
    else the ``python3`` or ``python`` on ``PATH``. A compiled kpip that finds
    none can still resolve -- for the CPython it was built with -- but not
    install: ``installing`` says which the caller needs.

    Callers ask once per link and once per module, and finding it again
    stats the interpreter's path, or every ``PATH`` entry, so the answer is
    kept: by ``--python``, which a command sets and restores, or for the
    process, which never changes the environments or ``PATH`` it searched.
    """
    python = os.environ.get("KPIP_PYTHON")
    if python:
        key: tuple[object, ...] = ("python", python)
    elif not is_compiled():
        return own_interpreter()
    else:
        key = ("found", installing)
    found = _interpreters.get(key)
    if found is None:
        found = _interpreters[key] = _find_target(python, installing=installing)
    return found


def _find_target(python: str | None, *, installing: bool) -> Interpreter:
    if python:
        return probe(identify(python))
    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        prefix = os.environ.get(variable)
        if prefix:
            return probe(identify(prefix))
    tried = []
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found is None or found in tried:
            continue
        tried.append(found)
        try:
            return probe(found)
        except CommandError:
            # Not a Python that answers: on Windows, python3 is often the
            # Microsoft Store's stub, beside a python that works.
            continue
    if not installing:
        return own_interpreter()
    raise CommandError(
        "No Python interpreter to install for: activate an environment, "
        "or name one with --python" + (f" (tried {', '.join(tried)})" if tried else "")
    )


# Interpreters by the realpath of their executable, this process's under
# None, and the target by what it was found from.
_interpreters: dict[object, Interpreter] = register_table({})
