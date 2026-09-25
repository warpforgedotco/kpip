"""Argument parser for ``kpip lock``.

Kept apart from the command module so that ``kpip lock --help`` builds a
parser without loading the machinery that runs the command.
"""

from __future__ import annotations

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from kpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    """Resolve requirements and write a PEP 751 ``pylock.toml`` file."""

    from kpip.cli.parser import ArgumentParser

    parser = ArgumentParser(prog="kpip lock", allow_abbrev=False)

    parser.add_argument("requirements", nargs="*")

    parser.add_argument("-e", "--editable", action="append", default=[])

    parser.add_argument("-r", "--requirement", action="append", default=[])

    parser.add_argument(
        "-c",
        "--constraint",
        dest="constraints",
        metavar="CONSTRAINT",
        action="append",
        default=[],
    )

    parser.add_argument("-f", "--find-links", action="append", default=[])

    parser.add_argument("--no-index", action="store_true")

    parser.add_argument("--no-binary", action="append", default=[])

    parser.add_argument("--no-build-isolation", action="store_true")

    parser.add_argument("--quiet", action="store_true")

    parser.add_argument("--python-version", metavar="PYTHON_VERSION")

    parser.add_argument("--output", default="pylock.toml")

    # The lock already at --output is where this one starts: each package
    # keeps its version there while that still satisfies the requirements.
    parser.add_argument("-U", "--upgrade", action="store_true")

    parser.add_argument(
        "-P",
        "--upgrade-package",
        dest="upgrade_packages",
        metavar="PACKAGE",
        action="append",
        default=[],
    )

    parser.add_argument("--cache-dir")

    parser.add_argument("--no-cache-dir", action="store_true")
    parser.add_argument("--refresh", action="store_true")

    return parser


_FLAGS = {
    "--no-index": "no_index",
    "--no-build-isolation": "no_build_isolation",
    "--quiet": "quiet",
    "-U": "upgrade",
    "--upgrade": "upgrade",
    "--no-cache-dir": "no_cache_dir",
    "--refresh": "refresh",
}
_APPENDED = {
    "-e": "editable",
    "--editable": "editable",
    "-r": "requirement",
    "--requirement": "requirement",
    "-c": "constraints",
    "--constraint": "constraints",
    "-f": "find_links",
    "--find-links": "find_links",
    "--no-binary": "no_binary",
    "-P": "upgrade_packages",
    "--upgrade-package": "upgrade_packages",
}
_STORED = {
    "--python-version": "python_version",
    "--output": "output",
    "--cache-dir": "cache_dir",
}


def parse_lock_options(args: list[str]) -> Any:
    """What ``create_parser().parse_args(args)`` gives, mostly without argparse.

    Importing ``argparse`` costs a lock about 3 ms, more than a replayed lock
    takes altogether. The options above, spelled ``--name value``,
    ``--name=value`` or ``-xvalue``, and positional requirements are read
    here; anything else -- help, ``--``, an unknown option, a missing value
    or one that looks like an option -- goes to argparse, which reports it as
    it always has.
    """
    from types import SimpleNamespace

    values: dict[str, Any] = {
        "requirements": [],
        "editable": [],
        "requirement": [],
        "constraints": [],
        "find_links": [],
        "no_index": False,
        "no_binary": [],
        "no_build_isolation": False,
        "quiet": False,
        "python_version": None,
        "output": "pylock.toml",
        "upgrade": False,
        "upgrade_packages": [],
        "cache_dir": None,
        "no_cache_dir": False,
        "refresh": False,
    }
    # argparse takes the positional requirements as one run: a second run,
    # after an option, is an error it reports.
    positionals_ended = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if not token.startswith("-") or token == "-":
            if positionals_ended:
                return create_parser().parse_args(args)
            values["requirements"].append(token)
            continue
        positionals_ended = bool(values["requirements"])
        flag = _FLAGS.get(token)
        if flag is not None:
            values[flag] = True
            continue
        name, equals, value = token.partition("=")
        if not equals and not token.startswith("--") and len(token) > 2:
            name, value, equals = token[:2], token[2:], "="
        destination = _APPENDED.get(name) or _STORED.get(name)
        if destination is None:
            return create_parser().parse_args(args)
        if not equals:
            if index == len(args):
                return create_parser().parse_args(args)
            value = args[index]
            index += 1
        if not value or value.startswith("-"):
            return create_parser().parse_args(args)
        if name in _APPENDED:
            values[destination].append(value)
        else:
            values[destination] = value
    return SimpleNamespace(**values)
