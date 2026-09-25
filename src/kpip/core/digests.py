"""SHA-2 digests of short inputs, without loading OpenSSL.

``hashlib`` loads OpenSSL on import, which a lock answered from the cache
would spend more on than on everything it hashes: URLs, cache keys, a
lock's path. The interpreter's own SHA-2 gives the same digests. It is
several times slower per byte, so bulk data, a file above all, still goes
through ``hashlib``.
"""

from __future__ import annotations


def sha224_hexdigest(data: bytes) -> str:
    try:
        from _sha2 import sha224  # ty: ignore[unresolved-import]
    except ImportError:
        try:
            from _sha256 import sha224  # ty: ignore[unresolved-import]
        except ImportError:
            from hashlib import sha224

    return sha224(data).hexdigest()


def sha256_hexdigest(data: bytes) -> str:
    try:
        from _sha2 import sha256  # ty: ignore[unresolved-import]
    except ImportError:
        try:
            from _sha256 import sha256  # ty: ignore[unresolved-import]
        except ImportError:
            from hashlib import sha256

    return sha256(data).hexdigest()
