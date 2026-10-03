"""``_kpip_http.exchange``, and the session reading through it.

The C is built against the test interpreter: for plain sockets everywhere,
and for TLS too where the interpreter's ``_ssl`` loads a shared libssl the
module can link to, so the two share one OpenSSL.
"""

from __future__ import annotations

import gzip
import http.server
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from kpip.network import http_exchange
from kpip.network.session import ExchangedResponse, NetworkSession
from kpip_test_support.native import build_extension

SOURCE = Path(http_exchange.__file__).parent / "_accel" / "_kpip_http.c"

REQUEST = (
    b"GET /simple/demo/ HTTP/1.1\r\nHost: index.invalid\r\nUser-Agent: kpip\r\n\r\n"
)


def _shared_libssl() -> str | None:
    """The libssl ``_ssl`` loads, when it is a shared library of its own."""
    import _ssl

    path = getattr(_ssl, "__file__", None)
    if path is None or not sys.platform.startswith("linux"):
        return None
    listed = subprocess.run(["ldd", path], capture_output=True, text=True, check=False)
    for line in listed.stdout.splitlines():
        if "libssl" in line and "=>" in line:
            return line.split("=>")[1].split()[0]
    return None


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> types.ModuleType:
    directory = tmp_path_factory.mktemp("kpip-http")
    libssl = _shared_libssl()
    if libssl is not None:
        libcrypto = libssl.replace("libssl", "libcrypto")
        module = build_extension(
            SOURCE, "_kpip_http", directory, link=(libssl, libcrypto)
        )
        if module is not None:
            module.with_tls = True
            return module
    plain = tmp_path_factory.mktemp("kpip-http-plain")
    module = build_extension(
        SOURCE, "_kpip_http", plain, defines=("KPIP_HTTP_PLAIN_ONLY",)
    )
    assert module is not None, "_kpip_http.c did not build"
    module.with_tls = False
    return module


def serve(
    pieces: list[bytes], *, close: bool = True, pause: float = 0.0
) -> tuple[socket.socket, threading.Thread, list[bytes]]:
    """A connected client socket whose peer reads one request, then writes
    ``pieces`` one send at a time."""
    client, server = socket.socketpair()
    received: list[bytes] = []

    def run() -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = server.recv(65536)
            if not chunk:
                break
            data += chunk
        received.append(data)
        for piece in pieces:
            server.sendall(piece)
            if pause:
                time.sleep(pause)
        if not close:
            # Open until the client is done with it.
            server.recv(1)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    client.settimeout(5)
    return client, thread, received


def run_exchange(
    built: types.ModuleType,
    pieces: list[bytes],
    *,
    close: bool = True,
    timeout_ms: int = 5000,
    pause: float = 0.0,
) -> tuple[object, list[bytes]]:
    client, thread, received = serve(pieces, close=close, pause=pause)
    try:
        result = built.exchange(None, client.fileno(), REQUEST, timeout_ms, False)
    finally:
        client.close()
    thread.join(5)
    return result, received


def test_a_response_with_a_length(built: types.ModuleType) -> None:
    result, received = run_exchange(
        built,
        [b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nETag: "v1"\r\n\r\nhello'],
        close=False,
    )

    assert received == [REQUEST]
    assert result == (
        200,
        "OK",
        [("Content-Length", "5"), ("ETag", '"v1"')],
        b"hello",
        True,
    )


def test_a_response_split_into_single_bytes(built: types.ModuleType) -> None:
    raw = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nX-A: 1\r\n\r\n"
        b"3\r\nabc\r\n0\r\n\r\n"
    )

    result, _ = run_exchange(
        built, [raw[i : i + 1] for i in range(len(raw))], close=False
    )

    assert result == (
        200,
        "OK",
        [("Transfer-Encoding", "chunked"), ("X-A", "1")],
        b"abc",
        True,
    )


def test_chunks_with_extensions_and_trailers(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built,
        [
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: Chunked\r\n\r\n",
            b"4;name=value\r\nwiki\r\n5 ; x\r\npedia\r\nE\r\n in\r\n\r\nchunks.\r\n",
            b"0;last\r\nExpires: never\r\nX-Trailer: 1\r\n\r\n",
        ],
        close=False,
    )

    assert result[3] == b"wikipedia in\r\n\r\nchunks."
    assert result[4] is True


