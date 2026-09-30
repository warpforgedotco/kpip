"""SHA-2 digests of short inputs."""

from __future__ import annotations

lazy from hashlib import sha224, sha256


def sha224_hexdigest(data: bytes) -> str:
    return sha224(data).hexdigest()


def sha256_hexdigest(data: bytes) -> str:
    return sha256(data).hexdigest()


_HEX_DIGITS = "0123456789abcdefABCDEF"


def valid_sha256(value: object) -> bool:
    """Whether ``value`` is a sha256 hex digest, in either case."""
    return isinstance(value, str) and len(value) == 64 and not value.strip(_HEX_DIGITS)
