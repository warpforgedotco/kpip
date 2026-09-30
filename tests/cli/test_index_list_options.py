"""pip's index and selection options on ``index`` and ``list``.

Each output here was compared with pip 26.2.1 run on the same wheelhouse.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from kpip.cli.main import main

from tests.wheel_helpers import make_sdist, make_wheel


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    """``lib`` as wheels of 0.9, 1.0 and 2.0rc1 and a source distribution
    of 1.5."""
    house = tmp_path / "wheelhouse"
    house.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    for version in ("0.9", "1.0", "2.0rc1"):
        make_wheel(house, "lib", "lib", version)
    shutil.copy(
        make_sdist(staging, "lib", "lib", "1.5", standalone_backend=True), house
    )
    return house


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A library with ``lib`` 0.9 installed."""
    directory = tmp_path / "site"
    dist_info = directory / "lib-0.9.dist-info"
    dist_info.mkdir(parents=True)
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: lib\nVersion: 0.9\n"
    )
    dist_info.joinpath("INSTALLER").write_text("kpip\n")
    return directory


class TestIndexVersions:
    def versions(
        self, wheelhouse: Path, capsys: pytest.CaptureFixture[str], *args: str
    ) -> list[str]:
        given = ["--no-index", "--find-links", str(wheelhouse), "--json", *args]

        assert main(["index", "versions", "lib", *given]) == 0

        return json.loads(capsys.readouterr().out)["versions"]

    @pytest.mark.parametrize(
        "args, versions",
        [
            ([], ["1.5", "1.0", "0.9"]),
            (["--pre"], ["2.0rc1", "1.5", "1.0", "0.9"]),
            (["--all-releases", "lib"], ["2.0rc1", "1.5", "1.0", "0.9"]),
            (["--all-releases", ":all:", "--only-final", "lib"], ["1.5", "1.0", "0.9"]),
            (["--only-binary", ":all:"], ["1.0", "0.9"]),
            (["--no-binary", ":all:"], ["1.5"]),
            (["--no-binary", ":all:", "--only-binary", "lib"], ["1.0", "0.9"]),
            (["--prefer-binary"], ["1.5", "1.0", "0.9"]),
            (
                ["--python-version", "3.12", "--platform", "manylinux2014_x86_64"],
                ["1.5", "1.0", "0.9"],
            ),
        ],
    )
    def test_the_versions_are_those_the_options_select(
        self,
        wheelhouse: Path,
        capsys: pytest.CaptureFixture[str],
        args: list[str],
        versions: list[str],
    ) -> None:
        assert self.versions(wheelhouse, capsys, *args) == versions

    def test_find_links_has_pips_short_spelling(
        self, wheelhouse: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            main(["index", "versions", "lib", "--no-index", "-f", str(wheelhouse)]) == 0
        )

        assert capsys.readouterr().out == (
            "lib (1.5)\nAvailable versions: 1.5, 1.0, 0.9\n"
        )

    def test_a_project_with_nothing_to_list_is_an_error(
        self, wheelhouse: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        given = ["--no-index", "--find-links", str(wheelhouse)]

        assert main(["index", "versions", "absent", *given]) != 0

        assert "No matching distribution found for absent" in capsys.readouterr().err

    def test_pre_cannot_go_with_release_control(
        self, wheelhouse: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["index", "versions", "lib", "--pre", "--only-final", "lib"]) != 0

        assert (
            "--pre cannot be used with --all-releases or --only-final"
            in capsys.readouterr().err
        )


class TestListOutdated:
    def latest(
        self,
        wheelhouse: Path,
        site: Path,
        capsys: pytest.CaptureFixture[str],
        *args: str,
    ) -> tuple[str, str]:
        given = ["--no-index", "--find-links", str(wheelhouse), "--format", "json"]

        assert main(["list", "--path", str(site), "--outdated", *given, *args]) == 0

        (row,) = json.loads(capsys.readouterr().out)

        return row["latest_version"], row["latest_filetype"]

    @pytest.mark.parametrize(
        "args, latest",
        [
            ([], ("1.5", "sdist")),
            (["--pre"], ("2.0rc1", "wheel")),
            (["--all-releases", "lib"], ("2.0rc1", "wheel")),
            (["--prefer-binary"], ("1.0", "wheel")),
            (["--only-binary", ":all:"], ("1.0", "wheel")),
            (["--no-binary", "lib"], ("1.5", "sdist")),
        ],
    )
    def test_the_latest_is_what_the_options_would_install(
        self,
        wheelhouse: Path,
        site: Path,
        capsys: pytest.CaptureFixture[str],
        args: list[str],
        latest: tuple[str, str],
    ) -> None:
        assert self.latest(wheelhouse, site, capsys, *args) == latest

    def test_pre_cannot_go_with_release_control(
        self, site: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["list", "--path", str(site), "--pre", "--only-final", "lib"]) != 0

        assert (
            "--pre cannot be used with --all-releases or --only-final"
            in capsys.readouterr().err
        )