def test_a_body_shorter_than_its_length_fails(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built, [b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort"]
    )

    assert result == built.FAILED_PROTOCOL


def test_disagreeing_lengths_fail(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built, [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nabc"]
    )

    assert result == built.FAILED_PROTOCOL


def test_a_gzip_body_comes_back_as_sent(built: types.ModuleType) -> None:
    body = gzip.compress(b"page" * 100)
    result, _ = run_exchange(
        built,
        [
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        ],
        close=False,
    )

    assert result[3] == body


def test_not_modified_has_no_body(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built,
        [b'HTTP/1.1 304 Not Modified\r\nETag: "v1"\r\nContent-Length: 99\r\n\r\n'],
        close=False,
    )

    assert result == (
        304,
        "Not Modified",
        [("ETag", '"v1"'), ("Content-Length", "99")],
        b"",
        True,
    )


@pytest.mark.parametrize(
    "head, keep_alive",
    [
        (b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n", False),
        (
            b"HTTP/1.1 200 OK\r\nConnection: Keep-Alive, Upgrade\r\nContent-Length: 2\r\n\r\n",
            True,
        ),
        (b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n", False),
        (
            b"HTTP/1.0 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\n",
            True,
        ),
    ],
)
def test_whether_the_connection_stays_open(
    built: types.ModuleType, head: bytes, keep_alive: bool
) -> None:
    result, _ = run_exchange(built, [head + b"ok"], close=False)

    assert result[3] == b"ok"
    assert result[4] is keep_alive


def test_a_body_to_the_end_of_the_stream(built: types.ModuleType) -> None:
    result, _ = run_exchange(built, [b"HTTP/1.1 200 OK\r\n\r\n", b"to the ", b"end"])

    assert result == (200, "OK", [], b"to the end", False)


def test_bytes_past_the_body_close_the_connection(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built,
        [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nokHTTP/1.1 200 OK\r\n"],
        close=False,
    )

    assert result[3] == b"ok"
    assert result[4] is False


def test_duplicate_headers_are_kept_in_order(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built,
        [
            b"HTTP/1.1 200 OK\r\nSet-Cookie: a=1\r\nVary: x\r\nSet-Cookie: b=2\r\nContent-Length: 0\r\n\r\n"
        ],
        close=False,
    )

    assert result[2] == [
        ("Set-Cookie", "a=1"),
        ("Vary", "x"),
        ("Set-Cookie", "b=2"),
        ("Content-Length", "0"),
    ]


def test_an_interim_response_is_skipped(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built,
        [b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx"],
        close=False,
    )

    assert result[:2] == (200, "OK")
    assert result[3] == b"x"


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/1.1 200 OK\r\nX-Long: a\r\n  folded\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n0\r\n\r\n",
        b"HTTP/1.1 101 Switching Protocols\r\n\r\n",
    ],
)
def test_what_is_left_to_urllib3(built: types.ModuleType, head: bytes) -> None:
    result, _ = run_exchange(built, [head])

    assert result == built.FAILED_UNSUPPORTED


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/2 200 OK\r\n\r\n",
        b"HTTP/1.1 20 OK\r\n\r\n",
        b"HTTP/1.1 200 OK\nContent-Length: 0\n\n",
        b"HTTP/1.1 200 OK\r\nBad Header: x\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
        b"",
    ],
)
def test_malformed_responses_fail(built: types.ModuleType, head: bytes) -> None:
    result, _ = run_exchange(built, [head])

    assert result == built.FAILED_PROTOCOL


def test_a_silent_peer_times_out(built: types.ModuleType) -> None:
    client, thread, _ = serve([], close=False)
    started = time.monotonic()
    try:
        result = built.exchange(None, client.fileno(), REQUEST, 100, False)
    finally:
        client.close()
    thread.join(5)

    assert result == built.FAILED_TIMEOUT
    assert time.monotonic() - started < 3


def test_a_slow_response_is_read_whole(built: types.ModuleType) -> None:
    result, _ = run_exchange(
        built,
        [b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n", b"ab", b"cd", b"ef"],
        close=False,
        pause=0.02,
    )

    assert result[3] == b"abcdef"


def test_a_large_body(built: types.ModuleType) -> None:
    body = bytes(range(256)) * 20000
    result, _ = run_exchange(
        built,
        [
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n",
            body,
        ],
        close=False,
    )

    assert result[3] == body


def test_another_object_is_refused(built: types.ModuleType) -> None:
    with pytest.raises(TypeError):
        built.exchange(object(), 0, REQUEST, 100, False)


# -- through the session ------------------------------------------------------


class Index(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections: set[tuple[str, int]] = set()
    requests: list[tuple[str, dict[str, str]]] = []

    def log_message(self, *args: object) -> None:
        pass

    def send_body(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if "Transfer-Encoding" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if headers.get("Transfer-Encoding") == "chunked":
            for start in range(0, len(body), 7):
                piece = body[start : start + 7]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        Index.connections.add(self.client_address)
        Index.requests.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()})
        )
        page = b'{"meta": {"api-version": "1.0"}, "name": "demo", "files": []}'
        if self.path == "/simple/demo/":
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_body(304, b"", {"ETag": '"v1"', "Cache-Control": "max-age=0"})
                return
            self.send_body(
                200,
                gzip.compress(page),
                {
                    "Content-Type": "application/vnd.pypi.simple.v1+json",
                    "Content-Encoding": "gzip",
                    "ETag": '"v1"',
                    "Cache-Control": "max-age=0",
                },
            )
        elif self.path == "/simple/chunked/":
            self.send_body(200, page, {"Transfer-Encoding": "chunked"})
        elif self.path == "/simple/Moved/":
            self.send_body(301, b"", {"Location": "/simple/demo/"})
        elif self.path == "/simple/folded/":
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nX-Folded: a\r\n b\r\nContent-Length: 2\r\n\r\nok"
            )
        elif self.path == "/simple/busy/":
            self.send_body(503, b"busy", {})
        else:
            self.send_body(404, b"missing", {})


@pytest.fixture
def index() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Index)
    Index.connections = set()
    Index.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def exchanges(built: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The URLs the session reads through C."""
    seen: list[str] = []
    real = http_exchange.exchange

    def counted(manager: object, url: str, *args: object) -> object:
        result = real(manager, url, *args)  # ty: ignore[invalid-argument-type]
        if result is not None:
            seen.append(url)
        return result

    monkeypatch.setattr(http_exchange, "_kpip_http", built)
    monkeypatch.setattr(http_exchange, "exchange", counted)
    return seen


def fetch(session: NetworkSession, url: str) -> tuple[int, str, dict[str, str], bytes]:
    response = session.get(url)
    headers = {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() not in ("date", "server")
    }
    return response.status, response.reason, headers, response.data


def test_the_session_reads_as_urllib3_does(
    index: str, exchanges: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ("/simple/demo/", "/simple/chunked/", "/simple/Moved/", "/simple/nope/")
    through_c = [fetch(NetworkSession(), index + path) for path in paths]

    monkeypatch.setattr(http_exchange, "_kpip_http", None)
    through_urllib3 = [fetch(NetworkSession(), index + path) for path in paths]

    assert through_c == through_urllib3
    assert through_c[0][3].startswith(b'{"meta"')
    assert exchanges == [
        index + "/simple/demo/",
        index + "/simple/chunked/",
        index + "/simple/Moved/",
        index + "/simple/demo/",
        index + "/simple/nope/",
    ]


def test_a_connection_is_reused(index: str, exchanges: list[str]) -> None:
    session = NetworkSession()
    for _ in range(3):
        assert session.get(index + "/simple/demo/").status == 200

    assert len(exchanges) == 3
    assert len(Index.connections) == 1


def test_the_request_carries_the_session_headers(
    index: str, exchanges: list[str]
) -> None:
    session = NetworkSession()
    session.get(index + "/simple/demo/", headers={"Accept": "application/json"})

    path, headers = Index.requests[-1]
    assert path == "/simple/demo/"
    assert headers["host"] == index.removeprefix("http://")
    assert headers["accept"] == "application/json"
    assert headers["accept-encoding"] == "gzip"
    assert headers["user-agent"].startswith("kpip/")


def test_a_cached_page_revalidates_through_c(
    index: str, exchanges: list[str], tmp_path: Path
) -> None:
    session = NetworkSession(cache=str(tmp_path / "cache"))
    first = session.get(index + "/simple/demo/")
    second = session.get(index + "/simple/demo/")

    assert first.status == second.status == 200
    assert first.data == second.data
    assert Index.requests[-1][1]["if-none-match"] == '"v1"'
    assert len(exchanges) == 2
    assert isinstance(first, ExchangedResponse)
    # The page is stored decoded, without the coding it came in.
    stored = session.cache_lookup(index + "/simple/demo/")[0]
    assert stored is None or "content-encoding" not in {
        name.lower() for name in stored.headers
    }


def test_what_c_leaves_is_read_by_urllib3(index: str, exchanges: list[str]) -> None:
    session = NetworkSession()
    folded = session.get(index + "/simple/folded/")
    busy = session.get(index + "/simple/busy/")

    assert folded.status == 200
    assert folded.data == b"ok"
    assert busy.status == 503
    # The folded header C refuses; the 503 is a status urllib3 retries.
    assert exchanges == []


def test_a_stream_is_left_to_urllib3(index: str, exchanges: list[str]) -> None:
    session = NetworkSession()
    response = session.get(index + "/simple/demo/", stream=True)

    assert b"".join(response.stream(1024))
    assert exchanges == []


def test_tls_through_c(
    built: types.ModuleType,
    exchanges: list[str],
    tmp_path: Path,
) -> None:
    if not built.with_tls:
        pytest.skip("this interpreter's _ssl has an OpenSSL of its own")
    from kpip_test_support.certs import make_tls_cert, serialize_cert, serialize_key

    cert, key = make_tls_cert("localhost")
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(serialize_cert(cert))
    key_path.write_bytes(serialize_key(key))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Index)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = NetworkSession()
        session.verify = str(cert_path)
        url = f"https://localhost:{server.server_address[1]}/simple/demo/"
        responses = [session.get(url) for _ in range(3)]
    finally:
        server.shutdown()
        server.server_close()

    assert [response.status for response in responses] == [200, 200, 200]
    assert responses[0].data.startswith(b'{"meta"')
    assert exchanges == [url, url, url]
