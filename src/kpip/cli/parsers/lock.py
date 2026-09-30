"""Argument parser for ``kpip lock``."""

from __future__ import annotations

lazy from kpip.cli.parser import ArgumentParser
lazy from kpip.cli.parsers.shared import (
    add_config_settings_option,
    add_editable_option,
    add_index_options,
    add_requirement_options,
    add_selection_options,
)


def create_parser() -> ArgumentParser:
    """Resolve requirements and write a PEP 751 ``pylock.toml`` file."""
    parser = ArgumentParser(prog="kpip lock", allow_abbrev=False)
    add_requirement_options(parser)
    add_editable_option(parser)
    add_config_settings_option(parser)
    add_index_options(parser)
    add_selection_options(parser)

    parser.add_argument("-q", "--quiet", action="store_true")

    parser.add_argument("-o", "--output", default="pylock.toml")

    # pip's lock is always for the running interpreter; this names another.
    parser.add_argument("--python-version", metavar="PYTHON_VERSION")

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
