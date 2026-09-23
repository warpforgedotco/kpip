"""FICLONE speed probe: demote filesystems whose clone behaves like a copy.

A working reflink finishes in microseconds regardless of size; on some
filesystems the ioctl succeeds while moving data at copy speed (uv#18259:
343ms hardlink vs 11.84s clone on XFS-on-EBS). ``_record_reflink_timing``
accumulates timed clones per device and either demotes the device to the
copy fallback or proves it fast. These tests drive the decision function
directly so they run on every platform.
"""

from __future__ import annotations

import pytest
from kpip.host import clone

MB = 1024 * 1024

MS = 1_000_000


@pytest.fixture(autouse=True)
def fresh_probe_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clone, "_reflink_slow", set())
    monkeypatch.setattr(clone, "_reflink_fast", set())
    monkeypatch.setattr(clone, "_reflink_probe", {})


def test_copy_speed_clone_demotes_the_device() -> None:
    # 32 MB in 150ms is ~213 MB/s: a data copy, not a metadata clone.
    clone._record_reflink_timing(7, 32 * MB, 150 * MS)

    assert 7 in clone._reflink_slow


def test_metadata_speed_clone_proves_the_device_fast() -> None:
    # 64 MB cloned with almost no time spent: nothing but CoW explains it.
    clone._record_reflink_timing(7, 64 * MB, 1 * MS)

    assert 7 not in clone._reflink_slow
    assert 7 in clone._reflink_fast


def test_slow_clones_accumulate_across_files() -> None:
    # Each call alone is under the time threshold; together they judge.
    clone._record_reflink_timing(7, 2 * MB, 15 * MS)

    assert 7 not in clone._reflink_slow
    assert clone._reflink_probe[7] == (2 * MB, 15 * MS)

    clone._record_reflink_timing(7, 2 * MB, 15 * MS)

    assert 7 in clone._reflink_slow


def test_fast_clones_accumulate_to_a_fast_verdict() -> None:
    # 16 MB per 15ms is ~1 GB/s: crossing the time threshold proves fast.
    clone._record_reflink_timing(7, 16 * MB, 15 * MS)
    clone._record_reflink_timing(7, 16 * MB, 15 * MS)

    assert 7 not in clone._reflink_slow
    assert 7 in clone._reflink_fast


def test_a_proven_fast_device_is_never_rejudged() -> None:
    clone._reflink_fast.add(7)

    # Even a pathological later sample must not demote a proven device.
    clone._record_reflink_timing(7, 1 * MB, 500 * MS)

    assert 7 not in clone._reflink_slow
    assert 7 in clone._reflink_fast


def test_devices_are_judged_independently() -> None:
    clone._record_reflink_timing(7, 32 * MB, 150 * MS)
    clone._record_reflink_timing(8, 64 * MB, 1 * MS)

    assert clone._reflink_slow == {7}
    assert clone._reflink_fast == {8}
