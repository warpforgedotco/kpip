from __future__ import annotations


from kpip.core.hashes import Hashes


def test_hashes_intersection() -> None:
    left = Hashes({"sha256": ["a", "b"]})
    right = Hashes({"sha256": ["b", "c"]})

    assert (left & right).is_hash_allowed("sha256", "b")
    assert not (left & right).is_hash_allowed("sha256", "a")
