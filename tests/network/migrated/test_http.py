from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from kpip._vendor.urllib3.connection import HTTPConnection
from kpip._vendor.urllib3.exceptions import DecodeError, NewConnectionError
from kpip._vendor.urllib3.response import HTTPResponse as Urllib3HTTPResponse
from kpip._vendor.urllib3.util import Timeout
from kpip.network.exceptions import ConnectionFailedError, TooManyRedirectsError
from kpip.network.session import DEFAULT_TIMEOUT, NetworkSession
from kpip_test_support.transport_mocks import make_response


def test_session_decodes_gzip_responses(tmp_path) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            body = gzip.compress(b"compressed")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession(cache=str(tmp_path / "http-cache"))
        url = f"http://127.0.0.1:{server.server_port}/catalog"
        response = session.get(url)
        assert isinstance(response, Urllib3HTTPResponse)
        assert response.url == url
        assert response.data == b"compressed"
        assert response.headers.get("Content-Encoding") == "gzip"

        cached = session.get(url)
        assert cached.data == b"compressed"
        assert cached.from_cache
        assert cached.headers.get("Content-Encoding") is None
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_session_reuses_direct_http_connection() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        connections = 0
        requests = 0

        def setup(self) -> None:
            super().setup()
            type(self).connections += 1

        def do_GET(self) -> None:
            type(self).requests += 1
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        url = f"http://127.0.0.1:{server.server_port}/catalog"
        assert session.get(url).data == b"ok"

        responses: list[bytes] = []

        def request() -> None:
            responses.append(session.get(url).data)

        worker = threading.Thread(target=request)
        worker.start()
        worker.join()
        assert responses == [b"ok"]
        assert Handler.requests == 2
        assert Handler.connections == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_session_uses_finite_default_timeout() -> None:
    session = NetworkSession()

    assert session.timeout.connect_timeout == DEFAULT_TIMEOUT
    assert session.timeout.read_timeout == DEFAULT_TIMEOUT


