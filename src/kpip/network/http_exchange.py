"""A plain GET, exchanged in C on a connection from urllib3's pool.

urllib3 and http.client build each request and read each response a line at
a time under the interpreter lock, and an index's pages and metadata files
are small enough that this is most of what fetching them costs. With the
``_kpip_http`` built-in, :func:`exchange` takes a connection from the pool --
connected, handshaken and verified there, as for any request -- and has C
write the request and read the whole response without the lock.

It takes only what it can answer as urllib3 would: a GET with no body
through a pool of its own host, never a proxy's. Anything else, and any
exchange C gives up on, returns None, and the caller makes the request
through urllib3 as before; a GET can be made again. The connection goes back
to the pool when the response leaves it reusable, as urllib3 returns it.
"""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING, Any, NamedTuple

from kpip._vendor.urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from kpip._vendor.urllib3._collections import HTTPHeaderDict
from kpip._vendor.urllib3.connectionpool import _DEFAULT_TIMEOUT
from kpip._vendor.urllib3.response import BaseHTTPResponse, _get_decoder
from kpip._vendor.urllib3.util import Retry, Timeout
from kpip._vendor.urllib3.util.url import parse_url

try:
    import _kpip_http  # ty: ignore[unresolved-import]

except ImportError:
    _kpip_http = None

if TYPE_CHECKING:
    from collections.abc import Mapping

REDIRECT_STATUSES = frozenset(BaseHTTPResponse.REDIRECT_STATUSES)

_CONTENT_DECODERS = frozenset(BaseHTTPResponse.CONTENT_DECODERS)

_DECODER_ERRORS = BaseHTTPResponse.DECODER_ERROR_CLASSES

_FORBIDDEN = frozenset("\r\n\0")


class Exchanged(NamedTuple):
    """What urllib3's response would hold, read in C."""

    status: int
    reason: str
    headers: HTTPHeaderDict
    body: bytes
    decoded: bool
    retries: Retry


def available() -> bool:
    return _kpip_http is not None


def exchange(
    manager: Any,
    url: str,
    headers: Mapping[str, str],
    timeout: Any,
    retries: Any,
) -> Exchanged | None:
    """GET ``url`` through ``manager``'s pool in C, or None to leave it to
    urllib3."""
    if _kpip_http is None:
        return None

    pool = manager.connection_from_url(url)

    https = type(pool) is HTTPSConnectionPool

    if not https and type(pool) is not HTTPConnectionPool:
        return None

    request = request_bytes(pool, url, headers)

    if request is None:
        return None

    timeout_obj = pool._get_timeout(timeout)

    conn = pool._get_conn()

    reusable = False

    try:
        try:
            timeout_obj.start_connect()
            conn.timeout = Timeout.resolve_default_timeout(timeout_obj.connect_timeout)
            pool._validate_conn(conn)

            if conn.is_closed:
                conn.connect()

        except Exception:
            # urllib3 connects again, and raises or retries as it would.
            return None

        sock = conn.sock

        if https:
            sslobj = getattr(sock, "_sslobj", None)

            if not isinstance(sock, ssl.SSLSocket) or sslobj is None:
                return None

        else:
            sslobj = None

        # As urllib3 times the read, after the connect.
        read_timeout = timeout_obj.read_timeout

        if read_timeout is _DEFAULT_TIMEOUT:
            read_timeout = None

        sock.settimeout(read_timeout)

        result = _kpip_http.exchange(
            sslobj,
            sock.fileno(),
            request,
            -1 if read_timeout is None else max(1, int(read_timeout * 1000)),
            False,
        )

        if isinstance(result, int):
            return None

        status, reason, fields, body, keep_alive = result

        reusable = keep_alive

    finally:
        if not reusable:
            conn.close()

        pool._put_conn(conn)

    response_headers = HTTPHeaderDict()

    for name, value in fields:
        response_headers.add(name, value)

    if not isinstance(retries, Retry):
        retries = Retry.from_int(retries)

    # A status urllib3 would retry is urllib3's to retry.
    if retries.is_retry("GET", status, "Retry-After" in response_headers):
        return None

    decoded = False

    content_encoding = response_headers.get("content-encoding", "").lower()

    if content_encoding in _CONTENT_DECODERS or (
        "," in content_encoding
        and any(
            part.strip() in _CONTENT_DECODERS for part in content_encoding.split(",")
        )
    ):
        decoder = _get_decoder(content_encoding)

        try:
            decoded_body = decoder.decompress(body) + decoder.flush()

        except _DECODER_ERRORS:
            # urllib3 reads it again and raises its DecodeError.
            return None

        decoded = bool(body)

        body = decoded_body

    return Exchanged(status, reason, response_headers, body, decoded, retries)


def request_bytes(pool: Any, url: str, headers: Mapping[str, str]) -> bytes | None:
    """The request http.client writes for urllib3, or None for one it would
    refuse or write some other way."""
    names = {str(name).lower() for name in headers}

    # urllib3 names itself when the caller does not; kpip always names kpip.
    if "user-agent" not in names or "host" in names:
        return None

    host = pool.host

    if ":" in host:
        host = f"[{host}]"

    default_port = 443 if type(pool) is HTTPSConnectionPool else 80

    if pool.port is not None and pool.port != default_port:
        host = f"{host}:{pool.port}"

    lines = [f"GET {parse_url(url).request_uri} HTTP/1.1", f"Host: {host}"]

    if "accept-encoding" not in names:
        lines.append("Accept-Encoding: identity")

    for name, value in headers.items():
        name = str(name)
        value = str(value)

        if not name or ":" in name or _FORBIDDEN.intersection(name + value):
            return None

        lines.append(f"{name}: {value}")

    if _FORBIDDEN.intersection(lines[0]):
        return None

    try:
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    except UnicodeEncodeError:
        return None
