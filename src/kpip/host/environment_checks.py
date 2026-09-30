"""Checks pip makes of the environment before it changes it."""

from __future__ import annotations

lazy import configparser
lazy import locale
lazy import logging
lazy import os
lazy import sys
lazy import sysconfig

lazy from kpip.core.errors import KpipError
lazy from kpip.host.virtualenv import running_under_virtualenv

logger = logging.getLogger(__name__)


class ExternallyManagedEnvironment(KpipError):
    """The environment belongs to another package manager (PEP 668)."""


def _default_error() -> str:
    return (
        f"The Python environment under {sys.prefix} is managed externally, and "
        "may not be\nmanipulated by the user. Please use specific tooling from "
        "the distributor of\nthe Python installation to interact with this "
        "environment instead.\n"
    )


def _error_keys() -> list[str]:
    """The keys of ``EXTERNALLY-MANAGED`` to try, the user's language first."""
    try:
        language, _ = locale.getlocale(locale.LC_MESSAGES)

    except AttributeError:
        # LC_MESSAGES is not in the C standard; Windows has none.
        language = None

    keys: list[str] = []

    if language is not None:
        keys.append(f"Error-{language}")

        for separator in ("-", "_"):
            before, found, _ = language.partition(separator)

            if found:
                keys.append(f"Error-{before}")

    keys.append("Error")

    return keys


def _marker_error(marker: str) -> str | None:
    parser = configparser.ConfigParser(interpolation=None)

    try:
        parser.read(marker, encoding="utf-8")

        section = parser["externally-managed"]

    except KeyError:
        return None

    except (OSError, UnicodeDecodeError, configparser.ParsingError):
        logger.warning("Failed to read %s", marker)

        return None

    for key in _error_keys():
        if key in section:
            return section[key]

    return None


def check_externally_managed() -> None:
    """Refuse to change an environment its distributor marks as its own.

    As pip: a virtual environment is never externally managed, and the
    marker is ``EXTERNALLY-MANAGED`` beside the standard library.
    """
    if running_under_virtualenv():
        return

    marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")

    if not os.path.isfile(marker):
        return

    context = (_marker_error(marker) or _default_error()).rstrip("\n")

    raise ExternallyManagedEnvironment(
        "externally-managed-environment\n\n"
        "× This environment is externally managed\n"
        "╰─> " + context.replace("\n", "\n    ") + "\n\n"
        "note: If you believe this is a mistake, please contact your Python "
        "installation or OS distribution provider. You can override this, at "
        "the risk of breaking your Python installation or OS, by passing "
        "--break-system-packages.\n"
        "hint: See PEP 668 for the detailed specification."
    )


def warn_if_run_as_root() -> None:
    """Warn a Unix root user who is not in a virtual environment.

    In a virtual environment root still writes to the environment; on
    Windows there are no system-managed Python packages to break.
    """
    if running_under_virtualenv():
        return

    if not hasattr(os, "getuid") or sys.platform in {"win32", "cygwin"}:
        return

    if os.getuid() != 0:
        return

    logger.warning(
        "Running kpip as the 'root' user can result in broken permissions and "
        "conflicting behaviour with the system package manager, possibly "
        "rendering your system unusable. "
        "It is recommended to use a virtual environment instead. "
        "Use the --root-user-action option if you know what you are doing and "
        "want to suppress this warning."
    )
