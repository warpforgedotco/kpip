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


def test_a_lock_lists_its_packages_by_name(tmp_path: Path) -> None:
    """Whatever order the resolve pinned them in: that order depends on which
    pages arrived first, and a lock whose entries move is a diff with
    nothing in it."""
    from kpip.cli.lock import run_lock

    from ..wheel_helpers import make_wheel

    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "zeta", "zeta", "1.0", requires=["alpha"])
    make_wheel(wheelhouse, "alpha", "alpha", "1.0")
    output = tmp_path / "pylock.toml"
    common = ["--no-index", "--find-links", str(wheelhouse), "--output", str(output)]

    for lock in (
        lambda: fast.run_lock(["--quiet", "--no-cache-dir", *common, "zeta"]),
        lambda: run_lock(["--quiet", "--no-cache-dir", *common, "zeta"]),
    ):
        assert lock() == 0
        names = [
            line.split('"')[1]
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.startswith("name = ") and not line.endswith('.whl"')
        ]
        assert names == ["alpha", "zeta"], names
        output.unlink()
