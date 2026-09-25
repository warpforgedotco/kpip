"""``parse_lock_options`` reads a lock's arguments as argparse would."""

from __future__ import annotations

import random

import pytest
from kpip.cli.parsers.lock import create_parser, parse_lock_options

WORDS = [
    "requests",
    "rich>=13",
    "-",
    "-r",
    "req.txt",
    "--requirement",
    "--requirement=req.txt",
    "-rreq.txt",
    "-c",
    "c.txt",
    "--constraint=c.txt",
    "-e",
    ".",
    "-f",
    "links/",
    "--find-links",
    "--no-index",
    "--no-binary",
    ":all:",
    "--no-build-isolation",
    "--quiet",
    "--python-version",
    "3.9",
    "--python-version=3.12",
    "--output",
    "out.toml",
    "--output=",
    "-U",
    "--upgrade",
    "-P",
    "rich",
    "--upgrade-package=idna",
    "--cache-dir",
    "cache/",
    "--no-cache-dir",
    "--refresh",
    "--",
    "--bogus",
    "-x",
    "--out",
]


def outcome(parse: object, args: list[str]) -> object:
    try:
        return vars(parse(args))  # ty: ignore[call-non-callable]
    except SystemExit as error:
        return ("exit", error.code)


def test_common_arguments_parse_without_argparse() -> None:
    import sys

    sys.modules.pop("argparse", None)
    options = parse_lock_options(
        ["-r", "req.txt", "rich", "--output", "out.toml", "-U", "-P", "idna"]
    )
    assert "argparse" not in sys.modules
    assert options.requirement == ["req.txt"]
    assert options.requirements == ["rich"]
    assert options.upgrade_packages == ["idna"]


@pytest.mark.parametrize("seed", range(20))
def test_random_arguments_parse_as_argparse_does(seed: int, capsys: object) -> None:
    chooser = random.Random(seed)
    for _ in range(100):
        args = [chooser.choice(WORDS) for _ in range(chooser.randint(0, 7))]
        expected = outcome(lambda a: create_parser().parse_args(a), args)
        assert outcome(parse_lock_options, args) == expected, args
