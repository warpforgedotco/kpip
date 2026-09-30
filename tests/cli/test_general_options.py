"""What every command's parser takes, as pip's commands do."""

from __future__ import annotations

import pytest
from kpip.cli.parser import ArgumentParser
from kpip.cli.parsers.install import create_parser as install_parser
from kpip.cli.parsers.list import create_parser as list_parser
from kpip.cli.parsers.lock import create_parser as lock_parser


def _parser() -> ArgumentParser:
    parser = ArgumentParser(prog="kpip demo")
    parser.add_argument("--flag", action="store_true")
    parser.add_argument("--value")
    parser.add_argument("names", nargs="*")
    return parser


def test_positionals_may_come_before_between_and_after_options() -> None:
    options = _parser().parse_args(["a", "--flag", "b", "--value", "v", "c"])

    assert options.names == ["a", "b", "c"]
    assert options.flag
    assert options.value == "v"


def test_every_command_takes_quiet_and_verbose() -> None:
    options = _parser().parse_args(["-q", "a", "-vv", "--quiet"])

    assert options.quiet == 2
    assert options.verbose == 2
    assert options.names == ["a"]


def test_a_command_with_its_own_quiet_keeps_it() -> None:
    assert lock_parser().parse_args(["-q", "demo"]).quiet is True
    assert install_parser().parse_args(["demo", "-q", "-q"]).quiet == 2
    assert list_parser().parse_args(["-q"]).quiet == 1


def test_an_unknown_option_is_still_an_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        _parser().parse_args(["a", "--nope"])

    assert raised.value.code == 2
    assert "no such option: --nope" in capsys.readouterr().err
