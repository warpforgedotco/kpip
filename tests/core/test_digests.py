"""The OpenSSL-free digests agree with ``hashlib``."""

from __future__ import annotations

import hashlib

from kpip.core.digests import sha224_hexdigest, sha256_hexdigest
from kpip.index.hashes import SUPPORTED_RECORD_HASHES


def test_the_digests_are_hashlibs() -> None:
    for data in (b"", b"https://pypi.org/simple/requests/", bytes(range(256)) * 9):
        assert sha224_hexdigest(data) == hashlib.sha224(data).hexdigest()
        assert sha256_hexdigest(data) == hashlib.sha256(data).hexdigest()


def test_the_supported_record_hashes_are_the_guaranteed_ones() -> None:
    assert SUPPORTED_RECORD_HASHES == hashlib.algorithms_guaranteed
