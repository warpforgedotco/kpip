from __future__ import annotations

import logging
import os
import sys
import sysconfig
from collections.abc import Callable

from kpip._internal.exceptions import InvalidSchemeCombination, UserInstallationInvalid
from kpip._internal.interpreters import (
    Interpreter,
    installing_interpreter,
    target_interpreter,
)
from kpip._internal.models.scheme import SCHEME_KEYS, Scheme
from kpip._internal.utils.virtualenv import running_under_virtualenv

from .base import change_root, is_osx_framework

logger = logging.getLogger(__name__)


# Notes on _infer_* functions.
# Unfortunately ``get_default_scheme()`` didn't exist before 3.10, so there's no
# way to ask things like "what is the '_prefix' scheme on this platform". These
# functions try to answer that with some heuristics while accounting for ad-hoc
# platforms not covered by CPython's default sysconfig implementation. If the
# ad-hoc implementation does not fully implement sysconfig, we'll fall back to
# a POSIX scheme.

_AVAILABLE_SCHEMES = set(sysconfig.get_scheme_names())

_PREFERRED_SCHEME_API: Callable[[str], str] | None = getattr(
    sysconfig, "get_preferred_scheme", None
)


def _should_use_osx_framework_prefix() -> bool:
    """Check for Apple's ``osx_framework_library`` scheme.

    Python distributed by Apple's Command Line Tools has this special scheme
    that's used when:

    * This is a framework build.
    * We are installing into the system prefix.

    This does not account for ``pip install --prefix`` (also means we're not
    installing to the system prefix), which should use ``posix_prefix``, but
    logic here means ``_infer_prefix()`` outputs ``osx_framework_library``. But
    since ``prefix`` is not available for ``sysconfig.get_default_scheme()``,
    which is the stdlib replacement for ``_infer_prefix()``, presumably Apple
    wouldn't be able to magically switch between ``osx_framework_library`` and
    ``posix_prefix``. ``_infer_prefix()`` returning ``osx_framework_library``
    means its behavior is consistent whether we use the stdlib implementation
    or our own, and we deal with this special case in ``get_scheme()`` instead.
    """
    return (
        "osx_framework_library" in _AVAILABLE_SCHEMES
        and not running_under_virtualenv()
        and is_osx_framework()
    )


def _infer_prefix() -> str:
    """Try to find a prefix scheme for the current platform.

    This tries:

    * A special ``osx_framework_library`` for Python distributed by Apple's
      Command Line Tools, when not running in a virtual environment.
    * Implementation + OS, used by PyPy on Windows (``pypy_nt``).
    * Implementation without OS, used by PyPy on POSIX (``pypy``).
    * OS + "prefix", used by CPython on POSIX (``posix_prefix``).
    * Just the OS name, used by CPython on Windows (``nt``).

    If none of the above works, fall back to ``posix_prefix``.
    """
    if _PREFERRED_SCHEME_API:
        return _PREFERRED_SCHEME_API("prefix")
    if _should_use_osx_framework_prefix():
        return "osx_framework_library"
    implementation_suffixed = f"{sys.implementation.name}_{os.name}"
    if implementation_suffixed in _AVAILABLE_SCHEMES:
        return implementation_suffixed
    if sys.implementation.name in _AVAILABLE_SCHEMES:
        return sys.implementation.name
    suffixed = f"{os.name}_prefix"
    if suffixed in _AVAILABLE_SCHEMES:
        return suffixed
    if os.name in _AVAILABLE_SCHEMES:  # On Windows, prefx is just called "nt".
        return os.name
    return "posix_prefix"


def _infer_user() -> str:
    """Try to find a user scheme for the current platform."""
    if _PREFERRED_SCHEME_API:
        return _PREFERRED_SCHEME_API("user")
    if is_osx_framework() and not running_under_virtualenv():
        suffixed = "osx_framework_user"
    else:
        suffixed = f"{os.name}_user"
    if suffixed in _AVAILABLE_SCHEMES:
        return suffixed
    if "posix_user" not in _AVAILABLE_SCHEMES:  # User scheme unavailable.
        raise UserInstallationInvalid()
    return "posix_user"


def _infer_home() -> str:
    """Try to find a home for the current platform."""
    if _PREFERRED_SCHEME_API:
        return _PREFERRED_SCHEME_API("home")
    suffixed = f"{os.name}_home"
    if suffixed in _AVAILABLE_SCHEMES:
        return suffixed
    return "posix_home"


# Update these keys if the user sets a custom home.
_HOME_KEYS = [
    "installed_base",
    "base",
    "installed_platbase",
    "platbase",
    "prefix",
    "exec_prefix",
]
if sysconfig.get_config_var("userbase") is not None:
    _HOME_KEYS.append("userbase")


