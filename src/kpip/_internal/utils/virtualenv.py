from __future__ import annotations

import logging
import os
import re

from kpip._internal.interpreters import target_interpreter

logger = logging.getLogger(__name__)
_INCLUDE_SYSTEM_SITE_PACKAGES_REGEX = re.compile(
    r"include-system-site-packages\s*=\s*(?P<value>true|false)"
)


def running_under_virtualenv() -> bool:
    """Checks if the target interpreter's base_prefix and prefix match.

    This handles PEP 405 compliant virtual environments.
    """
    interpreter = target_interpreter()
    return interpreter.prefix != interpreter.base_prefix


def _get_pyvenv_cfg_lines() -> list[str] | None:
    """Reads {prefix}/pyvenv.cfg of the target interpreter and returns its
    contents as list of lines

    Returns None, if it could not read/access the file.
    """
    pyvenv_cfg_file = os.path.join(target_interpreter().prefix, "pyvenv.cfg")
    try:
        # Although PEP 405 does not specify, the built-in venv module always
        # writes with UTF-8. (pypa/pip#8717)
        with open(pyvenv_cfg_file, encoding="utf-8") as f:
            return f.read().splitlines()  # avoids trailing newlines
    except OSError:
        return None


def _no_global_under_venv() -> bool:
    """Check `{sys.prefix}/pyvenv.cfg` for system site-packages inclusion

    PEP 405 specifies that when system site-packages are not supposed to be
    visible from a virtual environment, `pyvenv.cfg` must contain the following
    line:

        include-system-site-packages = false

    Additionally, log a warning if accessing the file fails.
    """
    cfg_lines = _get_pyvenv_cfg_lines()
    if cfg_lines is None:
        # We're not in a "sane" venv, so assume there is no system
        # site-packages access (since that's PEP 405's default state).
        logger.warning(
            "Could not access 'pyvenv.cfg' despite a virtual environment "
            "being active. Assuming global site-packages is not accessible "
            "in this environment."
        )
        return True

    for line in cfg_lines:
        match = _INCLUDE_SYSTEM_SITE_PACKAGES_REGEX.match(line)
        if match is not None and match.group("value") == "false":
            return True
    return False


def virtualenv_no_global() -> bool:
    """Returns a boolean, whether running in venv with no system site-packages."""
    if running_under_virtualenv():
        return _no_global_under_venv()

    return False
