"""What ``list`` prints for a site directory, format by format."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from kpip.cli.list import run_list


def _dist(
    root: Path,
    dirname: str,
    name: str,
    version: str,
    *,
    build: str | None = None,
    editable_dir: Path | None = None,
    installer: str | None = None,
) -> Path:
    info = root / dirname
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    if build is not None:
        (info / "WHEEL").write_text(
            f"Wheel-Version: 1.0\nBuild: {build}\nTag: py3-none-any\n"
        )
    if editable_dir is not None:
        (info / "direct_url.json").write_text(
            json.dumps({"url": editable_dir.as_uri(), "dir_info": {"editable": True}})
        )
    if installer is not None:
        (info / "INSTALLER").write_text(f"{installer}\n")
    return info


def _list(args: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        assert run_list(args) == 0
    return out.getvalue()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    site = tmp_path / "site"
    src = tmp_path / "src" / "editable-pkg"
    src.mkdir(parents=True)
    _dist(
        site, "Zeta_Pkg-1.0.dist-info", "Zeta-Pkg", "1.0", build="7", installer="kpip"
    )
    _dist(
        site,
        "alpha-2.0.1.dist-info",
        "alpha",
        "2.0.1",
        editable_dir=src,
        installer="uv",
    )
    _dist(site, "Mid-0.3.dist-info", "mid", "0.3")
    _dist(site, "argparse-1.4.0.dist-info", "argparse", "1.4.0")
    (site / "empty-9.dist-info").mkdir()
    (site / "notes.txt").write_text("x")
    monkeypatch.setattr(sys, "path", [str(tmp_path / "missing"), str(site)])
    monkeypatch.delenv("KPIP_TARGET_PREFIX", raising=False)
    return site


def test_columns_rule_off_the_header_as_pip_does(site: Path) -> None:
    source = site.parent / "src" / "editable-pkg"
    location = "Editable project location"
    width = max(len(location), len(str(source)))

    assert _list([]) == (
        f"Package  Version Build {location}\n"
        f"-------- ------- ----- {'-' * width}\n"
        f"alpha    2.0.1         {source}\n"
        "mid      0.3\n"
        "Zeta-Pkg 1.0     7\n"
    )
    assert _list(["--path", str(site)]) == _list([])


def test_verbose_columns_add_location_and_installer(site: Path) -> None:
    header, rule, *rows = _list(["-v"]).splitlines()

    assert header.split()[-2:] == ["Location", "Installer"]
    assert set(rule) == {"-", " "}
    assert [row.split()[0] for row in rows] == ["alpha", "mid", "Zeta-Pkg"]
    assert rows[0].endswith(f"{site} uv")
    assert rows[1].endswith(str(site))
    assert rows[2].endswith(f"{site} kpip")


def test_json_lists_each_distribution(site: Path) -> None:
    source = str(site.parent / "src" / "editable-pkg")

    assert json.loads(_list(["--format=json"])) == [
        {"name": "alpha", "version": "2.0.1", "editable_project_location": source},
        {"name": "mid", "version": "0.3"},
        {"name": "Zeta-Pkg", "version": "1.0"},
    ]
    assert json.loads(_list(["--format", "json", "-v"])) == [
        {
            "name": "alpha",
            "version": "2.0.1",
            "location": str(site),
            "installer": "uv",
            "editable_project_location": source,
        },
        {"name": "mid", "version": "0.3", "location": str(site), "installer": ""},
        {
            "name": "Zeta-Pkg",
            "version": "1.0",
            "location": str(site),
            "installer": "kpip",
        },
    ]


def test_freeze_format_lists_pins(site: Path) -> None:
    assert _list(["--format=freeze"]) == "alpha==2.0.1\nmid==0.3\nZeta-Pkg==1.0\n"
    assert _list(["--format=freeze", "-v"]) == (
        f"alpha==2.0.1 ({site})\nmid==0.3 ({site})\nZeta-Pkg==1.0 ({site})\n"
    )


def test_exclude_takes_any_spelling_of_a_name(site: Path) -> None:
    assert _list(["--exclude", "Zeta-pkg", "--exclude", "alpha"]) == (
        "Package Version\n------- -------\nmid     0.3\n"
    )


def test_a_version_is_shown_as_written_in_columns_and_normalized_elsewhere(
    site: Path,
) -> None:
    """pip prints the raw version in columns and the parsed one in the
    machine-readable formats."""
    _dist(site, "respelled-1.0-rc2.dist-info", "respelled", "1.0-rc2")

    assert "respelled 1.0-rc2\n" in _list([])
    assert "respelled==1.0rc2\n" in _list(["--format=freeze"])
    assert {"name": "respelled", "version": "1.0rc2"} in json.loads(
        _list(["--format=json"])
    )


def test_nothing_to_list_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip prints no header over no rows."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "path", [str(empty)])

    assert _list([]) == ""
    assert _list(["--format=json"]) == "[]\n"


def test_pip_exclusion_covers_kpip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip.core.kpip_version import KPIP_DISTRIBUTION_NAMES

    site = tmp_path / "site"
    for name in (*KPIP_DISTRIBUTION_NAMES, "pip", "keep"):
        _dist(site, f"{name.replace('-', '_')}-1.0.dist-info", name, "1.0")
    monkeypatch.setattr(sys, "path", [str(site)])
    monkeypatch.delenv("KPIP_TARGET_PREFIX", raising=False)

    assert _list(["--exclude", "pip"]) == (
        "Package Version\n------- -------\nkeep    1.0\n"
    )
