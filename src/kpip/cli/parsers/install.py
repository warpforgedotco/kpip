"""Argument parser for ``kpip install``."""

from __future__ import annotations

from kpip.cli.parser import ArgumentParser
from kpip.cli.parsers.shared import (
    add_config_settings_option,
    add_editable_option,
    add_externally_managed_options,
    add_index_options,
    add_requirement_options,
    add_selection_options,
    add_target_python_options,
)


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip install", allow_abbrev=False)
    add_requirement_options(parser)
    add_editable_option(parser)
    add_config_settings_option(parser)
    add_index_options(parser)
    add_selection_options(parser)
    add_target_python_options(parser)
    add_externally_managed_options(parser)
    parser.add_argument(
        "--trusted-host",
        dest="trusted_hosts",
        action="append",
        default=[],
    )
    parser.add_argument("--cert")
    parser.add_argument("--client-cert")
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument(
        "--keyring-provider",
        choices=("auto", "disabled", "import", "subprocess"),
        default="auto",
    )
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--use-deprecated", action="append", default=[])
    parser.add_argument(
        "--use-feature",
        dest="use_features",
        action="append",
        default=[],
    )
    parser.add_argument("--disable-pip-version-check", action="store_true")
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--no-compile", action="store_true")
    parser.add_argument("-U", "--upgrade", action="store_true")
    parser.add_argument(
        "--upgrade-strategy",
        choices=("only-if-needed", "eager"),
        default="only-if-needed",
    )
    parser.add_argument("-I", "--ignore-installed", action="store_true")
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--no-user", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--prefix")
    parser.add_argument("-t", "--target")
    parser.add_argument("--cache-dir")
    parser.add_argument("--no-cache-dir", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="count", default=0)
    parser.add_argument("--no-warn-script-location", action="store_true")
    parser.add_argument("--no-warn-conflicts", action="store_true")
    return parser
