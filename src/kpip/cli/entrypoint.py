"""Canonical, dependency-light process entrypoint for kpip."""

from __future__ import annotations

import os
import sys

from kpip.cli.exit_codes import BROKEN_STDOUT, VIRTUALENV_NOT_FOUND
from kpip.cli.registry import COMMAND_SPECS, CommandSpec, get_command

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import NoReturn

_SWITCH_INTERVAL_SECONDS = 0.020
"""How long a thread may hold the interpreter lock before offering it up."""

_OLD_GENERATION_RATIO = 100
"""Generation-0 collections per generation-1 one, and the same again for 2."""

_YOUNG_GENERATION_THRESHOLD = 10_000
"""Allocations between generation-0 collections, at the least."""

_UNCOLLECTED_COMMANDS = frozenset({"lock"})
"""Commands that run with garbage collection off, see :func:`pause_collection`."""

_FLUSH_FAILED = 120
"""The status CPython exits with when it cannot flush a standard stream."""

VISIBLE_COMMAND_NAMES = tuple(spec.name for spec in COMMAND_SPECS if spec.visible)
COMMAND_NAMES = frozenset(spec.name for spec in COMMAND_SPECS)

VIRTUALENV_OPTIONS = frozenset(("--require-virtualenv", "--require-venv"))


VERBOSITY_FLAGS = frozenset(("-vv", "-vvv"))

VERSION_FLAGS = frozenset(("-V", "--version"))

HELP_FLAGS = frozenset(("-h", "--help"))


def extract_python_option(args: list[str]) -> tuple[list[str], str | None]:
    filtered: list[str] = []

    target_prefix: str | None = None

    index = 0

    while index < len(args):
        token = args[index]

        if token in COMMAND_NAMES:
            filtered.extend(args[index:])

            break

        if token == "--python":
            if index + 1 >= len(args):
                raise ValueError("--python requires a path")

            target_prefix = args[index + 1]

            index += 2

            continue

        if token.startswith("--python="):
            target_prefix = token.partition("=")[2]

            index += 1

            continue

        filtered.append(token)

        index += 1

    return filtered, target_prefix


def extract_global_options(
    args: list[str],
) -> tuple[list[str], int, bool, str | None]:
    filtered: list[str] = []

    log_file: str | None = None

    index = 0

    while index < len(args):
        token = args[index]

        if token == "--log":
            if index + 1 < len(args):
                log_file = args[index + 1]

            index += 2

            continue

        if token.startswith("--log="):
            log_file = token.partition("=")[2]

            index += 1

            continue

        filtered.append(token)

        index += 1

    result: list[str] = []

    verbosity = 0

    require_virtualenv = False

    index = 0

    while index < len(filtered):
        token = filtered[index]

        if token in VIRTUALENV_OPTIONS:
            require_virtualenv = True

            index += 1

            continue

        if token == "--verbose":
            verbosity += 1

            index += 1

            continue

        if token.startswith("-") and set(token[1:]) == {"v"}:
            verbosity += len(token) - 1

            index += 1

            continue

        result.extend(filtered[index:])

        break

    return result, verbosity, require_virtualenv, log_file


def print_help() -> None:
    print("Usage:")

    print("  kpip <command> [options]")

    print()

    print("Commands:")

    for command in VISIBLE_COMMAND_NAMES:
        print(f"  {command}")


def print_version(version: str | None, location: str | None) -> None:
    if version is None:
        from kpip import __version__

        version = __version__

    if location is None:
        location = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    print(
        f"kpip {version} from {os.path.realpath(location)} (python {python_version})",
    )


def print_command_help(command: str) -> int | None:
    """Print a command's own help, or return ``None`` for a non-command.

    ``help`` itself is excluded: it has no parser of its own, and reporting it
    as unknown is what ``kpip help help`` has always done.
    """

    spec = get_command(command)

    if spec is None or spec.name == "help":
        return None

    spec.create_parser().print_help()

    return 0


def run_help(args: list[str]) -> int:
    """Handle the ``kpip help [command]`` subcommand."""

    if not args or args == ["--help"]:
        print_help()

        return 0

    status = print_command_help(args[0])

    if status is None:
        print(f"ERROR: Unknown command: {args[0]}", file=sys.stderr)

        return 1

    return status


def handle_global_commands(
    argv: list[str],
    *,
    require_virtualenv: bool,
    version: str | None,
    location: str | None,
) -> int | None:
    """Handle help, version, the virtualenv gate, and unknown command names.

    Returns the process status, or ``None`` when ``argv`` names a real command
    and dispatch should continue.  The order matches what the fallback
    dispatcher used to do: help and version answer before the virtualenv gate,
    so ``kpip --require-virtualenv --help`` still works outside a virtualenv.
    """

    if not argv or argv[0] in HELP_FLAGS:
        print_help()

        return 0

    if argv[0] == "help":
        return run_help(argv[1:])

    if argv[0] in VERSION_FLAGS:
        print_version(version, location)

        return 0

    if require_virtualenv:
        from kpip.host.virtualenv import running_under_virtualenv

        if not running_under_virtualenv():
            print(
                "Could not find an activated virtualenv (required).",
                file=sys.stderr,
            )

            return VIRTUALENV_NOT_FOUND

    if argv[0] not in COMMAND_NAMES:
        print(f"ERROR: Unknown command: {argv[0]}", file=sys.stderr)

        return 1

    return None


