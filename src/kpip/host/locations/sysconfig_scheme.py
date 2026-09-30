"""pip's install schemes, for the interpreter kpip installs for.

The logic is pip's; the ``sysconfig`` it consults is the target
interpreter's, as :mod:`kpip.host.interpreter_facts` read it.
"""

from __future__ import annotations

import os

from kpip.core.errors import InstallationError
from kpip.host.interpreter_facts import Interpreter
from kpip.host.scheme import SCHEME_KEYS, Scheme

from .base import change_root


class InvalidSchemeCombination(InstallationError):
    """An invalid combination of installation location options was requested."""

    def __str__(self) -> str:
        before = ", ".join(str(arg) for arg in self.args[:-1])
        return f"Cannot set {before} and {self.args[-1]} together"


class UserInstallationInvalid(InstallationError):
    """A user installation was requested where it is not available."""

    def __str__(self) -> str:
        return "User base directory is not specified"


def is_osx_framework(interpreter: Interpreter) -> bool:
    return bool(interpreter.config.get("PYTHONFRAMEWORK"))


def should_use_osx_framework_prefix(interpreter: Interpreter) -> bool:
    """Check for Apple's ``osx_framework_library`` scheme.

    Python distributed by Apple's Command Line Tools has this special scheme
    that's used when:

    * This is a framework build.
    * We are installing into the system prefix.

    This does not account for ``kpip install --prefix`` (also means we're not
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
        "osx_framework_library" in interpreter.schemes
        and not interpreter.in_virtualenv
        and is_osx_framework(interpreter)
    )


def infer_prefix(interpreter: Interpreter) -> str:
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
    if "prefix" in interpreter.preferred:
        return interpreter.preferred["prefix"]
    if should_use_osx_framework_prefix(interpreter):
        return "osx_framework_library"
    implementation_suffixed = f"{interpreter.implementation}_{os.name}"
    if implementation_suffixed in interpreter.schemes:
        return implementation_suffixed
    if interpreter.implementation in interpreter.schemes:
        return interpreter.implementation
    suffixed = f"{os.name}_prefix"
    if suffixed in interpreter.schemes:
        return suffixed
    if os.name in interpreter.schemes:
        return os.name
    return "posix_prefix"


def infer_user(interpreter: Interpreter) -> str:
    """Try to find a user scheme for the current platform."""
    if "user" in interpreter.preferred:
        return interpreter.preferred["user"]
    if is_osx_framework(interpreter) and not interpreter.in_virtualenv:
        suffixed = "osx_framework_user"
    else:
        suffixed = f"{os.name}_user"
    if suffixed in interpreter.schemes:
        return suffixed
    if "posix_user" not in interpreter.schemes:
        raise UserInstallationInvalid
    return "posix_user"


def infer_home(interpreter: Interpreter) -> str:
    """Try to find a home for the current platform."""
    if "home" in interpreter.preferred:
        return interpreter.preferred["home"]
    suffixed = f"{os.name}_home"
    if suffixed in interpreter.schemes:
        return suffixed
    return "posix_home"


HOME_KEYS = (
    "installed_base",
    "base",
    "installed_platbase",
    "platbase",
    "prefix",
    "exec_prefix",
)


def get_scheme(
    dist_name: str,
    *,
    interpreter: Interpreter,
    user: bool = False,
    home: str | None = None,
    root: str | None = None,
    isolated: bool = False,
    prefix: str | None = None,
) -> Scheme:
    """Get the "scheme" corresponding to the input parameters.

    :param dist_name: the name of the package to retrieve the scheme for, used
        in the headers scheme path
    :param interpreter: the Python the scheme is that of
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

    if home is not None:
        scheme_name = infer_home(interpreter)
    elif user:
        scheme_name = infer_user(interpreter)
    else:
        scheme_name = infer_prefix(interpreter)

    if prefix is not None and scheme_name == "osx_framework_library":
        scheme_name = "posix_prefix"

    keys = HOME_KEYS
    if interpreter.config.get("userbase") is not None:
        keys = (*keys, "userbase")
    if home is not None:
        variables = dict.fromkeys(keys, home)
    elif prefix is not None:
        variables = dict.fromkeys(keys, prefix)
    else:
        variables = {}

    paths = interpreter.paths(scheme_name, variables)

    if interpreter.in_virtualenv:
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
