"""Benchmarks for the network session policy layer, offline.

``NetworkSession.request`` runs once per index page, metadata file, and
artifact, so its per-request overhead (header assembly, credential
resolution, HTTP-cache probing, coalescing bookkeeping) scales with every
resolve.  These benchmarks stub the transport with canned responses so they
measure only kpip's own policy code, never sockets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kpip.core.http_contracts import HttpResponse
from kpip.network.download import Downloader
from kpip.network.session import NetworkSession
from kpip.network.lazy_wheel import dist_from_wheel_url
from kpip_test_support.transport_mocks import make_response
from pytest_codspeed import BenchmarkFixture

from benchmark_support import make_wheel, simple_index_html

TransportCall = tuple[str, str, dict[str, str], bytes | None, bool]

INDEX_URLS = [
    "https://mirror.invalid/simple/",
    "https://example.invalid/simple/",
]
PAGE_URLS = [f"https://example.invalid/simple/package-{index}/" for index in range(50)]
PAGE_BODY = simple_index_html(120).encode()
ARTIFACT_URLS = [
    f"https://example.invalid/packages/package-{index}-1.0.0-py3-none-any.whl"
    for index in range(8)
]
ARTIFACT_BODY = bytes(512 * 1024)


class FakeTransportSession(NetworkSession):
    """NetworkSession whose transport is a canned in-memory response table."""

    def __init__(
        self, bodies: dict[str, bytes], response_headers: dict[str, str], **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.bodies = bodies
        self.response_headers = response_headers
        self.responses = {
            url: make_response(
                status=200,
                reason="OK",
                url=url,
                headers={
                    **response_headers,
                    "Content-Length": str(len(body)),
                },
                body=body,
            )
            for url, body in bodies.items()
        }
        self.not_modified_responses = {
            url: make_response(
                status=304,
                reason="Not Modified",
                url=url,
                headers=dict(response_headers),
                body=b"",
            )
            for url in bodies
        }

    def open_internal(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        request_body: bytes | None,
        timeout,
        *,
        stream: bool = False,
    ) -> HttpResponse:
        if_none_match = headers.get("if-none-match")
        if if_none_match and if_none_match == self.response_headers.get("ETag"):
            return self.not_modified_responses[url]
        body = self.bodies[url.partition("#")[0]]
        range_header = headers.get("range")
        if not stream and range_header is None:
            return self.responses[url]
        status = 200
        response_headers = dict(self.response_headers)
        if range_header:
            total = len(body)
            spec = range_header.partition("=")[2]
            start_text, _, end_text = spec.partition("-")
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else total - 1
            body = body[start : end + 1]
            status = 206
            response_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        response_headers["Content-Length"] = str(len(body))
        return make_response(
            status=status,
            reason="OK",
            url=url,
            headers=response_headers,
            body=body,
            stream=stream,
        )


class RecordingTransportSession(FakeTransportSession):
    """Record the requests needed to prepare transport responses out of band."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.transport_calls: list[TransportCall] = []

    def open_internal(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: Any,
        *,
        stream: bool = False,
    ) -> HttpResponse:
        self.transport_calls.append(
            (
                method,
                url,
                dict(headers),
                body,
                stream,
            ),
        )
        return super().open_internal(method, url, headers, body, timeout, stream=stream)


