from __future__ import annotations

import os
import sys

from kpip.core.utils import CACHE_VERSION_TAG, versioned_bucket


def user_cache_dir(appname: str) -> str:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            "~\\AppData\\Local",
        )
        return os.path.join(local, appname, "Cache")
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return os.path.join(xdg_cache, appname)
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", appname)
    return os.path.join(home, ".cache", appname)


# Version 2: entries nest one 256-wide level deep instead of five (see
# network/cache.py:get_cache_path).
HTTP_CACHE_BUCKET = versioned_bucket("http", 2)
WHEEL_CACHE_BUCKET = versioned_bucket("wheels", 2)


def http_cache_path(cache_dir: str) -> str:
    """The HTTP page cache directory under cache directory ``cache_dir``."""
    return os.path.join(cache_dir, HTTP_CACHE_BUCKET)


def cache_root(explicit: str | None = None) -> str:
    """The user-facing cache root: explicit, then ``KPIP_CACHE_DIR``, then default."""

    return explicit or os.environ.get("KPIP_CACHE_DIR") or user_cache_dir("kpip")


def versioned_cache_dir(root: str) -> str:
    """The directory under ``root`` that holds this kpip's cache formats.

    Every persisted cache lives under one ``v<N>`` directory named by
    ``CACHE_VERSION``; bumping it retires the whole tree at once, and a purge
    removes every ``v*`` directory. Individual stores version themselves
    inside it (``core/utils.py:versioned_bucket``) so that one changing format
    does not discard the rest.
    """

    return os.path.join(root, CACHE_VERSION_TAG)


def resolve_cache_dir(explicit: str | None = None) -> str:
    """The cache a command should use: explicit, then ``KPIP_CACHE_DIR``, then default.

    Callers that must honor ``--no-cache-dir`` check that themselves; this
    answers only "which directory".
    """

    return versioned_cache_dir(cache_root(explicit))


_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))


def command_cache_dir(explicit: str | None, disabled: bool) -> str | None:
    """The cache a command uses, or ``None`` when caching is turned off.

    ``--no-cache-dir`` or a truthy ``KPIP_NO_CACHE_DIR`` turns it off;
    otherwise it is :func:`resolve_cache_dir`. Every command that caches asks
    here, so ``lock``, ``install`` and ``download`` agree on both.
    """

    if disabled:
        return None

    if os.environ.get("KPIP_NO_CACHE_DIR", "").strip().lower() in _TRUE_VALUES:
        return None

    return resolve_cache_dir(explicit)


def site_config_dirs(appname: str) -> list[str]:
    if sys.platform == "win32":
        common = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return [os.path.join(common, appname)]
    if sys.platform == "darwin":
        xdg_data_dirs = os.environ.get("XDG_DATA_DIRS")
        if xdg_data_dirs:
            return [
                os.path.join(path, appname) for path in xdg_data_dirs.split(os.pathsep)
            ]
        paths: list[str] = []
        prefix = sys.prefix
        if prefix.startswith("/opt/homebrew/opt/python@"):
            paths.append("/opt/homebrew/share/" + appname)
        paths.append(f"/Library/Application Support/{appname}")
        return paths
    xdg_config_dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    paths = [
        os.path.join(path, appname)
        for path in xdg_config_dirs.split(os.pathsep)
        if path
    ]
    return paths + ["/etc"]


def user_config_dir(appname: str, roaming: bool = True) -> str:
    if sys.platform == "win32":
        base = "APPDATA" if roaming else "LOCALAPPDATA"
        root = os.environ.get(base) or os.path.expanduser(
            "~\\AppData\\Roaming" if roaming else "~\\AppData\\Local",
        )
        return os.path.join(root, appname)
    if sys.platform == "darwin":
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home and os.path.isdir(os.path.join(xdg_data_home, appname)):
            return os.path.join(xdg_data_home, appname)
        home = os.path.expanduser("~")
        support = os.path.join(home, "Library", "Application Support")
        if os.path.isdir(support):
            return os.path.join(support, appname)
        return os.path.join(home, ".config", appname)
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return os.path.join(xdg_config_home, appname)
    home = os.path.expanduser("~")
    return os.path.join(home, ".config", appname)
