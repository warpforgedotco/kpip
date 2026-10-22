"""
This code wraps the vendored appdirs module to so the return values are
compatible for the current pip code base.

The intention is to rewrite current usages gradually, keeping the tests pass,
and eventually drop this after all usages are changed.
"""

import os
import sys

from kpip._vendor import platformdirs as _appdirs
from kpip._vendor.platformdirs.macos import MacOS as _MacOS

from kpip._internal.interpreters import target_interpreter


class _TargetMacOS(_MacOS):
    """platformdirs' macOS directories, with Homebrew's found from the target
    interpreter: platformdirs reads ``sys.base_prefix``, which in the compiled
    kpip is the bundled CPython's, never Homebrew's."""

    def _base_site_dirs(self) -> list[str]:
        base_prefix = target_interpreter().base_prefix
        path_list = []
        if "/opt/python" in base_prefix:
            homebrew_prefix = base_prefix.split("/opt/python")[0]
            path_list.append(
                self._join_app_name_and_version(f"{homebrew_prefix}/share")
            )
        path_list.append(
            self._join_app_name_and_version("/Library/Application Support")
        )
        return path_list


def user_cache_dir(appname: str) -> str:
    return _appdirs.user_cache_dir(appname, appauthor=False)


def _macos_user_config_dir(appname: str, roaming: bool = True) -> str:
    # Use ~/Application Support/pip, if the directory exists.
    path = _appdirs.user_data_dir(appname, appauthor=False, roaming=roaming)
    if os.path.isdir(path):
        return path

    # Use a Linux-like ~/.config/pip, by default.
    linux_like_path = "~/.config/"
    if appname:
        linux_like_path = os.path.join(linux_like_path, appname)

    return os.path.expanduser(linux_like_path)


def user_config_dir(appname: str, roaming: bool = True) -> str:
    if sys.platform == "darwin":
        return _macos_user_config_dir(appname, roaming)

    return _appdirs.user_config_dir(appname, appauthor=False, roaming=roaming)


# for the discussion regarding site_config_dir locations
# see <https://github.com/pypa/pip/issues/1733>
def site_config_dirs(appname: str) -> list[str]:
    if sys.platform == "darwin":
        dirval = _TargetMacOS(appname, appauthor=False, multipath=True).site_data_dir
        return dirval.split(os.pathsep)

    dirval = _appdirs.site_config_dir(appname, appauthor=False, multipath=True)
    if sys.platform == "win32":
        return [dirval]

    # Unix-y system. Look in /etc as well.
    return dirval.split(os.pathsep) + ["/etc"]