class PreparedTransportSession(NetworkSession):
    """Return native responses prepared outside a benchmark's timed region."""

    def __init__(self, responses: list[HttpResponse], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.responses = iter(responses)

    def open_internal(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: Any,
        *,
        stream: bool = False,
    ) -> HttpResponse:
        return next(self.responses)


def prepare_transport_session(
    calls: list[TransportCall],
    bodies: dict[str, bytes],
    response_headers: dict[str, str],
) -> PreparedTransportSession:
    builder = FakeTransportSession(
        bodies=bodies,
        response_headers=response_headers,
    )
    responses = [
        builder.open_internal(
            method,
            url,
            headers,
            body,
            timeout=None,
            stream=stream,
        )
        for method, url, headers, body, stream in calls
    ]
    return PreparedTransportSession(responses)


class CannedPoolManager:
    """Pool-manager boundary that returns native responses without sockets."""

    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        return self.responses[url]


class NativeTransportSession(NetworkSession):
    """NetworkSession exercising its real urllib3 request path offline."""

    def __init__(self, responses: dict[str, HttpResponse], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.manager = CannedPoolManager(responses)

    def transport_manager(
        self, url: str, *, verify: bool | str, parsed: Any = None
    ) -> CannedPoolManager:
        return self.manager


def page_session(cache: str | None) -> FakeTransportSession:
    return FakeTransportSession(
        bodies=dict.fromkeys(PAGE_URLS, PAGE_BODY),
        response_headers={
            "Content-Type": "text/html",
            "Cache-Control": "max-age=86400",
            "ETag": '"deadbeef"',
        },
        index_urls=INDEX_URLS,
        cache=cache,
    )


@pytest.fixture(scope="session")
def warm_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """An HTTP cache primed with every index page, served fresh forever."""
    cache_dir = str(tmp_path_factory.mktemp("network-http-cache"))
    session = page_session(cache_dir)
    for url in PAGE_URLS:
        session.get(url).close()
    return cache_dir


def test_session_requests_cache_miss(benchmark: BenchmarkFixture) -> None:
    """Full request() policy overhead when every GET goes to the transport."""
    session = page_session(cache=None)

    def fetch_all() -> int:
        total = 0
        for url in PAGE_URLS:
            response = session.get(url)
            total += len(response.data)
            response.close()
        return total

    assert benchmark(fetch_all) == len(PAGE_BODY) * len(PAGE_URLS)


def test_session_native_transport_policy(benchmark: BenchmarkFixture) -> None:
    """Full request policy through the native urllib3 path, excluding sockets."""
    responses = {
        url: make_response(
            status=200,
            reason="OK",
            url=url,
            headers={
                "Content-Type": "text/html",
                "Content-Length": str(len(PAGE_BODY)),
            },
            body=PAGE_BODY,
        )
        for url in PAGE_URLS
    }
    session = NativeTransportSession(responses, index_urls=INDEX_URLS)

    def fetch_all() -> int:
        total = 0
        for url in PAGE_URLS:
            response = session.get(url)
            total += len(response.data)
            response.close()
        return total

    assert benchmark(fetch_all) == len(PAGE_BODY) * len(PAGE_URLS)


def test_session_requests_cache_hit(
    benchmark: BenchmarkFixture,
    warm_cache_dir: str,
) -> None:
    """request() served entirely from the on-disk HTTP cache."""
    session = page_session(warm_cache_dir)

    def fetch_all() -> int:
        total = 0
        for url in PAGE_URLS:
            response = session.get(url)
            assert response.from_cache
            total += len(response.data)
            response.close()
        return total

    assert benchmark(fetch_all) == len(PAGE_BODY) * len(PAGE_URLS)


def test_session_requests_revalidation_304(
    benchmark: BenchmarkFixture,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Conditional GETs answered 304, served from the revalidated cache."""
    cache_dir = str(tmp_path_factory.mktemp("network-revalidation-cache"))
    session = FakeTransportSession(
        bodies=dict.fromkeys(PAGE_URLS, PAGE_BODY),
        response_headers={
            "Content-Type": "text/html",
            "Cache-Control": "max-age=0",
            "ETag": '"stale-page"',
        },
        index_urls=INDEX_URLS,
        cache=cache_dir,
    )
    for url in PAGE_URLS:
        session.get(url).close()

    def revalidate_all() -> int:
        total = 0
        for url in PAGE_URLS:
            response = session.get(url)
            assert response.from_cache
            total += len(response.data)
            response.close()
        return total

    assert benchmark(revalidate_all) == len(PAGE_BODY) * len(PAGE_URLS)


def test_session_fresh_cache_probe(
    benchmark: BenchmarkFixture,
    warm_cache_dir: str,
) -> None:
    """has_fresh_cached_response() over a warm cache, memo cleared per pass."""
    session = page_session(warm_cache_dir)

    def probe_all() -> int:
        session.fresh_cached_response_cache.clear()
        return sum(session.has_fresh_cached_response(url) for url in PAGE_URLS)

    assert benchmark(probe_all) == len(PAGE_URLS)


def test_session_cache_store(
    benchmark: BenchmarkFixture,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """cache_response() writing metadata and body files for each response."""
    cache_dir = str(tmp_path_factory.mktemp("network-store-cache"))
    session = page_session(cache_dir)

    def store_all() -> int:
        count = 0
        for url in PAGE_URLS:
            response = session.open_internal(
                "GET",
                url,
                dict(session.headers),
                None,
                timeout=None,
            )
            session.cache_response(response)
            count += 1
        return count

    assert benchmark(store_all) == len(PAGE_URLS)


def test_auth_credential_resolution(benchmark: BenchmarkFixture) -> None:
    """Per-request credential lookup against configured index URLs."""
    session = page_session(cache=None)
    auth = session.auth

    def resolve_all() -> int:
        count = 0
        for url in PAGE_URLS:
            resolved, username, password = auth.get_url_and_credentials(url)
            assert username is None
            assert password is None
            count += 1
        return count

    assert benchmark(resolve_all) == len(PAGE_URLS)


def test_download_artifacts(
    benchmark: BenchmarkFixture,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Streaming artifact downloads through the Downloader chunk loop."""
    from kpip.index.links import Link

    bodies = dict.fromkeys(ARTIFACT_URLS, ARTIFACT_BODY)
    response_headers = {"Content-Type": "application/octet-stream"}
    links = [Link.from_url(url, source_url=None) for url in ARTIFACT_URLS]
    location = str(tmp_path_factory.mktemp("network-downloads"))

    recording_session = RecordingTransportSession(
        bodies=bodies,
        response_headers=response_headers,
    )
    assert sum(1 for _ in Downloader(recording_session).batch(links, location)) == len(
        ARTIFACT_URLS,
    )

    def setup() -> tuple[tuple[Downloader], dict[str, Any]]:
        session = prepare_transport_session(
            recording_session.transport_calls,
            bodies,
            response_headers,
        )
        return (Downloader(session),), {}

    def download_all(downloader: Downloader) -> int:
        return sum(1 for _ in downloader.batch(links, location))

    assert benchmark.pedantic(download_all, setup=setup, rounds=25) == len(
        ARTIFACT_URLS,
    )


@pytest.fixture(scope="session")
def range_wheel(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, bytes]:
    wheelhouse = tmp_path_factory.mktemp("lazy-wheelhouse")
    path = make_wheel(wheelhouse, "lazy-target", "1.0.0", payload_files=50)
    url = f"https://example.invalid/packages/{path.name}"
    return url, Path(path).read_bytes()


def test_lazy_wheel_metadata(
    benchmark: BenchmarkFixture,
    range_wheel: tuple[str, bytes],
) -> None:
    """Range-request metadata extraction via LazyZipOverHTTP."""
    url, body = range_wheel
    bodies = {url: body}
    response_headers = {
        "Content-Type": "application/octet-stream",
        "Accept-Ranges": "bytes",
    }
    recording_session = RecordingTransportSession(
        bodies=bodies,
        response_headers=response_headers,
    )
    assert (
        dist_from_wheel_url("lazy-target", url, recording_session).metadata["Name"]
        == "lazy-target"
    )

    def setup() -> tuple[tuple[PreparedTransportSession], dict[str, Any]]:
        session = prepare_transport_session(
            recording_session.transport_calls,
            bodies,
            response_headers,
        )
        return (session,), {}

    def read_metadata(session: NetworkSession) -> str:
        dist = dist_from_wheel_url("lazy-target", url, session)
        return dist.metadata["Name"]

    assert benchmark.pedantic(read_metadata, setup=setup, rounds=25) == "lazy-target"