def test_session_uses_urllib3_status_retries() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            status = 500 if self.requests == 1 else 200
            body = b"retry" if status == 500 else b"ok"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession(retries=1)
        url = f"http://127.0.0.1:{server.server_port}/catalog"

        assert session.get(url).data == b"ok"
        assert Handler.requests == 2
        assert session.retry.status_forcelist == {500, 502, 503, 520, 527}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_session_coalesces_concurrent_gets() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            time.sleep(0.1)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        url = f"http://127.0.0.1:{server.server_port}/catalog"
        barrier = threading.Barrier(2)
        responses: list[bytes] = []

        def request() -> None:
            barrier.wait()
            responses.append(session.get(url).data)

        workers = [threading.Thread(target=request) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert responses == [b"ok", b"ok"]
        assert Handler.requests == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_uncontended_get_does_not_allocate_wait_event(monkeypatch) -> None:
    session = NetworkSession()
    url = "https://example.invalid/simple/demo/"

    monkeypatch.setattr(
        session,
        "open_internal",
        lambda *args, **kwargs: make_response(
            status=200,
            reason="OK",
            url=url,
            headers={},
            body=b"ok",
        ),
    )

    def unexpected_event() -> None:
        pytest.fail("an uncontended request allocated a wait event")

    monkeypatch.setattr(threading, "Event", unexpected_event)

    assert session.open_coalesced("GET", url, {}, None, timeout=None).data == b"ok"


def test_new_connection_error_is_not_reported_as_timeout(monkeypatch) -> None:
    session = NetworkSession()
    error = NewConnectionError(HTTPConnection("example.invalid"), "connection failed")

    def fail(*args, **kwargs):
        del args, kwargs
        raise error

    monkeypatch.setattr(session, "open_coalesced", fail)

    with pytest.raises(ConnectionFailedError):
        session.get("https://example.invalid/simple/")


def test_decode_error_remains_available_to_callers(monkeypatch) -> None:
    session = NetworkSession()
    error = DecodeError("invalid compressed response")

    def fail(*args, **kwargs):
        del args, kwargs
        raise error

    monkeypatch.setattr(session, "open_coalesced", fail)

    with pytest.raises(DecodeError, match="invalid compressed response"):
        session.get("https://example.invalid/demo.whl")


def test_streaming_header_override_is_case_insensitive(monkeypatch) -> None:
    session = NetworkSession()
    session.auth = None
    seen_headers: dict[str, str] = {}

    def open_response(method, url, headers, body, timeout, *, stream=False):
        del method, body, timeout, stream
        seen_headers.update(headers)
        return make_response(
            status=200,
            reason="OK",
            url=url,
            headers={},
            body=b"",
        )

    monkeypatch.setattr(session, "open_coalesced", open_response)

    session.get(
        "https://example.invalid/demo.whl",
        headers={"accept-encoding": "gzip"},
        stream=True,
    )

    assert [
        (name, value)
        for name, value in seen_headers.items()
        if name.lower() == "accept-encoding"
    ] == [("accept-encoding", "identity")]


def test_tuple_timeout_is_normalized_before_transport(monkeypatch) -> None:
    session = NetworkSession()
    session.auth = None
    seen_timeout: Timeout | None = None

    def open_response(method, url, headers, body, timeout, *, stream=False):
        nonlocal seen_timeout
        del method, headers, body, stream
        seen_timeout = timeout
        return make_response(
            status=200,
            reason="OK",
            url=url,
            headers={},
            body=b"",
        )

    monkeypatch.setattr(session, "open_with_redirects", open_response)

    session.get("https://example.invalid/demo.whl", timeout=(1.5, 4.0))

    assert seen_timeout is not None
    assert seen_timeout.connect_timeout == 1.5
    assert seen_timeout.read_timeout == 4.0


def test_native_timeout_reaches_transport_without_copy(monkeypatch) -> None:
    session = NetworkSession()
    session.auth = None
    native_timeout = Timeout(connect=1.5, read=4.0)
    seen_timeout: Timeout | None = None

    def open_response(method, url, headers, body, timeout, *, stream=False):
        nonlocal seen_timeout
        del method, headers, body, stream
        seen_timeout = timeout
        return make_response(
            status=200,
            reason="OK",
            url=url,
            headers={},
            body=b"",
        )

    monkeypatch.setattr(session, "open_with_redirects", open_response)

    session.get("https://example.invalid/demo.whl", timeout=native_timeout)

    assert seen_timeout is native_timeout


def test_no_redirect_reuses_request_headers_at_transport(monkeypatch) -> None:
    session = NetworkSession()
    request_headers = {"accept": "application/octet-stream"}
    seen_headers = None

    class Manager:
        def request(self, method, url, *, headers, **kwargs):
            nonlocal seen_headers
            del method, kwargs
            seen_headers = headers
            return make_response(
                status=200,
                reason="OK",
                url=url,
                headers={},
                body=b"wheel",
            )

    monkeypatch.setattr(session, "transport_manager", lambda *args, **kwargs: Manager())

    session.open_with_redirects(
        "GET",
        "https://example.invalid/demo.whl",
        request_headers,
        None,
        Timeout(),
        stream=False,
    )

    assert seen_headers is request_headers


def test_lowercase_range_header_bypasses_cache(tmp_path) -> None:
    class FailingCache:
        def get_with_body(self, key):
            raise AssertionError(f"cache lookup for range request: {key}")

        def set_with_body(self, key, metadata, body):
            del key, metadata, body

    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"wheel")
    session = NetworkSession(cache=FailingCache())

    response = session.get(artifact.as_uri(), headers={"range": "bytes=0-1"})

    assert response.data == b"wheel"


def test_file_response_streams(tmp_path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"wheel")

    response = NetworkSession().get(artifact.as_uri(), stream=True)

    assert b"".join(response.stream(2)) == b"wheel"


