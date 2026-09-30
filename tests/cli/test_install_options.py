"""pip's requirement options that ``install`` had not taken."""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.cli.parsers.install import create_parser as install_parser

from tests.cli.option_support import add_newer_sdist, build_wheelhouse, run


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    return build_wheelhouse(tmp_path / "wheelhouse")


@pytest.fixture
def newer_sdist(wheelhouse: Path, tmp_path: Path) -> Path:
    return add_newer_sdist(wheelhouse, tmp_path / "staging")


class TestInstall:
    def installed(self, target: Path) -> list[str]:
        return sorted(
            path.name.split("-")[0] + "-" + path.name.split("-")[1][: -len(".dist")]
            for path in target.glob("*.dist-info")
        )

    def test_only_deps_installs_what_the_requirement_needs(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"

        assert run("install", wheelhouse, "app", "--only-deps", "-t", target) == 0

        assert self.installed(target) == ["lib-1.0"]

    def test_only_deps_of_a_project_directory(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        project.joinpath("pyproject.toml").write_text(
            '[project]\nname = "project"\nversion = "1"\ndependencies = ["app"]\n'
        )
        target = tmp_path / "target"

        assert run("install", wheelhouse, project, "--only-deps", "-t", target) == 0

        assert self.installed(target) == ["app-1.0", "lib-1.0"]

    def test_prefer_binary(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"

        assert run("install", wheelhouse, "app", "--prefer-binary", "-t", target) == 0

        assert self.installed(target) == ["app-1.0", "lib-1.0"]

    def test_an_index_url_is_read_however_it_is_spelled(self) -> None:
        for given in (
            ["-i", "https://example.invalid/simple"],
            ["--index-url=https://example.invalid/simple"],
            ["--pypi-url", "https://example.invalid/simple"],
        ):
            options = install_parser().parse_args(["lib", *given])

            assert options.index_url == "https://example.invalid/simple"