def run_command(argv: list[str], spec: CommandSpec) -> int:
    """Run a resolved command, giving the lock fast path its last chance."""

    from kpip.cli import fast

    status = fast.run_lock_after_startup(argv)

    if status is not None:
        return status

    runner = spec.load_runner()

    if runner is None:
        raise AssertionError(f"unhandled command: {spec.name}")

    return runner(argv[1:])


def flush_streams() -> None:
    sys.stdout.flush()

    sys.stderr.flush()


def switch_threads_less_often() -> float | None:
    """Hand the interpreter lock over less often for the length of one command.

    A resolve runs dozens of fetch workers against one interpreter lock while
    the main thread does the catalog and resolver work.  CPython offers the
    lock up every 5 ms by default, which suits a program where a prompt
    handover matters; here it only multiplies handovers, and each one is a
    pair of system calls and a scheduler round trip that buys nothing because
    the waiting threads are mostly blocked on sockets anyway.

    Raising it to 20 ms cut a cold airflow lock's system time by a fifth and
    its wall time by roughly 6% on a four-core machine and 9% on a two-core
    one, with identical output.  Returns the interval it replaced, or None if
    it changed nothing; ``KPIP_SWITCH_INTERVAL=default`` leaves CPython's.
    """
    if os.environ.get("KPIP_SWITCH_INTERVAL") == "default":
        return None

    previous = sys.getswitchinterval()

    sys.setswitchinterval(_SWITCH_INTERVAL_SECONDS)

    return previous


def pause_collection(command: str) -> bool:
    """Turn garbage collection off for a command that gains nothing from it.

    A lock's heap is the catalog and the resolver's clause set, alive until
    the command ends, and collecting it reclaims nothing: peak memory of the
    airflow, bio-embeddings, pydantic and backtracking locks is the same to
    the megabyte with collection off. Collecting only walks it, which with
    the thresholds of :func:`collect_less_often` still cost a warm airflow
    lock some 10% compiled. Commands that install stay collected: they run
    build backends and hold on to archives, where cycles can matter.

    Returns whether it turned collection off, for ``main`` to turn it back
    on for an in-process caller. ``KPIP_GC=default`` leaves it on.
    """
    import gc

    if (
        command not in _UNCOLLECTED_COMMANDS
        or os.environ.get("KPIP_GC") == "default"
        or not gc.isenabled()
    ):
        return False

    gc.disable()

    return True


