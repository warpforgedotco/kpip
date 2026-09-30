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

import json
import os
import shutil
import subprocess
import sys

from kpip.core.caches import register_table
from kpip.core.errors import CommandError
from kpip.core.compiled import is_compiled, is_own_interpreter

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
    """The Python at ``executable``, read by running :data:`PROBE` with it."""
    key = os.path.realpath(executable)
    found = _interpreters.get(key)
    if found is None:
        try:
            result = subprocess.run(
                [executable, "-c", PROBE],
                capture_output=True,
                text=True,
                check=True,
            )
            facts = json.loads(result.stdout)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise CommandError(
                f"Could not read the Python interpreter {executable}: {exc}"
            ) from exc
        found = Interpreter(facts)
        _interpreters[key] = found
    return found


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


def identify(python: str) -> str:
    """The interpreter ``--python`` names: the file, or an environment's.

    As pip: a directory is taken for a virtual environment, and its
    ``bin/python`` or ``Scripts/python.exe`` is the interpreter.
    """
    if os.path.isdir(python):
        for name in ("bin/python", "Scripts/python.exe"):
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
    """
    python = os.environ.get("KPIP_PYTHON")
    if python:
        return probe(identify(python))
    if not is_compiled():
        return own_interpreter()
    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        prefix = os.environ.get(variable)
        if prefix:
            return probe(identify(prefix))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found is not None:
            return probe(found)
    if not installing:
        return own_interpreter()
    raise CommandError(
        "No Python interpreter to install for: activate an environment, "
        "or name one with --python"
    )


_interpreters: dict[str | None, Interpreter] = register_table({})
