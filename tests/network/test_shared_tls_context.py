"""A TLS context shared by every connection is configured once, not per
connection: without the GIL, writing it while other threads' handshakes read
it sent servers a malformed ClientHello ("tlsv1 alert decode error")."""

from __future__ import annotations

import ssl
import threading

from kpip._vendor.urllib3.util.ssl_ import _set_alpn_protocols_once


class CountingContext(ssl.SSLContext):
    def __init__(self, *args: object) -> None:
        self.calls = 0

    def set_alpn_protocols(self, protocols: object) -> None:
        self.calls += 1
        super().set_alpn_protocols(protocols)


def test_alpn_is_set_once_however_many_connections_start_at_once() -> None:
    context = CountingContext(ssl.PROTOCOL_TLS_CLIENT)
    start = threading.Barrier(32)

    def connect() -> None:
        start.wait()
        _set_alpn_protocols_once(context)

    threads = [threading.Thread(target=connect) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert context.calls == 1
