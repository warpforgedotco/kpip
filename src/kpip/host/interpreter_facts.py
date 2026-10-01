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
import re
import shutil
import subprocess
import sys
import tempfile
import time

from kpip.core import run_options
from kpip.core.appdirs import resolve_cache_dir
from kpip.core.caches import register_table
from kpip.core.errors import CommandError
from kpip.core.utils import versioned_bucket
from kpip.core.compiled import is_compiled, is_own_interpreter

try:
    import winreg
except ImportError:
    winreg = None  # ty: ignore[invalid-assignment]

SAFE_PATH = 'import sys\nif sys.path and sys.path[0] == "":\n    del sys.path[0]\n'
"""Run first by code given to another interpreter with ``-c`` or on its
standard input, which puts the working directory first on its
``sys.path``: a project's own ``platform.py`` or ``json.py`` must not stand
in for the standard library. Only that entry: under ``PYTHONSAFEPATH``, or
an embeddable Python's ``._pth``, the first is the standard library's."""

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
        "facts",
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
        # As read, for the environments derived from it (remember_environment).
        self.facts = facts
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

    The probe goes on its standard input rather than with ``-c``: a
    ``.bat`` shim, as pyenv-win puts on ``PATH``, runs through ``cmd.exe``,
    which ends an argument at its first newline. Kept by the path as given,
    not resolved: a virtual environment's ``python`` links to its base's.
    """
    key = os.path.abspath(executable)
    found = _interpreters.get(key)
    if found is None:
        facts, store = _cached_facts(executable)
        if facts is None:
            started = time.time_ns()
            facts = _run_probe(executable)
            if store is not None:
                _store_facts(store, facts, executable, started)
        found = Interpreter(facts)
        _interpreters[key] = found
    return found


def _run_probe(executable: str) -> dict:
    try:
        result = subprocess.run(
            [executable, "-"],
            input=SAFE_PATH + PROBE,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CommandError(f"Could not run the Python interpreter {executable}: {exc}")
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
            f"The Python interpreter {executable} did not answer as one: "
            "name a working one with --python."
        ) from None


_PROBE_CACHE = versioned_bucket("interpreters", 1)

_PROBE_DIGEST = hashlib.sha256((SAFE_PATH + PROBE).encode()).hexdigest()[:16]

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

_PROBE_CACHE_ENTRIES = 256
"""Probes kept: each build environment leaves one."""


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


def remember_environment(
    creator: Interpreter, env_path: str, executable: str, purelib: str, platlib: str
) -> None:
    """Keep, for the next kpip to probe it, what a fresh environment
    ``creator`` made at ``env_path`` would say of itself.

    The kpip that installs a build environment's requirements is told the
    environment's Python, and would run it only to learn what follows from
    its creator's facts: its own executable and prefix, the ``venv`` scheme
    preferred, no user site, its own site-packages where its creator's site
    directories were on ``sys.path`` -- the environment's prefix first, on
    Windows -- and its config's ``base`` and ``platbase``, which the install
    schemes are laid out from. Its ``prefix`` and ``exec_prefix`` follow the
    environment in some versions and stay the build's in others; no scheme
    or decision of kpip's reads them. Only for a creator with a ``venv``
    scheme, there from 3.11.
    """
    if "venv" not in creator.schemes:
        return
    _, store = _cached_facts(executable)
    if store is None:
        return
    base = creator.facts
    creator_sites = {creator.user_site}
    try:
        creator_paths = creator.paths(creator.preferred.get("prefix", "posix_prefix"))
        creator_sites |= {creator_paths["purelib"], creator_paths["platlib"]}
    except AttributeError, KeyError:
        return
    if os.name == "nt":
        creator_sites.add(creator.prefix)
    interpreter_path = []
    for entry in base["path"]:
        if entry in creator_sites:
            break
        interpreter_path.append(entry)
    own_sites = [env_path, purelib] if os.name == "nt" else [purelib]
    if platlib not in own_sites:
        own_sites.append(platlib)
    facts = {
        **base,
        "executable": executable,
        "prefix": env_path,
        "path": [*interpreter_path, *own_sites],
        "user_site_enabled": False,
        "preferred": {**base["preferred"], "prefix": "venv"},
        "config": {**base["config"], "base": env_path, "platbase": env_path},
    }
    _store_facts(store, facts, executable)


def _store_facts(
    store: tuple[str, list], facts: dict, executable: str, started: int | None = None
) -> None:
    """Keep ``facts`` for the next run, unless they may not hold for it.

    Not when the file run is not the interpreter that answered -- a pyenv,
    asdf or mise shim, which never changes while the version it picks does
    -- nor when a directory on its ``sys.path`` changed while it was probed
    (``started``): its facts may predate the change.
    """
    path, key = store
    if os.path.realpath(facts.get("executable") or "") != os.path.realpath(executable):
        return
    directories = [*facts.get("path", ()), facts.get("user_site")]
    if started is not None:
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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(entry, file)
        os.replace(temporary, path)
        _prune(os.path.dirname(path))
    except OSError:
        pass


def _prune(directory: str) -> None:
    """Keep the newest :data:`_PROBE_CACHE_ENTRIES` probes."""
    with os.scandir(directory) as entries:
        kept = [entry for entry in entries if entry.name.endswith(".json")]
    if len(kept) <= _PROBE_CACHE_ENTRIES:
        return
    kept.sort(key=lambda entry: entry.stat().st_mtime_ns)
    for entry in kept[: len(kept) - _PROBE_CACHE_ENTRIES]:
        try:
            os.unlink(entry.path)
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


def system_requested() -> bool:
    """Whether ``--system`` (or ``KPIP_SYSTEM_PYTHON``) asks for a system
    Python: ``install`` and ``uninstall`` set it from the option."""
    return os.environ.get("KPIP_SYSTEM_PYTHON", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def active_environments() -> list[tuple[str, str]]:
    """The environments an install is for, before any Python on ``PATH``:
    how each was found, and its prefix, in the order uv looks.

    The activated virtual environment; a named conda environment; a
    ``.venv`` in the working directory or one above it, or the environment
    the working directory is inside; then conda's base environment, which
    is a system Python more than an environment someone chose. With
    ``--system``, as uv, only the last: the rest are passed over.
    """
    found: list[tuple[str, str]] = []
    conda = os.environ.get("CONDA_PREFIX")
    base = bool(conda) and _is_conda_base(conda)
    if system_requested():
        return [(f"CONDA_PREFIX names {conda}", conda)] if conda and base else []
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        found.append((f"VIRTUAL_ENV names {virtual_env}", virtual_env))
    if conda and not base:
        found.append((f"CONDA_PREFIX names {conda}", conda))
    discovered = _working_directory_environment()
    if discovered is not None:
        found.append((f"the environment at {discovered}", discovered))
    if conda and base:
        found.append((f"CONDA_PREFIX names {conda}", conda))
    return found


def _working_directory_environment() -> str | None:
    try:
        directory = os.getcwd()
    except OSError:
        return None
    while True:
        if os.path.isfile(os.path.join(directory, "pyvenv.cfg")):
            return directory
        dot_venv = os.path.join(directory, ".venv")
        if os.path.isdir(dot_venv) or os.path.islink(dot_venv):
            # As uv: not passed over for one further up, which nobody
            # meant -- nor taken, a conda environment included.
            if not os.path.isfile(os.path.join(dot_venv, "pyvenv.cfg")):
                raise NotAnEnvironment(dot_venv)
            return dot_venv
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _is_conda_base(prefix: str) -> bool:
    """Whether ``CONDA_PREFIX`` is conda's base environment, by uv's rule.

    ``_CONDA_ROOT`` names base when conda sets it. Otherwise
    ``CONDA_DEFAULT_ENV`` is the active environment's name, or its path when
    it was created with ``-p``: an environment kept in a directory of its
    own name is not base. Pixi never makes a base environment.
    """

    def same(a: str, b: str) -> bool:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
            os.path.normpath(b)
        )

    if os.path.isfile(os.path.join(prefix, "conda-meta", "pixi")):
        return False
    root = os.environ.get("_CONDA_ROOT")
    if root and same(prefix, root):
        return True
    name = os.environ.get("CONDA_DEFAULT_ENV")
    if not name or same(prefix, name):
        return False
    return os.path.basename(os.path.normpath(prefix)) != name


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
    compiled kpip, which has none, the first of ``active_environments()``,
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
        key = ("found", installing, system_requested())
    found = _interpreters.get(key)
    if found is None:
        found = _interpreters[key] = _find_target(python, installing=installing)
    return found


class EnvironmentWithoutPython(CommandError):
    """The environment an install is for has no Python that runs: what it
    would be for is not there. ``summary`` says which, for ``--version``."""

    def __init__(self, where: str) -> None:
        self.summary = f"{where}, which has no working Python"
        super().__init__(
            f"{self.summary}: activate another environment, deactivate this "
            "one, or name one with --python"
        )


class NotAnEnvironment(EnvironmentWithoutPython):
    """A ``.venv`` with no ``pyvenv.cfg``: uv refuses it, rather than look
    past it or guess what it is."""

    def __init__(self, dot_venv: str) -> None:
        self.summary = f"{dot_venv} is not a virtual environment"
        CommandError.__init__(
            self,
            f"{self.summary}: it has no pyvenv.cfg. Recreate it, remove it, "
            "or name a Python with --python",
        )


def registered_pythons() -> list[str]:
    """The Pythons Windows has registered (PEP 514), newest first.

    The python.org installer leaves ``python`` off ``PATH`` unless asked,
    and registers the interpreter instead, as the ``py`` launcher and uv
    find it. Nothing elsewhere.
    """
    if sys.platform != "win32" or winreg is None:
        return []
    found: list[tuple[tuple[int, ...], str]] = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            company = winreg.OpenKey(root, r"Software\Python\PythonCore")
        except OSError:
            continue
        with company:
            index = 0
            while True:
                try:
                    tag = winreg.EnumKey(company, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(company, rf"{tag}\InstallPath") as install:
                        try:
                            executable = winreg.QueryValueEx(install, "ExecutablePath")[
                                0
                            ]
                        except OSError:
                            executable = os.path.join(
                                winreg.QueryValueEx(install, "")[0], "python.exe"
                            )
                except OSError:
                    continue
                version = tuple(
                    int(part) for part in re.findall(r"\d+", tag.split("-")[0])[:2]
                )
                if os.path.isfile(executable):
                    found.append((version, executable))
    found.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(executable for _, executable in found))


def _find_target(python: str | None, *, installing: bool) -> Interpreter:
    if python:
        return probe(identify(python))
    for where, prefix in active_environments():
        try:
            return probe(identify(prefix))
        except CommandError as exc:
            if installing:
                raise EnvironmentWithoutPython(where) from exc
            # Nothing is installed: resolving needs no environment.
            continue
    tried = []
    candidates = [shutil.which(name) for name in ("python3", "python")]
    for found in [*candidates, *registered_pythons()]:
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
        "No Python interpreter to install for: VIRTUAL_ENV and CONDA_PREFIX "
        "are unset, there is no .venv here or above, and "
        + (
            f"none of {', '.join(tried)} answered"
            if tried
            else "there is no python3 or python on PATH"
            + (" nor a registered Python" if os.name == "nt" else "")
        )
        + ". Activate an environment, or name a Python with --python."
    )


# Interpreters by the realpath of their executable, this process's under
# None, and the target by what it was found from.
_interpreters: dict[object, Interpreter] = register_table({})
