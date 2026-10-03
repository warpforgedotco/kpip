"""TLS reads under one release of the interpreter lock.

``ssl.SSLSocket.recv_into`` reads one TLS record, at most 16 KiB, per call,
and gives up and retakes the interpreter lock around each: a wheel read a
megabyte at a time is sixty-four handoffs of it, contended by every thread
of the install. ``_kpip_tls.fill`` waits for the first byte as ``recv``
does, then takes every record already there, all without the lock.
"""

from __future__ import annotations

import ssl
from typing import Any

try:
    import _kpip_tls  # ty: ignore[unresolved-import]

except ImportError:
    _kpip_tls = None


class FillingSSLSocket(ssl.SSLSocket):
    """An ``SSLSocket`` whose reads take every record already there."""

    def recv_into(self, buffer: Any, nbytes: int | None = None, flags: int = 0) -> int:
        sslobj = self._sslobj  # ty: ignore[unresolved-attribute]

        if sslobj is not None and not flags:
            timeout = self.gettimeout()

            got = _kpip_tls.fill(  # ty: ignore[unresolved-attribute]
                sslobj,
                self.fileno(),
                buffer,
                nbytes or memoryview(buffer).nbytes,
                -1 if timeout is None else max(0, int(timeout * 1000)),
            )

            if got >= 0:
                return got

            if got == -1:
                raise TimeoutError("The read operation timed out")

        return super().recv_into(buffer, nbytes, flags)


def install() -> None:
    """Make every TLS socket kpip opens a ``FillingSSLSocket``, when the
    built-in is there."""
    if _kpip_tls is not None:
        ssl.SSLContext.sslsocket_class = FillingSSLSocket
