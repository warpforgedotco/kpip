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

    monkeypatch.setattr(
        install.ResolutionEngine, "resolve_serving_stale_pages", unexpected
    )
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
