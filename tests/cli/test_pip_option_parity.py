"""Every option a pip command takes, the kpip command of that name takes.

The pip compared against is the one installed for the tests. An option pip
gains shows up here as a failure, to be declared in ``kpip.cli.parsers`` or
listed below with the reason it is left out.
"""

from __future__ import annotations

import pytest
from kpip.cli.general_options import add_general_options
from kpip.cli.registry import get_command
from pip._internal.commands import create_command

COMMANDS = (
    "install",
    "download",
    "wheel",
    "lock",
    "index",
    "list",
    "freeze",
    "uninstall",
    "show",
    "check",
    "hash",
    "inspect",
    "cache",
)

# Read from the command line by ``cli.entrypoint`` before a command's parser
# sees it, so no parser declares them.
READ_BEFORE_DISPATCH = frozenset(
    (
        "--python",
        "--require-virtualenv",
        "--require-venv",
        "--log",
        "--log-file",
        "--local-log",
        "-V",
        "--version",
    )
)

# pip options kpip does not take, each with why.
LEFT_OUT = {
    "--no-proxy-env": (
        "a general option, which belongs to cli.general_options; nothing in "
        "kpip can yet be told to ignore the proxy environment"
    ),
}

# Options kpip adds to a pip command.
KPIP_ONLY = {
    # --system: uv's guard against changing a Python nobody chose.
    "install": frozenset(("--refresh", "--system")),
    "uninstall": frozenset(("--system",)),
    "lock": frozenset(
        (
            "-U",
            "--upgrade",
            "-P",
            "--upgrade-package",
            "--python-version",
            "--refresh",
        )
    ),
}


def pip_options(command: str) -> set[str]:
    parser = create_command(command).parser
    return {
        string
        for option in parser.option_list_all
        for string in (*option._long_opts, *option._short_opts)
    }


def kpip_options(command: str) -> set[str]:
    spec = get_command(command)
    assert spec is not None
    parser = spec.create_parser()
    add_general_options(parser)
    return set(parser._option_string_actions)


@pytest.mark.parametrize("command", COMMANDS)
def test_a_command_takes_every_option_pip_gives_it(command: str) -> None:
    missing = (
        pip_options(command)
        - kpip_options(command)
        - READ_BEFORE_DISPATCH
        - LEFT_OUT.keys()
    )

    assert not missing, f"kpip {command} lacks {' '.join(sorted(missing))}"


@pytest.mark.parametrize("command", COMMANDS)
def test_a_command_takes_nothing_pip_does_not_but_what_is_listed(
    command: str,
) -> None:
    extra = (
        kpip_options(command)
        - pip_options(command)
        - KPIP_ONLY.get(command, frozenset())
    )

    assert not extra, f"kpip {command} adds {' '.join(sorted(extra))}"


def test_the_lists_name_only_what_they_excuse() -> None:
    """An entry that excuses nothing has outlived its reason."""
    every_pip_option = set().union(*(pip_options(command) for command in COMMANDS))

    assert LEFT_OUT.keys() <= every_pip_option
    assert READ_BEFORE_DISPATCH <= every_pip_option

    for command, options in KPIP_ONLY.items():
        assert options <= kpip_options(command) - pip_options(command)