def test_fresh_cache_hit_skips_credential_resolution(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NetworkSession(cache=str(tmp_path / "http-cache"))
    url = "https://example.invalid/simple/demo/"
    session.cache_response(
        make_response(
            status=200,
            reason="OK",
            url=url,
            headers={"Cache-Control": "max-age=60"},
            body=b"cached body",
        ),
    )

    def unexpected_auth(url: str) -> None:
        pytest.fail(f"resolved credentials for fresh cache hit: {url}")

    monkeypatch.setattr(session.auth, "get_url_and_credentials", unexpected_auth)

    response = session.get(url)

    assert response.from_cache
    assert response.data == b"cached body"


def test_304_reuses_body_opened_during_cache_lookup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NetworkSession(cache=str(tmp_path / "http-cache"))
    assert session.cache is not None
    url = "https://example.invalid/simple/demo/"
    session.cache_response(
        make_response(
            status=200,
            reason="OK",
            url=url,
            headers={"Cache-Control": "max-age=0", "ETag": '"tag"'},
            body=b"cached body",
        ),
    )
    monkeypatch.setattr(
        session.cache,
        "get_body",
        lambda key: pytest.fail(f"reopened cached body: {key}"),
    )
    monkeypatch.setattr(
        session,
        "open_coalesced",
        lambda *args, **kwargs: make_response(
            status=304,
            reason="Not Modified",
            url=url,
            headers={"Cache-Control": "max-age=0", "ETag": '"tag"'},
            body=b"",
        ),
    )

    response = session.get(url)

    assert response.from_cache
    assert response.data == b"cached body"


def test_session_revalidates_stale_cache_with_conditional_headers(
    tmp_path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        conditional_headers: dict[str, str | None] = {}
        full_responses = 0
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            if self.headers.get("If-None-Match") or self.headers.get(
                "If-Modified-Since",
            ):
                type(self).conditional_headers = {
                    "If-None-Match": self.headers.get("If-None-Match"),
                    "If-Modified-Since": self.headers.get("If-Modified-Since"),
                }
                self.send_response(304)
                self.send_header("ETag", '"tag-2"')
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                return
            type(self).full_responses += 1
            body = b"cached body"
            self.send_response(200)
            self.send_header("ETag", '"tag-1"')
            self.send_header("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
            self.send_header("Cache-Control", "max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession(cache=str(tmp_path / "http-cache"))
        url = f"http://127.0.0.1:{server.server_port}/catalog"

        first = session.get(url)
        assert first.data == b"cached body"
        assert not getattr(first, "from_cache", False)

        second = session.get(url)
        assert second.data == b"cached body"
        assert second.from_cache
        assert second.headers.get("ETag") == '"tag-2"'
        assert Handler.full_responses == 1
        assert Handler.conditional_headers == {
            "If-None-Match": '"tag-1"',
            "If-Modified-Since": "Mon, 01 Jan 2024 00:00:00 GMT",
        }

        third = session.get(url)
        assert third.data == b"cached body"
        assert third.from_cache
        assert Handler.requests == 2

        stored = json.loads(session.cache.get(url).decode("utf-8"))
        assert stored["etag"] == '"tag-2"'
        assert stored["headers"]["Cache-Control"] == "max-age=3600"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_unchanged_immediately_stale_304_skips_metadata_rewrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NetworkSession(cache=str(tmp_path / "http-cache"))
    assert session.cache is not None
    url = "https://example.invalid/simple/demo/"
    body = b"cached body"
    session.cache_response(
        make_response(
            status=200,
            reason="OK",
            url=url,
            headers={"Cache-Control": "max-age=0", "ETag": '"tag"'},
            body=body,
        ),
    )
    raw_metadata = session.cache.get(url)
    assert raw_metadata is not None
    metadata = json.loads(raw_metadata)
    writes: list[tuple[str, bytes]] = []
    monkeypatch.setattr(session.cache, "set", lambda *args: writes.append(args))

    response = session.revalidated_response(
        url,
        metadata,
        {"Cache-Control": "max-age=0", "ETag": '"tag"'},
    )

    assert response.data == body
    assert writes == []


def test_cache_response_writes_metadata_and_body_atomically() -> None:
    writes: list[tuple[str, bytes, bytes]] = []

    class RecordingCache:
        def set_with_body(self, key: str, metadata: bytes, body: bytes) -> None:
            writes.append((key, metadata, body))

    url = "https://example.invalid/simple/demo/"
    body = b"cached body"
    session = NetworkSession(cache=RecordingCache())
    session.cache_response(
        make_response(
            status=200,
            reason="OK",
            url=url,
            headers={"Cache-Control": "max-age=60"},
            body=body,
        ),
    )

    assert len(writes) == 1
    assert writes[0][0] == url
    assert json.loads(writes[0][1])["status"] == 200
    assert writes[0][2] == body


def test_environ_proxies_cached_per_host_and_port(
    monkeypatch,
) -> None:
    """NO_PROXY can name a port; the bypass decision must not be shared
    between two ports of the same host."""
    import urllib.parse

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "internal.test:8080")
    session = NetworkSession()

    bypassed_url = "http://internal.test:8080/simple/"
    proxied_url = "http://internal.test:9090/simple/"

    bypassed = session.environ_proxies_for(
        bypassed_url,
        urllib.parse.urlsplit(bypassed_url),
    )
    proxied = session.environ_proxies_for(
        proxied_url,
        urllib.parse.urlsplit(proxied_url),
    )

    assert bypassed == {}
    assert proxied.get("http") == "http://proxy.invalid:3128"


def test_redirect_gains_environment_proxy_for_destination(
    monkeypatch,
) -> None:
    """trust_env is off, so the redirect hook must add the destination's
    environment proxy itself (and still honor NO_PROXY for it)."""

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "start.test")
    session = NetworkSession()

    assert session.proxy_for("http://elsewhere.test/file") == (
        "http://proxy.invalid:3128"
    )
    assert session.proxy_for("http://start.test/file") is None


def test_redirect_reselects_tls_policy_for_destination(monkeypatch) -> None:
    start_url = "https://trusted.test/start"
    target_url = "https://secure.test/target"
    responses = {
        start_url: make_response(
            status=302,
            reason="Found",
            url=start_url,
            headers={"Location": target_url, "Content-Length": "0"},
            body=b"",
        ),
        target_url: make_response(
            status=200,
            reason="OK",
            url=target_url,
            headers={"Content-Length": "2"},
            body=b"ok",
        ),
    }
    selected: list[tuple[str, bool | str]] = []

    class Pool:
        def is_same_host(self, url: str) -> bool:
            del url
            return False

    class Manager:
        def __init__(self, url: str) -> None:
            self.url = url

        def request(self, method: str, url: str, **kwargs):
            del method
            assert url == self.url
            response = responses[url]
            response.retries = kwargs["retries"]
            return response

        def connection_from_url(self, url: str) -> Pool:
            assert url == self.url
            return Pool()

    def transport_manager(url: str, *, verify: bool | str, parsed=None) -> Manager:
        del parsed
        selected.append((url, verify))
        return Manager(url)

    session = NetworkSession(trusted_hosts=["trusted.test"])
    session.auth = None
    session.environ_ca_bundle = None
    monkeypatch.setattr(session, "transport_manager", transport_manager)

    response = session.get(start_url)

    assert response.data == b"ok"
    assert selected == [(start_url, False), (target_url, True)]


def test_redirect_exhaustion_has_a_redirect_specific_error(monkeypatch) -> None:
    url = "https://example.invalid/start"
    redirected = make_response(
        status=302,
        reason="Found",
        url=url,
        headers={"Location": "/again", "Content-Length": "0"},
        body=b"",
    )

    class Pool:
        def is_same_host(self, target: str) -> bool:
            del target
            return True

    class Manager:
        def request(self, method: str, target: str, **kwargs):
            del method, target, kwargs
            return redirected

        def connection_from_url(self, target: str) -> Pool:
            del target
            return Pool()

    session = NetworkSession()
    session.retry = session.retry.new(redirect=0)
    monkeypatch.setattr(session, "transport_manager", lambda *args, **kwargs: Manager())

    with pytest.raises(TooManyRedirectsError, match="Too many redirects"):
        session.get(url)


def test_redirect_response_is_drained_when_processing_fails(monkeypatch) -> None:
    url = "https://example.invalid/start"
    redirected = make_response(
        status=302,
        reason="Found",
        url=url,
        headers={"Location": "/target", "Content-Length": "0"},
        body=b"",
    )
    drained = False

    def drain() -> None:
        nonlocal drained
        drained = True

    monkeypatch.setattr(redirected, "drain_conn", drain)

    class Manager:
        def request(self, method: str, target: str, **kwargs):
            del method, target, kwargs
            return redirected

        def connection_from_url(self, target: str) -> None:
            del target
            raise RuntimeError("failed to inspect redirect target")

    session = NetworkSession()
    monkeypatch.setattr(session, "transport_manager", lambda *args, **kwargs: Manager())

    with pytest.raises(RuntimeError, match="failed to inspect redirect target"):
        session.get(url)

    assert drained


def test_redirect_does_not_send_netrc_credentials_over_http(
    tmp_path,
    monkeypatch,
) -> None:
    netrc_path = tmp_path / "netrc"
    netrc_path.write_text("machine localhost login redirected password secret\n")
    monkeypatch.setenv("NETRC", str(netrc_path))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        seen_authorization: list[str | None] = []

        def do_GET(self) -> None:
            if self.path == "/start":
                target = f"http://localhost:{self.server.server_port}/target"
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            type(self).seen_authorization.append(self.headers.get("Authorization"))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        url = f"http://127.0.0.1:{server.server_port}/start"
        response = session.get(url, headers={"authorization": "Basic original"})
        assert response.data == b"ok"

        assert Handler.seen_authorization == [None]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_same_host_redirect_preserves_existing_authorization(
    tmp_path,
    monkeypatch,
) -> None:
    netrc_path = tmp_path / "netrc"
    netrc_path.write_text(
        "machine 127.0.0.1 login replacement password credentials\n",
    )
    monkeypatch.setenv("NETRC", str(netrc_path))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        seen_authorization: list[str | None] = []

        def do_GET(self) -> None:
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            type(self).seen_authorization.append(self.headers.get("Authorization"))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        session = NetworkSession()
        session.auth = None
        url = f"http://127.0.0.1:{server.server_port}/start"
        response = session.get(url, headers={"Authorization": "Basic original"})

        assert response.data == b"ok"
        assert Handler.seen_authorization == ["Basic original"]
        assert response.retries is not None
        assert [item.status for item in response.retries.history] == [302]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_ssl_context_shared_per_tls_policy() -> None:
    """One SSLContext per (verify, cert) policy, reused by every pool.

    Without a shared context urllib3 builds a fresh one and re-parses the
    CA bundle for every new HTTPS connection.
    """
    import ssl

    session = NetworkSession()

    context = session.ssl_context_for(True, None)

    assert session.ssl_context_for(True, None) is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname

    insecure = session.ssl_context_for(False, None)

    assert insecure is not context
    assert insecure.verify_mode == ssl.CERT_NONE
    assert not insecure.check_hostname

    manager = session.transport_manager("https://a.invalid/", verify=True)

    assert manager.connection_pool_kw["ssl_context"] is context
    assert session.transport_manager("https://b.invalid/", verify=True) is manager
    assert len(session.ssl_contexts) == 2


def test_cross_host_https_redirect_uses_destination_netrc(
    tmp_path,
    monkeypatch,
) -> None:
    """An authenticated request redirected to another HTTPS host drops the
    original Authorization header and authenticates with the destination's
    own credentials."""
    import base64

    netrc_path = tmp_path / "netrc"
    netrc_path.write_text("machine dest.test login mirror password sesame\n")
    monkeypatch.setenv("NETRC", str(netrc_path))

    start_url = "https://start.test/file"
    target_url = "https://dest.test/file"
    responses = {
        start_url: make_response(
            status=302,
            reason="Found",
            url=start_url,
            headers={"Location": target_url, "Content-Length": "0"},
            body=b"",
        ),
        target_url: make_response(
            status=200,
            reason="OK",
            url=target_url,
            headers={"Content-Length": "2"},
            body=b"ok",
        ),
    }
    seen: list[tuple[str, str | None]] = []

    class Pool:
        def is_same_host(self, url: str) -> bool:
            del url
            return False

    class Manager:
        def request(self, method, url, *, headers, **kwargs):
            del method
            seen.append((url, headers.get("authorization")))
            response = responses[url]
            response.retries = kwargs["retries"]
            return response

        def connection_from_url(self, url: str) -> Pool:
            del url
            return Pool()

    session = NetworkSession()
    monkeypatch.setattr(session, "transport_manager", lambda *args, **kwargs: Manager())

    response = session.get(start_url, headers={"Authorization": "Basic original"})

    assert response.data == b"ok"
    expected = "Basic " + base64.b64encode(b"mirror:sesame").decode()
    assert seen == [(start_url, "Basic original"), (target_url, expected)]


def test_anonymous_cross_host_redirect_stays_anonymous(
    tmp_path,
    monkeypatch,
) -> None:
    """A request that never carried credentials gains none from a redirect,
    even when netrc knows the destination host."""
    netrc_path = tmp_path / "netrc"
    netrc_path.write_text("machine dest.test login mirror password sesame\n")
    monkeypatch.setenv("NETRC", str(netrc_path))

    start_url = "https://anon-start.test/file"
    target_url = "https://dest.test/file"
    responses = {
        start_url: make_response(
            status=302,
            reason="Found",
            url=start_url,
            headers={"Location": target_url, "Content-Length": "0"},
            body=b"",
        ),
        target_url: make_response(
            status=200,
            reason="OK",
            url=target_url,
            headers={"Content-Length": "2"},
            body=b"ok",
        ),
    }
    seen: list[tuple[str, str | None]] = []

    class Pool:
        def is_same_host(self, url: str) -> bool:
            del url
            return False

    class Manager:
        def request(self, method, url, *, headers, **kwargs):
            del method
            seen.append((url, headers.get("authorization")))
            response = responses[url]
            response.retries = kwargs["retries"]
            return response

        def connection_from_url(self, url: str) -> Pool:
            del url
            return Pool()

    session = NetworkSession()
    monkeypatch.setattr(session, "transport_manager", lambda *args, **kwargs: Manager())

    response = session.get(start_url)

    assert response.data == b"ok"
    assert seen == [(start_url, None), (target_url, None)]
