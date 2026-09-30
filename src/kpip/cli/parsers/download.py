"""Argument parser for ``kpip download``."""

from __future__ import annotations

import os

from kpip.cli.parser import ArgumentParser
from kpip.cli.parsers.shared import (
    add_index_options,
    add_requirement_options,
    add_selection_options,
    add_target_python_options,
)


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip download")
    add_requirement_options(parser)
    add_index_options(parser)
    add_selection_options(parser)
    add_target_python_options(parser)
    parser.add_argument(
        "-d",
        "--dest",
        "--destination-dir",
        "--destination-directory",
        dest="dest",
        default=os.curdir,
    )
    return parser
