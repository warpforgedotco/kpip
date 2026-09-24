"""The wheelhouse fast path answers a repeated lock from its plan cache."""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.cli import fast
from kpip.core.utils import key_bytes

PACKAGES = Path(__file__).parent / "data" / "packages"
SIMPLEWHEEL = PACKAGES / "simplewheel-2.0-py2.py3-none-any.whl"


def test_equal_keys_built_differently_are_the_same_bytes() -> None:
    """marshal flags objects shared elsewhere, so equal keys differed."""
    digest = "".join(["ab", "cd"])
    shared = ("x", digest)
    alone = ("x", "".join(["ab", "cd"]))

    assert shared == alone
    assert key_bytes(shared) == key_bytes(alone)
    del digest


@pytest.mark.parametrize("keep_output", [False, True])
def test_a_repeated_lock_is_answered_from_the_plan_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_output: bool
) -> None:
    output = tmp_path / "pylock.toml"
    args = [
        "--quiet",
        "--no-index",
        "--find-links",
        str(SIMPLEWHEEL),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--output",
        str(output),
        "simplewheel==2.0",
    ]

    assert fast.run_lock(args) == 0
    written = output.read_text(encoding="utf-8")
    if not keep_output:
        output.unlink()

    from kpip.resolution.api import ResolutionEngine

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("resolved a lock the plan cache holds")

    monkeypatch.setattr(ResolutionEngine, "resolve_wheelhouse", unexpected)

    assert fast.run_lock(args) == 0
    assert output.read_text(encoding="utf-8") == written