def collect_less_often() -> tuple[int, int, int] | None:
    """Make garbage collections rare for the length of one command.

    CPython's thresholds assume a small live heap.  A resolve keeps the whole
    index catalog and the resolver's clause set alive and allocates millions
    of short-lived tuples through it, so the stock thresholds (``(2000, 10,
    10)`` on 3.14, ``(700, 10, 10)`` before) spend 16% of a warm airflow
    lock collecting.  They reclaim almost nothing -- a few hundred objects a
    run -- because what is alive is alive for the rest of the run.

    The generation-1 and generation-2 multipliers make full traversals of
    that heap rare, and generation 0 waits for at least 10,000 allocations
    rather than 2,000: it was still running some 360 times per airflow lock.
    Together that takes collection from 16% to 5% of the lock, and peak
    memory is unchanged.  Nothing is disabled -- a cycle is still reclaimed,
    only later, and a resolve large enough to need a full traversal still
    gets one.  A generation-0 threshold set higher, or to 0, is kept.
    ``KPIP_GC=default`` restores CPython's own settings.

    Returns the thresholds it replaced, or None if it changed nothing.  A
    command normally runs in a process that is about to exit, but ``main``
    is importable and is called in-process by tests and by anything
    embedding kpip, and collection thresholds are interpreter-wide: they
    are restored when the command finishes.
    """
    if os.environ.get("KPIP_GC") == "default":
        return None

    import gc

    previous = gc.get_threshold()

    young = previous[0] and max(previous[0], _YOUNG_GENERATION_THRESHOLD)

    gc.set_threshold(young, _OLD_GENERATION_RATIO, _OLD_GENERATION_RATIO)

    return previous


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
    keep_collection_paused: bool = False,
) -> int:
    verbosity = 0

    restore_thresholds: tuple[int, int, int] | None = None

    restore_switch_interval: float | None = None

    collection_paused = False

    managed_environment = {
        name: os.environ.get(name)
        for name in ("KPIP_RESOLVER_DEBUG", "KPIP_TARGET_PREFIX")
    }
    try:
        argv = list(sys.argv[1:] if args is None else args)
        argv, verbosity, require_virtualenv, log_file = extract_global_options(argv)
        if verbosity >= 2 or any(token in VERBOSITY_FLAGS for token in argv):
            os.environ["KPIP_RESOLVER_DEBUG"] = "1"
        argv, target_prefix = extract_python_option(argv)
        if target_prefix is not None:
            os.environ["KPIP_TARGET_PREFIX"] = target_prefix

        if (
            not require_virtualenv
            and log_file is None
            and verbosity == 0
            and len(argv) > 1
            and argv[0] not in HELP_FLAGS
            and any(token in HELP_FLAGS for token in argv[1:])
        ):
            status = print_command_help(argv[0])

            if status is not None:
                flush_streams()

                return status

        status = handle_global_commands(
            argv,
            require_virtualenv=require_virtualenv,
            version=version,
            location=location,
        )

        if status is not None:
            flush_streams()

            return status

        from kpip.cli import fast

        status, fast_install_attempted = fast.run_before_startup(argv)

        if status is not None:
            flush_streams()

            return status

        quiet_fast_command = fast.suppresses_logging(argv, log_file=log_file)

        spec = get_command(argv[0])

        if spec is None:
            raise AssertionError(f"unhandled command: {argv[0]}")

        if version is not None and (
            spec.needs_execution_context and spec.needs_tempdir
        ):
            from kpip.core.utils import configure

            configure(version=version)

        if (
            spec.needs_logging
            and not quiet_fast_command
            and not os.environ.get("KPIP_QUIET")
        ):
            from kpip.cli.logging_config import configure_logging

            configure_logging(log_file)

        if not fast_install_attempted:
            status = fast.run_install_after_startup(argv)

            if status is not None:
                flush_streams()

                return status

        restore_thresholds = collect_less_often()

        collection_paused = pause_collection(spec.name)

        restore_switch_interval = switch_threads_less_often()

        if spec.needs_tempdir:
            from kpip.core.temp_dir import global_tempdir_manager

            with global_tempdir_manager():
                status = run_command(argv, spec)

        else:
            status = run_command(argv, spec)

        flush_streams()

        return status

    except OSError as exc:
        import errno

        if not isinstance(exc, BrokenPipeError) and exc.errno not in {
            errno.EINVAL,
            errno.EBADF,
        }:
            raise

        try:
            devnull = os.open(os.devnull, os.O_WRONLY)

            os.dup2(devnull, sys.stdout.fileno())

            os.close(devnull)

        except OSError:
            pass

        print("ERROR: Pipe to stdout was broken", file=sys.stderr)

        if verbosity > 0:
            import traceback

            from kpip.cli.logging_config import BrokenStdoutLoggingError

            try:
                raise BrokenStdoutLoggingError() from exc

            except BrokenStdoutLoggingError:
                traceback.print_exc(file=sys.stderr)

        return BROKEN_STDOUT

    except KeyboardInterrupt:
        print("ERROR: Operation cancelled by user", file=sys.stderr)

        return 1

    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)

        return 1

    except Exception as exc:
        from kpip.core.errors import KpipError

        if not isinstance(exc, KpipError):
            raise

        print(f"ERROR: {exc}", file=sys.stderr)

        return 1

    finally:
        for name, previous in managed_environment.items():
            if previous is None:
                os.environ.pop(name, None)

            else:
                os.environ[name] = previous

        if restore_thresholds is not None:
            import gc

            gc.set_threshold(*restore_thresholds)

        # Turning collection back on schedules one over everything the command
        # allocated, which a process that exits next would only throw away.
        if collection_paused and not keep_collection_paused:
            import gc

            gc.enable()

        if restore_switch_interval is not None:
            sys.setswitchinterval(restore_switch_interval)


def exit_without_teardown(status: int) -> NoReturn:
    """End the process after a command, without tearing the interpreter down.

    Teardown frees every object one at a time and runs full collections over
    what is left, which after a resolve is the whole catalog and clause set:
    a fifth of a warm airflow lock went there, after the lock file was
    written. The operating system takes the memory back at once instead.

    What a normal exit does that anything can observe still happens, in the
    order CPython does it: non-daemon threads and executor workers are waited
    for, the atexit callbacks run (logging's flush among them), and the
    standard streams are flushed, exiting with 120 if one cannot be, as
    CPython does. Files a command left open are not flushed, so commands
    close what they write. ``KPIP_EXIT=full`` exits normally, e.g. for a leak
    checker.
    """
    if os.environ.get("KPIP_EXIT") == "full":
        sys.exit(status)

    import atexit
    import threading

    # Private, but it is what interpreter shutdown calls: it joins non-daemon
    # threads and runs the callbacks concurrent.futures registers there.
    shutdown = getattr(threading, "_shutdown", None)
    if shutdown is not None:
        shutdown()

    atexit._run_exitfuncs()  # noqa: SLF001

    for stream in (sys.stdout, sys.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.flush()
        except (OSError, ValueError):
            status = _FLUSH_FAILED

    os._exit(status)


def console_main(
    *,
    version: str | None = None,
    location: str | None = None,
) -> NoReturn:
    """The ``kpip`` command: run :func:`main`, then end the process."""
    exit_without_teardown(
        main(version=version, location=location, keep_collection_paused=True)
    )
