from __future__ import annotations

import os

from kpip.core.errors import InstallationError


def change_root(new_root: str, pathname: str) -> str:
    """Return 'pathname' with 'new_root' prepended.

    If 'pathname' is relative, this is equivalent to os.path.join(new_root, pathname).
    Otherwise, it requires making 'pathname' relative and then joining the
    two, which is tricky on DOS/Windows and Mac OS.

    This is borrowed from Python's standard library's distutils module.
    """
    if os.name == "posix":
        if not os.path.isabs(pathname):
            return os.path.join(new_root, pathname)
        return os.path.join(new_root, pathname[1:])

    if os.name == "nt":
        drive, path = os.path.splitdrive(pathname)
        if path[0] == "\\":
            path = path[1:]
        return os.path.join(new_root, path)

    raise InstallationError(
        f"Unknown platform: {os.name}\nCan not change root path prefix on unknown platform.",
    )
