"""Where a pylock distribution's path or url points."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from kpip.core.urls import path_to_url
from kpip.resolution.files.pylock import pylock_location

WHEEL = "https://files.invalid/packages/demo-1.0-py3-none-any.whl"


@pytest.mark.parametrize(
    "reference",
    ["pylock.toml", "/work/project/pylock.toml", "https://locks.invalid/pylock.toml"],
)
def test_an_absolute_url_is_kept(reference: str) -> None:
    """kpip writes an index wheel's ``url``, which is no path beside the lock."""
    assert pylock_location(reference, WHEEL) == WHEEL
    assert pylock_location(reference, "file:///wheels/demo.whl") == (
        "file:///wheels/demo.whl"
    )


def test_a_path_is_relative_to_a_local_lock(tmp_path: Path) -> None:
    reference = str(tmp_path / "pylock.toml")

    assert pylock_location(reference, "wheels/demo.whl") == path_to_url(
        os.path.join(os.path.realpath(tmp_path), "wheels/demo.whl")
    )


def test_a_path_is_relative_to_a_remote_lock() -> None:
    assert (
        pylock_location("https://locks.invalid/app/pylock.toml", "demo.whl")
        == "https://locks.invalid/app/demo.whl"
    )
