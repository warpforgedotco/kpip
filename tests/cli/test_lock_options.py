"""pip's requirement options on ``lock``.

Each lock here was compared with what pip 26.2.1 locks from the same wheelhouse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.cli.parsers.lock import create_parser as lock_parser

from tests.cli.option_support import (
    APP,
    LIB,
    LIB_PRE,
    add_newer_sdist,
    build_wheelhouse,
    run,
)


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    return build_wheelhouse(tmp_path / "wheelhouse")


@pytest.fixture
def newer_sdist(wheelhouse: Path, tmp_path: Path) -> Path:
    return add_newer_sdist(wheelhouse, tmp_path / "staging")


class TestLock:
    def locked(self, wheelhouse: Path, tmp_path: Path, *args: str | Path) -> list[str]:
        output = tmp_path / "pylock.toml"

        assert run("lock", wheelhouse, *args, "-o", output) == 0

        return [
            line.split('"')[1]
            for line in output.read_text().splitlines()
            if line.startswith("url = ")
        ]

    def files(self, wheelhouse: Path, *filenames: str) -> list[str]:
        return [(wheelhouse / name).as_uri() for name in filenames]

    def test_no_deps(self, wheelhouse: Path, tmp_path: Path) -> None:
        assert self.locked(wheelhouse, tmp_path, "app", "--no-deps") == self.files(
            wheelhouse, APP
        )

    def test_only_deps(self, wheelhouse: Path, tmp_path: Path) -> None:
        assert self.locked(wheelhouse, tmp_path, "app", "--only-deps") == self.files(
            wheelhouse, LIB
        )

    def test_only_deps_of_a_project_directory(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        project.joinpath("pyproject.toml").write_text(
            '[project]\nname = "project"\nversion = "1"\ndependencies = ["app"]\n'
        )

        assert self.locked(wheelhouse, tmp_path, project, "--only-deps") == self.files(
            wheelhouse, APP, LIB
        )

    @pytest.mark.parametrize(
        "control, lib",
        [
            (["--pre"], LIB_PRE),
            (["--all-releases", "lib"], LIB_PRE),
            (["--all-releases", "lib", "--only-final", ":all:"], LIB),
        ],
    )
    def test_release_control(
        self, wheelhouse: Path, tmp_path: Path, control: list[str], lib: str
    ) -> None:
        assert self.locked(wheelhouse, tmp_path, "app", *control) == self.files(
            wheelhouse, APP, lib
        )

    @pytest.mark.parametrize(
        "selection", [["--prefer-binary"], ["--only-binary", ":all:"]]
    )
    def test_a_wheel_is_locked_over_a_newer_source_distribution(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path, selection: list[str]
    ) -> None:
        assert self.locked(wheelhouse, tmp_path, "app", *selection) == self.files(
            wheelhouse, APP, LIB
        )

    def test_a_dependency_group(
        self, wheelhouse: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_path.joinpath("pyproject.toml").write_text(
            '[dependency-groups]\ndev = ["lib"]\n'
        )
        monkeypatch.chdir(tmp_path)

        assert self.locked(wheelhouse, tmp_path, "--group", "dev") == self.files(
            wheelhouse, LIB
        )

    def test_a_script(self, wheelhouse: Path, tmp_path: Path) -> None:
        script = tmp_path / "script.py"
        script.write_text('# /// script\n# dependencies = ["lib"]\n# ///\n')

        assert self.locked(
            wheelhouse, tmp_path, "--requirements-from-script", script
        ) == self.files(wheelhouse, LIB)

    @pytest.mark.parametrize(
        "given",
        [
            ["--index-url", "https://example.invalid/simple"],
            ["--pypi-url", "https://example.invalid/simple"],
            ["--extra-index-url", "https://example.invalid/simple"],
            ["--pre"],
            ["--only-final", "lib"],
            ["--no-deps"],
            ["--only-deps"],
            ["--prefer-binary"],
            ["--only-binary", ":all:"],
            ["--ignore-requires-python"],
            ["--uploaded-prior-to", "P3D"],
            ["--refresh-package", "lib"],
            ["-C", "key=value"],
            ["--build-constraint", "constraints.txt"],
        ],
    )
    def test_a_lock_given_an_option_the_record_does_not_key_is_not_replayed(
        self, given: list[str]
    ) -> None:
        from kpip.cli.lock import lock_replay_key, resolves_as_recorded

        options = lock_parser().parse_args(["lib", *given])

        assert not resolves_as_recorded(options)
        assert lock_replay_key(options, "/cache", None) is None

    def test_a_configured_index_is_not_replayed_as_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kpip.cli.lock import lock_replay_key, resolves_as_recorded

        monkeypatch.setenv("KPIP_INDEX_URL", "https://example.invalid/simple")
        options = lock_parser().parse_args(["lib"])

        assert not resolves_as_recorded(options)
        assert lock_replay_key(options, "/cache", None) is None

    def test_configured_sources_are_locked_from(
        self, wheelhouse: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KPIP_NO_INDEX`` and ``KPIP_FIND_LINKS`` reach the lock, as pip's do."""
        from kpip.cli.main import main

        monkeypatch.setenv("KPIP_NO_INDEX", "1")
        monkeypatch.setenv("KPIP_FIND_LINKS", str(wheelhouse))
        output = tmp_path / "pylock.toml"

        assert main(["lock", "--no-cache-dir", "app", "-o", str(output)]) == 0
        assert [
            line.split('"')[1]
            for line in output.read_text().splitlines()
            if line.startswith("url = ")
        ] == [(wheelhouse / name).as_uri() for name in (APP, LIB)]

    def test_a_plain_lock_is_still_replayed(self) -> None:
        from kpip.cli.lock import resolves_as_recorded

        assert resolves_as_recorded(lock_parser().parse_args(["lib"]))
