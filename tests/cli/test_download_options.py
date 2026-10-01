"""pip's requirement options on ``download``.

Each behaviour here was compared with pip 26.2.1 run on the same wheelhouse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cli.option_support import (
    APP,
    LIB,
    LIB_PRE,
    LIB_SDIST,
    add_newer_sdist,
    build_wheelhouse,
    names,
    run,
    sha256,
)


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    return build_wheelhouse(tmp_path / "wheelhouse")


@pytest.fixture
def newer_sdist(wheelhouse: Path, tmp_path: Path) -> Path:
    return add_newer_sdist(wheelhouse, tmp_path / "staging")


class TestDownload:
    def test_a_requirement_comes_with_what_it_depends_on(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        assert run("download", wheelhouse, "app", "-d", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, LIB]

    def test_no_deps_leaves_the_dependencies(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        assert (
            run("download", wheelhouse, "app", "--no-deps", "-d", tmp_path / "out") == 0
        )

        assert names(tmp_path / "out") == [APP]

    @pytest.mark.parametrize("spelling", ["--only-deps", "--only-dependencies"])
    def test_only_deps_leaves_what_was_named(
        self, wheelhouse: Path, tmp_path: Path, spelling: str
    ) -> None:
        assert run("download", wheelhouse, "app", spelling, "-d", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [LIB]

    def test_only_deps_leaves_a_named_dependency_out_too(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"

        assert run("download", wheelhouse, "app", "lib", "--only-deps", "-d", out) == 0

        assert not out.exists() or names(out) == []

    @pytest.mark.parametrize(
        "conflict, named",
        [
            (["--no-deps"], "'--no-deps'"),
            (["--group", "dev"], "'--group'"),
            (["--no-deps", "--group", "dev"], "'--no-deps', or '--group'"),
        ],
    )
    def test_only_deps_goes_with_nothing_that_names_no_requirement(
        self,
        wheelhouse: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        conflict: list[str],
        named: str,
    ) -> None:
        assert run("download", wheelhouse, "app", "--only-deps", *conflict) != 0

        assert (
            f"Cannot use '--only-dependencies' in combination with {named}."
            in capsys.readouterr().err
        )

    def test_pre_takes_the_prerelease(self, wheelhouse: Path, tmp_path: Path) -> None:
        assert run("download", wheelhouse, "app", "--pre", "-d", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, LIB_PRE]

    @pytest.mark.parametrize(
        "control, lib",
        [
            (["--all-releases", "lib"], LIB_PRE),
            (["--all-releases", ":all:", "--only-final", "lib"], LIB),
            (["--only-final", "lib", "--all-releases", "lib"], LIB_PRE),
            (["--all-releases", "lib", "--only-final", ":all:"], LIB),
            (["--only-final", "lib", "--all-releases", ":all:"], LIB_PRE),
        ],
    )
    def test_release_control_is_read_in_the_order_given(
        self, wheelhouse: Path, tmp_path: Path, control: list[str], lib: str
    ) -> None:
        assert run("download", wheelhouse, "app", *control, "-d", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, lib]

    def test_pre_cannot_go_with_release_control(
        self, wheelhouse: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("download", wheelhouse, "app", "--pre", "--only-final", "lib") != 0

        assert (
            "--pre cannot be used with --all-releases or --only-final"
            in capsys.readouterr().err
        )

    def test_the_newest_release_is_taken_whatever_its_format(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        assert run("download", wheelhouse, "app", "-d", tmp_path / "out") == 0

        assert names(tmp_path / "out") == [APP, LIB_SDIST]

    @pytest.mark.parametrize(
        "selection",
        [["--prefer-binary"], ["--only-binary", ":all:"], ["--only-binary", "lib"]],
    )
    def test_a_wheel_is_taken_over_a_newer_source_distribution(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path, selection: list[str]
    ) -> None:
        assert (
            run("download", wheelhouse, "app", *selection, "-d", tmp_path / "out") == 0
        )

        assert names(tmp_path / "out") == [APP, LIB]

    def test_prefer_binary_from_a_requirements_file(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        """pip 26.2.1 takes the flag from a file as from the command line."""
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("--prefer-binary\napp\n")

        assert (
            run("download", wheelhouse, "-r", requirements, "-d", tmp_path / "out") == 0
        )

        assert names(tmp_path / "out") == [APP, LIB]

    def test_no_binary_takes_the_source_distribution(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"

        assert run("download", wheelhouse, "lib", "--no-binary", "lib", "-d", out) == 0

        assert names(out) == [LIB_SDIST]

    def test_format_control_is_read_in_the_order_given(
        self, wheelhouse: Path, newer_sdist: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        control = ["--no-binary", ":all:", "--only-binary", "lib"]

        assert run("download", wheelhouse, "lib", *control, "-d", out) == 0

        assert names(out) == [LIB]

    def test_another_interpreter_rules_source_distributions_out(
        self, wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "out"

        assert run("download", wheelhouse, "app", "--python-version", "3.12") != 0
        assert (
            "either --no-deps must be set, or --only-binary=:all: must be set"
            in capsys.readouterr().err
        )

        restricted = ["--python-version", "3.12", "--only-binary=:all:"]
        assert run("download", wheelhouse, "app", *restricted, "-d", out) == 0
        assert names(out) == [APP, LIB]

    @pytest.mark.parametrize(
        "spelling", ["-d", "--dest", "--destination-dir", "--destination-directory"]
    )
    def test_the_destination_has_pips_spellings(
        self, wheelhouse: Path, tmp_path: Path, spelling: str
    ) -> None:
        assert run("download", wheelhouse, "lib", spelling, tmp_path / "out") == 0

        assert names(tmp_path / "out") == [LIB]

    def test_the_destination_is_the_working_directory_unless_given(
        self, wheelhouse: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cwd").mkdir()
        monkeypatch.chdir(tmp_path / "cwd")

        assert run("download", wheelhouse, "lib") == 0

        assert names(tmp_path / "cwd") == [LIB]

    def test_a_script_names_its_requirements(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        script = tmp_path / "script.py"
        script.write_text('# /// script\n# dependencies = ["app"]\n# ///\n')

        assert (
            run(
                "download",
                wheelhouse,
                "--requirements-from-script",
                script,
                "-d",
                tmp_path / "out",
            )
            == 0
        )

        assert names(tmp_path / "out") == [APP, LIB]

    def test_a_script_for_another_python_is_refused_unless_told_to_ignore_it(
        self, wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = tmp_path / "script.py"
        script.write_text(
            '# /// script\n# requires-python = "<3"\n# dependencies = ["lib"]\n# ///\n'
        )
        given = ["--requirements-from-script", script, "-d", tmp_path / "out"]

        assert run("download", wheelhouse, *given) != 0
        assert "requires a different Python" in capsys.readouterr().err

        assert run("download", wheelhouse, *given, "--ignore-requires-python") == 0
        assert names(tmp_path / "out") == [LIB]


class TestHashes:
    def requirements(self, tmp_path: Path, *lines: str) -> Path:
        path = tmp_path / "requirements.txt"
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_require_hashes_refuses_a_requirement_without_one(
        self, wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("download", wheelhouse, "lib==1.0", "--require-hashes") != 0

        assert "--require-hashes mode" in capsys.readouterr().err

    def test_a_hash_on_one_requirement_is_required_of_all(
        self, wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        requirements = self.requirements(
            tmp_path,
            f"lib==1.0 --hash=sha256:{sha256(wheelhouse / LIB)}",
            "app==1.0",
        )

        assert run("download", wheelhouse, "-r", requirements) != 0

        assert "--require-hashes mode" in capsys.readouterr().err

    def test_no_require_hashes_checks_only_the_hashes_given(
        self, wheelhouse: Path, tmp_path: Path
    ) -> None:
        requirements = self.requirements(
            tmp_path,
            f"lib==1.0 --hash=sha256:{sha256(wheelhouse / LIB)}",
            "app==1.0",
        )
        out = tmp_path / "out"

        assert (
            run(
                "download",
                wheelhouse,
                "-r",
                requirements,
                "--no-require-hashes",
                "-d",
                out,
            )
            == 0
        )

        assert names(out) == [APP, LIB]

    def test_no_require_hashes_still_refuses_a_wrong_hash(
        self, wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        requirements = self.requirements(
            tmp_path, f"lib==1.0 --hash=sha256:{'0' * 64}", "app==1.0"
        )

        assert (
            run("download", wheelhouse, "-r", requirements, "--no-require-hashes") != 0
        )

        assert "DO NOT MATCH THE HASHES" in capsys.readouterr().err

    def test_the_two_cannot_go_together(
        self, wheelhouse: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        both = ["--require-hashes", "--no-require-hashes"]

        assert run("download", wheelhouse, "lib", *both) != 0

        assert (
            "--require-hashes and --no-require-hashes are mutually exclusive"
            in capsys.readouterr().err
        )
