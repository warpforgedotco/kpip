"""Narrow command fast paths and the argv shapes they recognize.

Each module here handles one deliberately small command shape: ``install``
(``fast_install``). A fast path is a
conservative recognizer, never separate command semantics; it returns ``None``
when an argument, target state, source shape, or feature falls outside its
subset, and normal command dispatch stays available after every decline.

This module must stay import-light. It is loaded on every startup, so the
heavier CLI dependencies are imported only once their shape matches.
"""

from __future__ import annotations


import os


REMOTE_EXACT_OPTIONS = ("--ignore-installed", "--no-compile", "--target")
LOCAL_WHEELHOUSE_OPTIONS = (
    "--no-index",
    "--ignore-installed",
    "--no-compile",
    "--target",
)
LOCAL_UPGRADE_OPTIONS = ("--no-index", "--upgrade", "--no-compile", "--target")
LOCAL_INSTALL_OPTIONS = ("--no-index", "--no-compile", "--target")


def option_value(args: list[str], index: int) -> str | None:
    """Return a following option value, or ``None`` for an invalid option."""
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    return None if value.startswith("-") else value


def consume_option(
    args: list[str],
    index: int,
    names: tuple[str, ...],
) -> tuple[str, str, int] | None:
    """Consume a value option in either separated or ``--name=value`` form."""
    token = args[index]
    for name in names:
        if token == name:
            value = option_value(args, index)
            return None if value is None else (name, value, index + 2)
        prefix = name + "="
        if token.startswith(prefix):
            value = token[len(prefix) :]
            return None if not value else (name, value, index + 1)
    return None


def read_requirements(path: str) -> list[str] | None:
    """Read simple requirement files without importing the full parser."""
    try:
        with open(path, encoding="utf-8") as requirement_file:
            return [
                line.strip()
                for line in requirement_file.read().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        return None


def extend_requirements(
    target: list[str],
    path: str,
    *,
    reject_pylock: bool = False,
) -> bool:
    """Read a requirement file and append its entries to ``target``."""
    if (
        reject_pylock
        and os.path.basename(path).startswith("pylock")
        and path.endswith(".toml")
    ):
        return False
    value = read_requirements(path)
    if value is None:
        return False
    target.extend(value)
    return True


def _has_all(options: list[str], names: tuple[str, ...]) -> bool:
    return all(name in options for name in names)


def suppresses_logging(args: list[str], *, log_file: str | None) -> bool:
    """Whether ``args`` names a quiet fast-path shape that must not log."""
    if not args or log_file is not None or "--quiet" not in args:
        return False

    if args[0] != "install":
        return False

    options = args[1:]
    return _has_all(options, LOCAL_INSTALL_OPTIONS) and (
        "--ignore-installed" in options or "--upgrade" in options
    )


def run_before_startup(args: list[str]) -> tuple[int | None, bool]:
    """Try the fast paths that run before CLI initialization."""
    if not args:
        return None, False

    command = args[0]
    options = args[1:]

    if command != "install":
        return None, False

    if (
        "--quiet" in options
        and "--no-index" not in options
        and _has_all(options, REMOTE_EXACT_OPTIONS)
    ):
        from kpip.cli import fast_install

        return fast_install.run_cached_remote(options), False

    if (
        "--quiet" in options
        and "--ignore-installed" not in options
        and _has_all(options, LOCAL_UPGRADE_OPTIONS)
    ):
        from kpip.cli import fast_install

        return fast_install.run_local_fallback(options), True

    if _has_all(options, LOCAL_WHEELHOUSE_OPTIONS):
        from kpip.cli import fast_install

        status = fast_install.run(options)
        if status is not None:
            return status, True
        return fast_install.run_local_fallback(options), True

    return None, False


def run_install_after_startup(args: list[str]) -> int | None:
    """Try the local install fast path once logging has been configured."""
    if not args or args[0] != "install":
        return None

    options = args[1:]

    if not _has_all(options, LOCAL_WHEELHOUSE_OPTIONS):
        return None

    from kpip.cli import fast_install

    return fast_install.run(options)
