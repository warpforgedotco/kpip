"""An install of what the environment already has resolves nothing."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from kpip.cli import install
from kpip.cli.install import run_install
from kpip.resolution.api import ResolutionEngine


def _dist(root: Path, dirname: str, name: str, version: str) -> None:
    info = root / dirname
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "site"
    _dist(root, "simple-2.0.0.dist-info", "simple", "2.0.0")
    _dist(root, "other-1.5.dist-info", "other", "1.5")
    monkeypatch.setattr(sys, "path", [str(root)])
    monkeypatch.delenv("KPIP_TARGET_PREFIX", raising=False)
    monkeypatch.setenv("KPIP_NO_CACHE_DIR", "1")

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("resolved requirements the environment already satisfies")

    monkeypatch.setattr(ResolutionEngine, "resolve_serving_stale_pages", unexpected)
    monkeypatch.setattr(install, "create_candidate_provider", unexpected)

    # The command leaves its source options in the environment, for the
    # builds it starts, and lowers the log level when quiet.
    environment = dict(os.environ)
    level = logging.getLogger().level

    yield root

    os.environ.clear()
    os.environ.update(environment)
    logging.getLogger().setLevel(level)


def test_satisfied_requirements_are_reported_and_nothing_is_resolved(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--no-index", "-f", "/wh", "simple", "Other>=1.2", "simple==2"]

    assert run_install(args) == 0
    assert capsys.readouterr().out == (
        "Looking in links: /wh\n"
        "Requirement already satisfied: simple\n"
        "Requirement already satisfied: Other>=1.2\n"
        "Requirement already satisfied: simple==2\n"
    )


def test_a_requirement_given_twice_is_reported_once(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """As pip reports it."""
    assert run_install(["--no-index", "simple", "simple"]) == 0
    assert capsys.readouterr().out == "Requirement already satisfied: simple\n"


def test_quiet_reports_nothing(site: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_install(["--no-index", "-q", "simple"]) == 0
    assert capsys.readouterr().out == ""


def _requires(root: Path, dirname: str, *requirements: str) -> None:
    with (root / dirname / "METADATA").open("a") as metadata:
        for requirement in requirements:
            metadata.write(f"Requires-Dist: {requirement}\n")


def _unmet(*names: str) -> dict[str, str]:
    from kpip.core.metadata import clear_installed_index, installed_index

    clear_installed_index()
    installed = installed_index()

    return install.unmet_dependencies(
        installed, [(installed[name], frozenset()) for name in names]
    )


def test_a_missing_dependency_of_a_satisfied_requirement_is_wanted(site: Path) -> None:
    """pip installs what a satisfied requirement needs and lacks."""
    _requires(site, "simple-2.0.0.dist-info", "absent>=1.0", "other")

    assert _unmet("simple") == {"absent": "absent>=1.0"}


def test_a_dependency_installed_in_a_version_ruled_out_is_wanted(site: Path) -> None:
    _requires(site, "simple-2.0.0.dist-info", "other>=2")

    assert _unmet("simple") == {"other": "other>=2"}


def test_dependencies_are_followed_through_what_is_installed(site: Path) -> None:
    _requires(site, "simple-2.0.0.dist-info", "other")
    _requires(site, "other-1.5.dist-info", "deep<3", "simple")

    assert _unmet("simple") == {"deep": "deep<3"}


def test_a_dependency_whose_marker_does_not_apply_is_not_wanted(site: Path) -> None:
    _requires(
        site,
        "simple-2.0.0.dist-info",
        'absent; sys_platform == "no-such-platform"',
        'extra-only; extra == "more"',
    )

    assert _unmet("simple") == {}


def test_an_install_resolves_the_missing_dependency_alone(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip.core.metadata import clear_installed_index

    _requires(site, "simple-2.0.0.dist-info", "absent>=1.0")
    clear_installed_index()
    kept = install.filter_already_satisfied_requirements

    asked: list[list[str]] = []

    def recording(requirements, outcome, **options):  # noqa: ANN001, ANN003, ANN202
        unresolved = kept(requirements, outcome, **options)
        asked.append([str(item.req) for item in unresolved])
        raise KeyboardInterrupt

    monkeypatch.setattr(install, "filter_already_satisfied_requirements", recording)

    with pytest.raises(KeyboardInterrupt):
        run_install(["--no-index", "simple"])

    with pytest.raises(KeyboardInterrupt):
        run_install(["--no-index", "--no-deps", "simple"])

    assert asked == [["absent>=1.0"], []]
