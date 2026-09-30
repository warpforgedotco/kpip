"""What every command's parser takes, as pip's commands do."""

from __future__ import annotations

import pytest
from kpip.cli.parser import ArgumentParser
from kpip.cli.parsers.install import create_parser as install_parser
from kpip.cli.parsers.list import create_parser as list_parser
from kpip.cli.parsers.lock import create_parser as lock_parser
from kpip.cli.parsers.wheel import create_parser as wheel_parser


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


def test_options_are_spelled_as_pip_spells_them() -> None:
    for parser in (install_parser, wheel_parser):
        options = parser().parse_args(["demo", "-C", "a=b", "--config-settings", "c"])

        assert options.config_settings == ["a=b", "c"]

    assert (
        install_parser()
        .parse_args(["demo", "--disable-pip-version-check"])
        .disable_pip_version_check
    )


GENERAL = [
    ["--isolated"],
    ["--debug"],
    ["--no-input"],
    ["--no-color"],
    ["--no-python-version-warning"],
    ["--disable-pip-version-check"],
    ["--keyring-provider", "disabled"],
    ["--proxy", "http://proxy.invalid:3128"],
    ["--retries", "2"],
    ["--resume-retries", "1"],
    ["--timeout", "7"],
    ["--default-timeout", "7"],
    ["--exists-action", "w"],
    ["--trusted-host", "host.invalid"],
    ["--cert", "/ca.pem"],
    ["--client-cert", "/client.pem"],
    ["--cache-dir", "/cache"],
    ["--no-cache-dir"],
    ["--use-feature", "fast-deps"],
    ["--use-deprecated", "legacy-resolver"],
]


@pytest.mark.parametrize(
    "parser", [install_parser, list_parser, lock_parser, wheel_parser, _parser]
)
@pytest.mark.parametrize("option", GENERAL, ids=lambda option: option[0])
def test_every_command_takes_pips_general_options(parser, option: list[str]) -> None:  # noqa: ANN001
    parser().parse_args(option)


def test_general_options_become_the_running_commands() -> None:
    from kpip.core import run_options

    _parser().parse_args(
        [
            "--proxy",
            "http://proxy.invalid:3128",
            "--retries",
            "2",
            "--timeout",
            "7",
            "--trusted-host",
            "a.invalid",
            "--trusted-host",
            "b.invalid",
            "--cert",
            "/ca.pem",
            "--no-input",
            "--exists-action",
            "w",
            "--no-cache-dir",
            "--isolated",
        ]
    )
    current = run_options.current

    assert current.proxy == "http://proxy.invalid:3128"
    assert current.retries == 2
    assert current.timeout == 7
    assert current.trusted_hosts == ("a.invalid", "b.invalid")
    assert current.cert == "/ca.pem"
    assert current.no_input
    assert current.exists_action == ("w",)
    assert current.no_cache_dir
    assert current.isolated

    _parser().parse_args([])

    assert run_options.current.proxy is None
    assert not run_options.current.isolated


def test_a_session_is_built_from_the_general_options(tmp_path) -> None:  # noqa: ANN001
    from kpip.network.deferred import DeferredNetworkSession

    _parser().parse_args(
        [
            "--proxy",
            "http://proxy.invalid:3128",
            "--retries",
            "2",
            "--timeout",
            "7",
            "--cert",
            str(tmp_path / "ca.pem"),
        ]
    )
    session = DeferredNetworkSession(cache_dir=None).materialize()

    assert session.proxies == {
        "http": "http://proxy.invalid:3128",
        "https": "http://proxy.invalid:3128",
    }
    assert session.retry.connect == 2
    assert session.timeout.connect_timeout == 7
    assert session.verify == str(tmp_path / "ca.pem")


def test_no_cache_dir_is_honoured_by_every_command() -> None:
    from kpip.core.appdirs import command_cache_dir

    _parser().parse_args(["--no-cache-dir"])

    assert command_cache_dir(None, False) is None
