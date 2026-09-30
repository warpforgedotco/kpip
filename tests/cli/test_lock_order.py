"""A lock lists its packages in one order, whatever the resolve did."""

from __future__ import annotations

from pathlib import Path

from kpip.core.utils import key_bytes


def test_equal_keys_built_differently_are_the_same_bytes() -> None:
    """marshal flags objects shared elsewhere, so equal keys differed."""
    digest = "".join(["ab", "cd"])
    shared = ("x", digest)
    alone = ("x", "".join(["ab", "cd"]))

    assert shared == alone
    assert key_bytes(shared) == key_bytes(alone)
    del digest


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

    assert run_lock(["--quiet", "--no-cache-dir", *common, "zeta"]) == 0

    names = [
        line.split('"')[1]
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.startswith("name = ") and not line.endswith('.whl"')
    ]

    assert names == ["alpha", "zeta"], names


def test_requirements_may_come_between_options(tmp_path: Path) -> None:
    """pip takes ``a --option b``; so does a lock."""
    from kpip.cli.lock import run_lock

    from ..wheel_helpers import make_wheel

    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "zeta", "zeta", "1.0")
    make_wheel(wheelhouse, "alpha", "alpha", "1.0")
    output = tmp_path / "pylock.toml"

    assert (
        run_lock(
            [
                "zeta",
                "--quiet",
                "--no-cache-dir",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "alpha",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    text = output.read_text(encoding="utf-8")

    assert 'name = "alpha"' in text
    assert 'name = "zeta"' in text
