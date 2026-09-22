"""The validated-catalog memo survives concurrent maintenance.

Worker threads build catalogs in parallel and each records its blob in a
small per-cache memo.  Moving a key to the end and evicting the oldest are
several dict operations apiece, and a thread switch between choosing the
oldest key and removing it raised ``KeyError`` when another thread had
already removed it.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from kpip.index import catalog_cache


def _catalog(index: int) -> tuple[list, list]:
    return ([], [f"record-{index}"])


def test_concurrent_writers_never_raise_and_respect_the_limit() -> None:
    cache = SimpleNamespace()
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(worker: int) -> None:
        start.wait()
        try:
            for step in range(200):
                url = f"https://example.invalid/simple/pkg-{worker}-{step}/"
                catalog_cache._remember_validated_catalog(
                    cache,
                    url,
                    f"{worker}-{step}".encode(),
                    _catalog(step),
                )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    memo = catalog_cache._validated_catalogs(cache)
    assert memo is not None
    assert len(memo) <= catalog_cache._VALIDATED_CATALOGS_LIMIT


def test_a_reader_touching_an_evicted_entry_does_not_raise() -> None:
    """The read path moves a hit to the end, and the hit may be gone by then."""
    cache = SimpleNamespace()
    catalog_cache._remember_validated_catalog(cache, "u", b"raw", _catalog(0))
    memo = catalog_cache._validated_catalogs(cache)
    assert memo is not None

    # Stand in for another thread evicting between the read and the touch.
    memo.clear()

    catalog_cache._remember_validated_catalog(cache, "u", b"raw", _catalog(0))

    assert list(memo) == ["u"]
