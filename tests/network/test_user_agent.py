"""The user agent names the Python the requests are for."""

from __future__ import annotations

import pytest
from kpip.core import packaging
from kpip.network import session
from kpip.network.session import NetworkSession


def test_a_lock_for_another_version_is_named_without_finding_an_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(**_: object) -> None:
        pytest.fail("looked up the target interpreter")

    monkeypatch.setattr(session, "target_interpreter", unexpected)
    monkeypatch.setattr(packaging, "_TARGET_PYTHON", "3.9.0")

    assert NetworkSession.user_agent().endswith(" Python/3.9.0")
