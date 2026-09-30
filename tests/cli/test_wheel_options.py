"""pip's requirement options on ``wheel``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cli.option_support import (
    APP,
    LIB,
    LIB_PRE,
    add_newer_sdist,
    build_wheelhouse,
    names,
    run,
)


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    return build_wheelhouse(tmp_path / "wheelhouse")


@pytest.fixture
def newer_sdist(wheelhouse: Path, tmp_path: Path) -> Path:
    return add_newer_sdist(wheelhouse, tmp_path / "staging")


class TestWheel:
    def test_wheels_are_collected_for_the_requirement_and_its_dependencies(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        assert run("wheel", wheelhouse, "app", "-w", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, LIB]

    def test_only_deps_leaves_what_was_named(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        assert (
            run("wheel", wheelhouse, "app", "--only-deps", "-w", tmp_path / "out") == 0
        )

        assert names(tmp_path / "out") == [LIB]

    def test_a_source_distribution_is_built(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        assert run("wheel", wheelhouse, "app", "-w", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, "lib-1.5-py3-none-any.whl"]

    def test_prefer_binary_takes_the_wheel_there_is(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"

        assert run("wheel", wheelhouse, "app", "--prefer-binary", "-w", out) == 0

        assert names(out) == [APP, LIB]

    def test_pre_takes_the_prerelease(self, wheelhouse: Path, tmp_path: Path) -> None:
        assert run("wheel", wheelhouse, "app", "--pre", "-w", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, LIB_PRE]
