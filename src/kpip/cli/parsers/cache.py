"""Argument parser for ``kpip cache``."""

from __future__ import annotations

lazy from kpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip cache")

    parser.add_argument("command", choices=("dir", "info", "list", "remove", "purge"))

    parser.add_argument("pattern", nargs="?")

    parser.add_argument("--format", choices=("human", "abspath"), default="human")

    parser.add_argument("--cache-dir")

    parser.add_argument("--no-cache-dir", action="store_true")

    parser.add_argument("-v", "--verbose", action="count", default=0)

    return parser
