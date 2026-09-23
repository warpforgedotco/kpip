"""Remembering which hosts will not serve HTTP range requests.

Probing costs a HEAD and a failed read, so the answer has to be remembered
or every candidate from an index without range support pays for it again.
Equally, it must only be remembered when the host actually said so -- a
dropped connection is not evidence about range support.
"""

from __future__ import annotations

import pytest
from kpip.network.session import NetworkSession
from kpip.network.lazy_wheel import HTTPRangeRequestUnsupported

_URL = "https://example.invalid/demo-1.0-py3-none-any.whl"


def _session() -> NetworkSession:
    return NetworkSession()


def test_metadata_is_returned_and_the_host_stays_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    monkeypatch.setattr(
        "kpip.network.lazy_wheel.metadata_text_from_wheel_url",
        lambda name, url, session: "Metadata-Version: 2.1\nName: demo\n",
    )

    assert session.wheel_metadata_text(_URL, "demo") is not None
    assert session.supports_range_requests(_URL)


def test_a_host_refusing_ranges_is_asked_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    calls: list[str] = []

    def refuse(name: str, url: str, session: object) -> str:
        calls.append(url)
        raise HTTPRangeRequestUnsupported(url)

    monkeypatch.setattr(
        "kpip.network.lazy_wheel.metadata_text_from_wheel_url",
        refuse,
    )

    assert session.wheel_metadata_text(_URL, "demo") is None
    assert session.wheel_metadata_text(_URL, "demo") is None
    assert not session.supports_range_requests(_URL)
    assert len(calls) == 1, "the host was probed again after refusing"


def test_the_verdict_is_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session()

    def refuse(name: str, url: str, session: object) -> str:
        raise HTTPRangeRequestUnsupported(url)

    monkeypatch.setattr(
        "kpip.network.lazy_wheel.metadata_text_from_wheel_url",
        refuse,
    )

    session.wheel_metadata_text(_URL, "demo")

    assert not session.supports_range_requests(_URL)
    assert session.supports_range_requests("https://other.invalid/demo.whl")


def test_an_unrelated_failure_is_not_taken_as_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel that is not a zip, or a connection that dropped, says nothing
    about whether the host serves ranges -- and must not disable them."""
    session = _session()

    def fail(name: str, url: str, session: object) -> str:
        raise OSError("connection reset")

    monkeypatch.setattr(
        "kpip.network.lazy_wheel.metadata_text_from_wheel_url",
        fail,
    )

    assert session.wheel_metadata_text(_URL, "demo") is None
    assert session.supports_range_requests(_URL), (
        "one unrelated failure disabled range requests for the whole host"
    )
