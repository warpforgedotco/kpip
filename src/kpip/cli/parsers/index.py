"""Argument parser for ``kpip index``."""

from __future__ import annotations

lazy from kpip.cli.parser import ArgumentParser
lazy from kpip.cli.parsers.shared import (
    add_index_options,
    add_selection_options,
    add_target_python_options,
)


def create_parser() -> ArgumentParser:
    """Query package indexes without resolving or installing a package."""

    parser = ArgumentParser(prog="kpip index")

    parser.add_argument("command", choices=("versions",))

    parser.add_argument("package")

    parser.add_argument("--json", action="store_true")

    parser.add_argument("--ignore-requires-python", action="store_true")

    add_index_options(parser)
    add_selection_options(parser)
    add_target_python_options(parser)

    return parser
