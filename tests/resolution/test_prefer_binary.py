"""``--prefer-binary`` decides which release is chosen, not only which file."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine

from tests.wheel_helpers import make_sdist, make_wheel


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    """``lib`` as wheels of 1.0 and 0.9 and a source distribution of 1.5."""
    house = tmp_path / "wheelhouse"
    house.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    make_wheel(house, "lib", "lib", "0.9")
    make_wheel(house, "lib", "lib", "1.0")
    shutil.copy(
        make_sdist(staging, "lib", "lib", "1.5", standalone_backend=True), house
    )
    return house


def resolved(wheelhouse: Path, requirement: str, *, prefer_binary: bool) -> str:
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
            prefer_binary=prefer_binary,
        ),
        ignore_installed=True,
    )

    try:
        (candidate,) = engine.resolve([requirement]).candidates
    finally:
        engine.close()

    return str(candidate.version)


def test_the_newest_release_is_chosen_whatever_its_format(wheelhouse: Path) -> None:
    assert resolved(wheelhouse, "lib", prefer_binary=False) == "1.5"


def test_a_release_with_a_wheel_is_chosen_over_a_newer_one_without(
    wheelhouse: Path,
) -> None:
    assert resolved(wheelhouse, "lib", prefer_binary=True) == "1.0"


def test_a_release_without_a_wheel_is_chosen_when_no_other_matches(
    wheelhouse: Path,
) -> None:
    assert resolved(wheelhouse, "lib>1", prefer_binary=True) == "1.5"
