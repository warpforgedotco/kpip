"""Argument parsers for ``kpip check``, ``hash``, ``show``, and ``inspect``."""

from __future__ import annotations

from kpip.cli.parser import ArgumentParser


def create_check_parser() -> ArgumentParser:
    return ArgumentParser(prog="kpip check")


def create_hash_parser() -> ArgumentParser:
    import hashlib

    parser = ArgumentParser(prog="kpip hash")
    parser.add_argument("files", nargs="+")
    parser.add_argument(
        "-a",
        "--algorithm",
        default="sha256",
        choices=sorted(hashlib.algorithms_available),
    )
    return parser


def create_show_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip show")
    parser.add_argument("-f", "--files", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("packages", nargs="*")
    return parser


def create_inspect_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip inspect")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    return parser
