"""--refresh: nothing stored before the command started is fresh."""

from __future__ import annotations

import pytest
from kpip.cli.parsers.install import create_parser as install_parser
from kpip.cli.parsers.lock import create_parser as lock_parser
from kpip.core.expiry import expiry_is_fresh, refresh_since


def test_a_response_stored_before_the_refresh_is_stale() -> None:
    assert expiry_is_fresh(2000.0, 900.0, 1000.0)

    refresh_since(950.0)

    assert not expiry_is_fresh(2000.0, 900.0, 1000.0)
    assert expiry_is_fresh(2000.0, 950.0, 1000.0)


def test_a_refreshed_response_still_expires() -> None:
    refresh_since(950.0)

    assert not expiry_is_fresh(990.0, 960.0, 1000.0)


@pytest.mark.parametrize(
    "parser, args",
    [(lock_parser, ["demo"]), (install_parser, ["demo"])],
)
def test_both_resolving_commands_take_refresh(parser, args) -> None:
    assert parser().parse_args([*args, "--refresh"]).refresh
    assert not parser().parse_args(args).refresh
