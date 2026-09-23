"""A command's HTTP session retries transient failures.

A resolve makes hundreds of index requests. With no retries, one dropped
connection or a single 503 failed the whole run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.network.deferred import DeferredNetworkSession
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


def test_the_lock_session_retries() -> None:
    """The lock session is the same session, reached one step later.

    ``kpip lock`` names only a cache directory; everything else about the
    session it used to build by hand is now the deferred session's default,
    so this pins the retries it ends up with rather than the arguments it
    passed.
    """
    session = DeferredNetworkSession(cache_dir=None).materialize()

    assert session.retry.connect == DEFAULT_RETRIES
    assert session.retry.read == DEFAULT_RETRIES
    assert session.retry.status == DEFAULT_RETRIES


def test_a_lock_that_answers_from_cache_never_builds_a_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The point of deferring it: the transport is not imported to be unused.

    Building a session imports the vendored HTTP stack, which is the single
    largest import on the lock path. A resolve whose index pages are all
    fresh asks the cache and sends nothing, so it must not get that far.
    """

    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("a cached answer must not cost a session")

    monkeypatch.setattr(DeferredNetworkSession, "materialize", fail)

    deferred = DeferredNetworkSession(cache_dir=str(tmp_path))

    # A real cache, asked about a URL it has never stored: the answer is no,
    # and reaching it is filesystem work the session plays no part in.
    assert deferred.cache is not None
    assert deferred.has_fresh_cached_response("https://example.invalid/x/") is False
