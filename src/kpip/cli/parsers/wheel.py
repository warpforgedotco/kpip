"""Argument parser for ``kpip wheel``."""

from __future__ import annotations

lazy import os

lazy from kpip.cli.parser import ArgumentParser
lazy from kpip.cli.parsers.shared import (
    add_config_settings_option,
    add_editable_option,
    add_index_options,
    add_requirement_options,
    add_selection_options,
)


def create_parser() -> ArgumentParser:
    """Build wheels for requirements without installing them."""
    parser = ArgumentParser(prog="kpip wheel")
    add_requirement_options(parser)
    add_editable_option(parser)
    add_config_settings_option(parser)
    add_index_options(parser)
    add_selection_options(parser)
    parser.add_argument("-w", "--wheel-dir", default=os.curdir)
    # Accepted as pip accepts it: a wheel built here is not verified, so
    # there is nothing for it to turn off.
    parser.add_argument("--no-verify", action="store_true")
    return parser
