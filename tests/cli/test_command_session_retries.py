"""A command's HTTP session retries transient failures.

A resolve makes hundreds of index requests. With no retries, one dropped
connection or a single 503 failed the whole run.
"""

from __future__ import annotations

import pytest
from kpip.cli import lock
from kpip.cli.requirements import DeferredNetworkSession
from kpip.network.http import DEFAULT_RETRIES


def test_the_install_session_retries() -> None:
    deferred = DeferredNetworkSession(
        index_urls=[],
        cache_dir=None,
        cert=None,
        client_cert=None,
        no_input=True,
        keyring_provider="disabled",
        proxy=None,
    )

    session = deferred.materialize()

    assert DEFAULT_RETRIES > 0
    assert session.retry.connect == DEFAULT_RETRIES
    assert session.retry.read == DEFAULT_RETRIES
    assert session.retry.status == DEFAULT_RETRIES


class _Built(Exception):
    pass


def test_the_lock_session_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_session(**kwargs: object) -> None:
        seen.update(kwargs)
        raise _Built

    monkeypatch.setattr(lock, "NetworkSession", fake_session)

    with pytest.raises(_Built):
        lock.run_lock(["-r", "requirements.in"])

    assert seen["retries"] == DEFAULT_RETRIES