def get_scheme(
    dist_name: str,
    user: bool = False,
    home: str | None = None,
    root: str | None = None,
    isolated: bool = False,
    prefix: str | None = None,
) -> Scheme:
    """
    Get the "scheme" corresponding to the input parameters.

    :param dist_name: the name of the package to retrieve the scheme for, used
        in the headers scheme path
    :param user: indicates to use the "user" scheme
    :param home: indicates to use the "home" scheme
    :param root: root under which other directories are re-based
    :param isolated: ignored, but kept for distutils compatibility (where
        this controls whether the user-site pydistutils.cfg is honored)
    :param prefix: indicates to use the "prefix" scheme and provides the
        base directory for the same
    """
    if user and prefix:
        raise InvalidSchemeCombination("--user", "--prefix")
    if home and prefix:
        raise InvalidSchemeCombination("--home", "--prefix")

    interpreter = installing_interpreter()
    if home is not None:
        scheme_name = _target_scheme(interpreter, "home", _infer_home)
    elif user:
        scheme_name = _target_scheme(interpreter, "user", _infer_user)
    else:
        scheme_name = _target_scheme(interpreter, "prefix", _infer_prefix)

    # Special case: When installing into a custom prefix, use posix_prefix
    # instead of posix_local because it should *only* be used to redirecting
    # to /usr/local for system packager installs. Notably, posix_local is used
    # on Debian and its derivatives.
    # See also: https://discuss.python.org/t/18240
    if prefix is not None and scheme_name == "posix_local":
        scheme_name = "posix_prefix"

    # Special case: When installing into a custom prefix, use posix_prefix
    # instead of osx_framework_library. See _should_use_osx_framework_prefix()
    # docstring for details.
    if prefix is not None and scheme_name == "osx_framework_library":
        scheme_name = "posix_prefix"

    home_keys = _HOME_KEYS
    if not interpreter.is_own:
        home_keys = _HOME_KEYS[:6]
        if interpreter.get_config_var("userbase") is not None:
            home_keys = [*home_keys, "userbase"]
    if home is not None:
        variables = {k: home for k in home_keys}
    elif prefix is not None:
        variables = {k: prefix for k in home_keys}
    else:
        variables = {}

    paths = interpreter.get_paths(scheme_name, variables)

    # Logic here is very arbitrary, we're doing it for compatibility, don't ask.
    # 1. Pip historically uses a special header path in virtual environments.
    # 2. If the distribution name is not known, distutils uses 'UNKNOWN'. We
    #    only do the same when not running in a virtual environment because
    #    pip's historical header path logic (see point 1) did not do this.
    if running_under_virtualenv():
        if user:
            base = variables.get("userbase", interpreter.prefix)
        else:
            base = variables.get("base", interpreter.prefix)
        python_xy = f"python{interpreter.major_minor}"
        paths["include"] = os.path.join(base, "include", "site", python_xy)
    elif not dist_name:
        dist_name = "UNKNOWN"

    scheme = Scheme(
        platlib=paths["platlib"],
        purelib=paths["purelib"],
        headers=os.path.join(paths["include"], dist_name),
        scripts=paths["scripts"],
        data=paths["data"],
    )
    if root is not None:
        converted_keys = {}
        for key in SCHEME_KEYS:
            converted_keys[key] = change_root(root, getattr(scheme, key))
        scheme = Scheme(**converted_keys)
    return scheme


def _target_scheme(
    interpreter: Interpreter, key: str, infer: Callable[[], str]
) -> str:
    """The scheme ``key`` installs use, as the target interpreter prefers.

    Every Python kpip installs for into another process has
    ``sysconfig.get_preferred_scheme``, new in 3.10.
    """
    if interpreter.is_own:
        return infer()
    try:
        return interpreter.preferred_scheme(key)
    except KeyError:
        if key == "user":
            raise UserInstallationInvalid() from None
        raise


def _default_paths() -> dict[str, str]:
    interpreter = installing_interpreter()
    if interpreter.is_own:
        return sysconfig.get_paths()
    return interpreter.get_paths(interpreter.preferred_scheme("prefix"))


def get_bin_prefix() -> str:
    # Forcing to use /usr/local/bin for standard macOS framework installs.
    prefix = target_interpreter().prefix
    if sys.platform[:6] == "darwin" and prefix[:16] == "/System/Library/":
        return "/usr/local/bin"
    return _default_paths()["scripts"]


def get_purelib() -> str:
    return _default_paths()["purelib"]


def get_platlib() -> str:
    return _default_paths()["platlib"]
