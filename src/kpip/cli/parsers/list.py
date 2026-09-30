"""Argument parser for ``kpip list``."""

from __future__ import annotations

from kpip.cli.parser import ArgumentParser
from kpip.cli.parsers.shared import add_index_options, add_selection_options


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip list")

    parser.add_argument("-o", "--outdated", action="store_true")

    parser.add_argument("-u", "--uptodate", action="store_true")

    parser.add_argument("-e", "--editable", action="store_true")

    parser.add_argument("-l", "--local", action="store_true")

    parser.add_argument("--user", action="store_true")

    parser.add_argument("--path", action="append", default=[])

    parser.add_argument("--not-required", action="store_true")

    parser.add_argument("--exclude", action="append", default=[])

    add_index_options(parser)

    add_selection_options(parser)

    parser.add_argument(
        "--exclude-editable",
        action="store_false",
        dest="include_editable",
        default=True,
    )

    parser.add_argument(
        "--include-editable",
        action="store_true",
        dest="include_editable",
    )

    parser.add_argument("-v", "--verbose", action="count", default=0)

    parser.add_argument(
        "--format",
        choices=("columns", "json", "freeze"),
        default="columns",
    )

    return parser
