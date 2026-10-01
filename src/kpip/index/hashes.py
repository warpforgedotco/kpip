"""Hash fragments and validation helpers for package links."""

from __future__ import annotations

SUPPORTED_HASHES = ("sha512", "sha384", "sha256", "sha224", "sha1", "md5")
SUPPORTED_RECORD_HASHES = frozenset(
    (
        "blake2b",
        "blake2s",
        "md5",
        "sha1",
        "sha224",
        "sha256",
        "sha384",
        "sha3_224",
        "sha3_256",
        "sha3_384",
        "sha3_512",
        "sha512",
        "shake_128",
        "shake_256",
    )
)
"""``hashlib.algorithms_guaranteed``, spelled out so that reading it does not
load OpenSSL."""
