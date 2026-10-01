"""Pages of links are read only from secure origins, as pip reads them.

Checked against pip 26.2.1: a plaintext index on a host not given with
``--trusted-host`` is ignored with a warning, while HTTPS, files and the
loopback interface are used as they are.
"""

from __future__ import annotations

import logging

import pytest
from kpip.core import run_options
from kpip.index.provider import CandidateProvider
from kpip.index.source_locations import FindLinksSource
from kpip.network.exceptions import InsecureRedirectError
from kpip.network.origins import is_secure_origin, secure_source, trusted_host_key
from kpip.network.session import NetworkSession
from kpip_test_support.transport_mocks import make_response

WARNING = (
    "The repository located at insecure.test is not a trusted or secure host "
    "and is being ignored. If this repository is available via HTTPS we "
    "recommend you use HTTPS instead, otherwise you may silence this warning "
    "and allow it anyway with '--trusted-host insecure.test'."
)


@pytest.mark.parametrize(
    "url, trusted, secure",
    [
        ("https://insecure.test/simple", [], True),
        ("file:///srv/wheels", [], True),
        ("wheels/simple", [], True),
        ("git+ssh://insecure.test/repo", [], True),
        ("http://localhost:8080/simple", [], True),
        ("http://127.0.0.5/simple", [], True),
        ("http://[::1]:8080/simple", [], True),
        ("http://insecure.test/simple", [], False),
        ("git+http://insecure.test/repo", [], False),
        ("http://insecure.test/simple", ["insecure.test"], True),
        ("http://insecure.test:8080/simple", ["insecure.test:8080"], True),
        ("http://insecure.test:8081/simple", ["insecure.test:8080"], False),
        ("http://other.test/simple", ["insecure.test"], False),
    ],
)
def test_secure_origins(url: str, trusted: list[str], secure: bool) -> None:
    assert is_secure_origin(url, {trusted_host_key(h) for h in trusted}) is secure


@pytest.fixture
def no_trusted_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_options.current, "trusted_hosts", ())


@pytest.mark.usefixtures("no_trusted_hosts")
def test_an_insecure_index_is_ignored_with_pips_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        provider = CandidateProvider.from_options(
            index_url="http://insecure.test/simple",
            extra_index_urls=["https://secure.test/simple"],
        )

    assert [source.index_url for source in provider.index_sources] == [
        "https://secure.test/simple"
    ]
    assert WARNING in caplog.messages


def test_a_trusted_host_given_to_the_command_is_used(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(run_options.current, "trusted_hosts", ("insecure.test",))

    with caplog.at_level(logging.WARNING):
        provider = CandidateProvider.from_options(
            index_url="http://insecure.test/simple"
        )

    assert [source.index_url for source in provider.index_sources] == [
        "http://insecure.test/simple"
    ]
    assert not caplog.messages


@pytest.mark.usefixtures("no_trusted_hosts")
def test_a_host_a_requirements_file_trusted_is_used() -> None:
    """``--trusted-host`` in a requirements file reaches the session."""
    session = NetworkSession()
    session.trusted_hosts.add(trusted_host_key("insecure.test"))

    assert secure_source("http://insecure.test/simple", session=session)


@pytest.mark.usefixtures("no_trusted_hosts")
def test_an_insecure_find_links_page_is_not_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Session:
        trusted_hosts: set[tuple[str, int | None]] = set()

        def get(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the page must not be fetched")

    with caplog.at_level(logging.WARNING):
        links = FindLinksSource(
            ("http://insecure.test/links/",), session=Session()
        ).links_from_find_link("http://insecure.test/links/")

    assert links == []
    assert WARNING in caplog.messages


@pytest.mark.usefixtures("no_trusted_hosts")
def test_an_archive_named_by_find_links_is_still_used() -> None:
    """pip validates the page it would read, not a file named outright."""
    links = FindLinksSource(("http://insecure.test/lib-1.0-py3-none-any.whl",))

    assert [
        link.url
        for link in links.links_from_find_link(
            "http://insecure.test/lib-1.0-py3-none-any.whl"
        )
    ] == ["http://insecure.test/lib-1.0-py3-none-any.whl"]


def redirecting_session(
    monkeypatch: pytest.MonkeyPatch, start: str, target: str, **options: object
) -> tuple[NetworkSession, list[str]]:
    responses = {
        start: make_response(
            status=302,
            reason="Found",
            url=start,
            headers={"Location": target, "Content-Length": "0"},
            body=b"",
        ),
        target: make_response(
            status=200,
            reason="OK",
            url=target,
            headers={"Content-Length": "2"},
            body=b"ok",
        ),
    }
    requested: list[str] = []

    class Pool:
        def is_same_host(self, url: str) -> bool:
            del url
            return False

    class Manager:
        def request(self, method: str, url: str, **kwargs: object):
            del method
            requested.append(url)
            response = responses[url]
            response.retries = kwargs["retries"]
            return response

        def connection_from_url(self, url: str) -> Pool:
            del url
            return Pool()

    session = NetworkSession(**options)  # ty: ignore[invalid-argument-type]
    session.auth = None
    monkeypatch.setattr(session, "transport_manager", lambda *a, **k: Manager())
    return session, requested


def test_a_redirect_from_https_to_plaintext_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, requested = redirecting_session(
        monkeypatch, "https://secure.test/simple/", "http://insecure.test/simple/"
    )

    with pytest.raises(InsecureRedirectError, match="insecure.test"):
        session.get("https://secure.test/simple/")

    assert requested == ["https://secure.test/simple/"]


def test_a_redirect_to_a_trusted_host_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = redirecting_session(
        monkeypatch,
        "https://secure.test/simple/",
        "http://insecure.test/simple/",
        trusted_hosts=["insecure.test"],
    )

    assert session.get("https://secure.test/simple/").data == b"ok"
