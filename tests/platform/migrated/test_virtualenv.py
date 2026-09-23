from __future__ import annotations

import sys

from kpip.host import virtualenv
import pytest


@pytest.mark.parametrize(
    "base_prefix, expected",
    [
        (None, False),
        (sys.prefix, False),
        ("not_sys_prefix", True),
    ],
)
def test_running_under_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    base_prefix: str | None,
    expected: bool,
) -> None:
    if base_prefix is None:
        monkeypatch.delattr(sys, "base_prefix", raising=False)
    else:
        monkeypatch.setattr(sys, "base_prefix", base_prefix, raising=False)
    assert virtualenv.running_under_virtualenv() == expected
