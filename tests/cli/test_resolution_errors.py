"""A resolve that finds nothing to install says what pip says, then why.

Each ``ERROR:`` line was compared with pip 26.2.1 on the same wheelhouse;
the ``hint:`` lines are kpip's own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.cli.main import main

from tests.wheel_helpers import make_wheel


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    house = tmp_path / "wheelhouse"
    house.mkdir()
    make_wheel(house, "parent-pkg", "parent_pkg", "1.0", requires=["child-pkg==2.0"])
    make_wheel(house, "child-pkg", "child_pkg", "1.0")
    make_wheel(house, "newpy", "newpy", "1.0", requires_python=">=3.99")
    make_wheel(house, "left-pkg", "left_pkg", "1.0", requires=["shared-pkg==1.0"])
    make_wheel(house, "right-pkg", "right_pkg", "1.0", requires=["shared-pkg==2.0"])
    make_wheel(house, "shared-pkg", "shared_pkg", "1.0")
    make_wheel(house, "shared-pkg", "shared_pkg", "2.0")
    return house


def failure(
    wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], *args: str
) -> list[str]:
    status = main(
        [
            "download",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "-d",
            str(tmp_path / "out"),
            *args,
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    return [
        line
        for line in (captured.err + captured.out).splitlines()
        if line.startswith(
            ("ERROR:", "hint:", "    ", "The conflict", "To fix", "1.", "2.")
        )
    ]


def test_a_release_nothing_has(
    wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert failure(wheelhouse, tmp_path, capsys, "shared-pkg==9.0") == [
        "ERROR: Could not find a version that satisfies the requirement "
        "shared-pkg==9.0 (from versions: 1.0, 2.0)",
        "ERROR: No matching distribution found for shared-pkg==9.0",
        "hint: no release of shared-pkg matches ==9.0; the newest is 2.0",
    ]


def test_a_dependency_nothing_satisfies_names_its_parent(
    wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert failure(wheelhouse, tmp_path, capsys, "parent-pkg") == [
        "ERROR: Could not find a version that satisfies the requirement "
        "child-pkg==2.0 (from parent-pkg) (from versions: 1.0)",
        "ERROR: No matching distribution found for child-pkg==2.0",
        "hint: no release of child-pkg matches ==2.0; the newest is 1.0",
    ]


def test_a_release_for_another_python(
    wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = failure(wheelhouse, tmp_path, capsys, "newpy")

    assert len(lines) == 1
    assert lines[0].startswith("ERROR: Package 'newpy' requires a different Python: ")
    assert lines[0].endswith(" not in '>=3.99'")


def test_two_parents_that_conflict(
    wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = failure(wheelhouse, tmp_path, capsys, "left-pkg", "right-pkg")

    # As pip's: the errors on stderr, the explanation on stdout.
    assert lines == [
        "ERROR: Cannot install left-pkg==1.0 and right-pkg==1.0 because these "
        "package versions have conflicting dependencies.",
        "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/"
        "topics/dependency-resolution/#dealing-with-dependency-conflicts",
        "The conflict is caused by:",
        "    left-pkg 1.0 depends on shared-pkg==1.0",
        "    right-pkg 1.0 depends on shared-pkg==2.0",
        "    shared-pkg",
        "hint: the releases of shared-pkg that fit are 1.0, 2.0",
        "To fix this you could try to:",
        "1. loosen the range of package versions you've specified",
        "2. remove package versions to allow pip to attempt to solve the "
        "dependency conflict",
    ]


def test_a_user_requirement_that_conflicts_is_the_users(
    wheelhouse: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = failure(wheelhouse, tmp_path, capsys, "left-pkg", "shared-pkg==2.0")

    assert lines[0] == (
        "ERROR: Cannot install left-pkg==1.0 and shared-pkg==2.0 because these "
        "package versions have conflicting dependencies."
    )
    assert "    The user requested shared-pkg==2.0" in lines
    assert "    left-pkg 1.0 depends on shared-pkg==1.0" in lines


def test_no_index_and_no_find_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["download", "--no-index", "-d", str(tmp_path), "six==1.17.0"])

    assert status == 1
    err = capsys.readouterr().err
    assert (
        "ERROR: Could not find a version that satisfies the requirement "
        "six==1.17.0 (from versions: none)"
    ) in err
    assert "ERROR: No matching distribution found for six==1.17.0" in err
    assert "hint: --no-index was given without --find-links" in err


def test_a_constrained_project_is_named_from_its_pyproject_unbuilt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version pyproject.toml states names the project without a build."""
    from kpip.cli import resolution_errors

    def no_build(*args: object, **kwargs: object) -> None:
        raise AssertionError("the report built the project")

    monkeypatch.setattr(resolution_errors, "prepare_project_metadata", no_build)
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "local-pkg"\nversion = "1.0"\n'
    )
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("local-pkg==2.0\n")

    status = main(
        [
            "download",
            "--no-index",
            "-c",
            str(constraints),
            "-d",
            str(tmp_path / "out"),
            str(project),
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert f"Cannot install local-pkg 1.0 (from {project})" in captured.err
    assert (
        f"hint: {project} is local-pkg 1.0, and the constraint local-pkg==2.0 "
        "rules it out"
    ) in captured.out
