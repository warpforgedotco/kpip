"""Large TLS reads under one release of the interpreter lock.

``ssl.SSLSocket.recv_into`` reads one TLS record per call, and gives up and
retakes the interpreter lock around each: a wheel read a megabyte at a time
is sixty-four handoffs of it, contended by every thread of the install. A
read of at least ``FILL_THRESHOLD`` bytes goes to ``_kpip_tls.fill`` instead,
which loops over the records in C without the lock.

A read that large comes only from a body: ``http.client`` reads one to fill
a buffer the caller wants filled, never past the body's end, and reads its
headers through a buffer smaller than the threshold.
"""

from __future__ import annotations

import ssl
from typing import Any

try:
    import _kpip_tls  # ty: ignore[unresolved-import]

except ImportError:
    _kpip_tls = None


FILL_THRESHOLD = 256 * 1024
"""The smallest read ``fill`` takes: above ``http.client``'s buffer size."""


class FillingSSLSocket(ssl.SSLSocket):
    """An ``SSLSocket`` whose large reads fill their buffer in C."""

    def recv_into(self, buffer: Any, nbytes: int | None = None, flags: int = 0) -> int:
        sslobj = self._sslobj  # ty: ignore[unresolved-attribute]

        if sslobj is not None and not flags:
            count = nbytes or memoryview(buffer).nbytes

            if count >= FILL_THRESHOLD:
                timeout = self.gettimeout()

                got = _kpip_tls.fill(  # ty: ignore[unresolved-attribute]
                    sslobj,
                    self.fileno(),
                    buffer,
                    count,
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
