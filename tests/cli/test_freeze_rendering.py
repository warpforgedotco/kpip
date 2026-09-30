"""What ``freeze`` prints for the distributions on the path."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from kpip.cli.freeze import run_freeze

URL_LINE = (
    "from-url @ https://example.invalid/from_url-0.5.tar.gz#sha256=" + "ab" * 32 + "\n"
)


def _dist(
    root: Path,
    dirname: str,
    name: str,
    version: str,
    *,
    direct_url: dict | None = None,
) -> Path:
    info = root / dirname
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
    return info


def _freeze(args: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        assert run_freeze(args) == 0
    return out.getvalue()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _dist(first, "Zeta_Pkg-1.0.dist-info", "Zeta-Pkg", "1.0")
    _dist(first, "shadowed-1.0.dist-info", "shadowed", "1.0")
    _dist(second, "shadowed-2.0.dist-info", "shadowed", "2.0")
    _dist(second, "alpha-2.0.1.dist-info", "alpha", "2.0.1")
    _dist(
        second,
        "from_url-0.5.dist-info",
        "from-url",
        "0.5",
        direct_url={
            "url": "https://example.invalid/from_url-0.5.tar.gz",
            "archive_info": {"hashes": {"sha256": "ab" * 32}},
        },
    )
    _dist(second, "kpip-0.0.1.dist-info", "kpip", "0.0.1")
    _dist(second, "argparse-1.4.0.dist-info", "argparse", "1.4.0")
    (second / "empty-9.dist-info").mkdir()
    monkeypatch.setattr(
        sys, "path", [str(tmp_path / "missing"), str(first), str(second)]
    )
    monkeypatch.delenv("KPIP_TARGET_PREFIX", raising=False)
    return tmp_path


def test_the_first_of_a_name_on_the_path_is_frozen(site: Path) -> None:
    assert _freeze([]) == (
        "alpha==2.0.1\nempty==9\n" + URL_LINE + "shadowed==1.0\nZeta-Pkg==1.0\n"
    )


def test_all_includes_kpip_itself(site: Path) -> None:
    assert _freeze(["--all"]) == (
        "alpha==2.0.1\nempty==9\n"
        + URL_LINE
        + "kpip==0.0.1\nshadowed==1.0\nZeta-Pkg==1.0\n"
    )


def test_exclude_drops_a_name(site: Path) -> None:
    assert _freeze(["--exclude", "alpha"]) == (
        "empty==9\n" + URL_LINE + "shadowed==1.0\nZeta-Pkg==1.0\n"
    )
    assert _freeze(["--exclude", "kpip"]) == _freeze([])


def test_path_orders_the_roots_as_given(site: Path) -> None:
    assert _freeze(["--path", str(site / "second")]) == (
        "alpha==2.0.1\nempty==9\n" + URL_LINE + "shadowed==2.0\n"
    )
    assert _freeze(
        ["--path", str(site / "second"), "--path", str(site / "first"), "--all"]
    ) == (
        "alpha==2.0.1\nempty==9\n"
        + URL_LINE
        + "kpip==0.0.1\nshadowed==2.0\nZeta-Pkg==1.0\n"
    )


def test_exclude_editable_leaves_editables_out(site: Path, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _dist(
        site / "first",
        "editable_pkg-3.0.dist-info",
        "editable-pkg",
        "3.0",
        direct_url={"url": src.as_uri(), "dir_info": {"editable": True}},
    )

    assert _freeze(["--exclude-editable"]) == (
        "alpha==2.0.1\nempty==9\n" + URL_LINE + "shadowed==1.0\nZeta-Pkg==1.0\n"
    )
    assert "editable" in _freeze([])


def test_a_version_is_frozen_as_the_parser_spells_it(site: Path) -> None:
    _dist(site / "first", "pre-1.0rc1.dist-info", "pre", "1.0rc1")
    _dist(site / "first", "respelled-1.0-rc2.dist-info", "respelled", "1.0-rc2")

    lines = _freeze([]).splitlines()

    assert "pre==1.0rc1" in lines
    assert "respelled==1.0rc2" in lines


def test_quiet_changes_nothing(site: Path) -> None:
    """pip's freeze writes to stdout itself, so ``-q`` does not silence it."""
    assert _freeze(["-q"]) == _freeze([])
