"""The code kpip runs with another Python to learn about it.

The probe gathers, and decides nothing: everything kpip decides from the
facts, it decides the same way for its own interpreter. It runs under
whatever Python the user has, so it is written for the oldest kpip installs
for (:data:`OLDEST_TARGET`). The wheel tags come from the vendored
``packaging``, sent along as text and run in the target, so they are
exactly what that interpreter supports -- manylinux and musllinux included,
which ``packaging`` reads from the interpreter's own executable.
"""

from __future__ import annotations

import hashlib
import functools

from kpip._internal.utils.compat import open_text_resource

OLDEST_TARGET = (3, 10)
"""The oldest Python kpip installs for, as pip's ``--python`` does."""

SAFE_PATH = 'import sys\nif sys.path and sys.path[0] == "":\n    del sys.path[0]\n'
"""Run first by code given to another interpreter with ``-c`` or on its
standard input, which puts the working directory first on its ``sys.path``:
a project's own ``platform.py`` or ``json.py`` must not stand in for the
standard library. Only that entry: under ``PYTHONSAFEPATH``, or an
embeddable Python's ``._pth``, the first entry is the standard library's."""

# Loaded in this order: each imports only the ones before it.
_PACKAGING_MODULES = ("_elffile", "_manylinux", "_musllinux", "tags")

PROBE = r"""
import importlib.util
import json
import os
import platform
import site
import struct
import sys
import sysconfig
import types


def full_version(info):
    version = "%d.%d.%d" % (info.major, info.minor, info.micro)
    if info.releaselevel != "final":
        version += info.releaselevel[0] + str(info.serial)
    return version


def load_tags():
    package = types.ModuleType("_kpip_probe_packaging")
    package.__path__ = []
    sys.modules[package.__name__] = package
    for name in PACKAGING_MODULES:
        module = types.ModuleType(package.__name__ + "." + name)
        module.__package__ = package.__name__
        sys.modules[module.__name__] = module
        setattr(package, name, module)
        exec(compile(PACKAGING_SOURCES[name], module.__name__, "exec"), module.__dict__)
    return package.tags


def tag_facts():
    if sys.version_info[:2] < OLDEST_TARGET:
        return {}
    tags = load_tags()
    name = tags.interpreter_name()
    if name == "cp":
        abis = tags._cpython_abis(sys.version_info[:2])
    else:
        abis = tags._generic_abi()
    return {
        "tags": [str(tag) for tag in tags.sys_tags()],
        "platforms": list(tags.platform_tags()),
        "interpreter_name": name,
        "interpreter_version": tags.interpreter_version(),
        "abis": list(abis),
    }


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
    result = {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "exec_prefix": sys.exec_prefix,
        "version": list(sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "magic": importlib.util.MAGIC_NUMBER.hex(),
        "path": [entry for entry in sys.path if entry],
        "stdlib": sysconfig.get_path("stdlib"),
        "platform": sysconfig.get_platform(),
        "platlibdir": getattr(sys, "platlibdir", "lib"),
        "pointer_bits": struct.calcsize("P") * 8,
        "site_packages": site.getsitepackages(),
        "user_site": site.getusersitepackages(),
        "user_base": site.getuserbase(),
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
            "os_name": os.name,
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
    result.update(tag_facts())
    return result


sys.stdout.write(json.dumps(facts()))
"""


@functools.cache
def probe_source() -> str:
    """The whole program a probed interpreter runs, ``packaging`` included."""
    sources = {}
    for name in _PACKAGING_MODULES:
        with open_text_resource("kpip._vendor.packaging", f"{name}.py") as source:
            sources[name] = source.read()
    return (
        SAFE_PATH
        + f"OLDEST_TARGET = {OLDEST_TARGET!r}\n"
        + f"PACKAGING_MODULES = {_PACKAGING_MODULES!r}\n"
        + f"PACKAGING_SOURCES = {sources!r}\n"
        + PROBE
    )


@functools.cache
def probe_digest() -> str:
    """Identifies the probe, so a cached answer from another is not used."""
    return hashlib.sha256(probe_source().encode()).hexdigest()[:16]
