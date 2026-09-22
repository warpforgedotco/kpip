"""A VCS URL is cloned once per process and its candidate learned once.

Resolving a VCS requirement used to clone and build it three times: to
learn its name and version when listed, again when its release was chosen,
and again for its metadata, each caller removing its checkout. Callers now
share one checkout, hand it back with ``release_checkout``, and the process
removes it at exit; the candidate itself is memoized per URL.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from kpip.index import candidates, vcs
from kpip.index.links import Link


@pytest.fixture
def repo(tmp_path: Path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "shared-demo"\nversion = "1.2.3"\n'
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
    )
    (root / "shared_demo.py").write_text("")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(command, cwd=root, check=True, env=env, capture_output=True)
    return f"git+file://{root}"


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcs, "_shared_checkouts", {})
    monkeypatch.setattr(candidates, "_vcs_candidates", {})


def test_the_checkout_is_shared_and_survives_release(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clones: list[str] = []
    real = vcs._clone_vcs

    def counting(url: str, **kwargs: Any) -> str:
        clones.append(url)
        return real(url, **kwargs)

    monkeypatch.setattr(vcs, "_clone_vcs", counting)

    first = vcs.materialize_vcs(repo, emit_resolution=False)
    vcs.release_checkout(first)
    second = vcs.materialize_vcs(repo, emit_resolution=False)

    assert second == first
    assert os.path.isdir(first)
    assert clones == [repo]

    vcs._remove_shared_checkouts()
    assert not os.path.exists(first)


def test_a_private_checkout_is_removed_on_release(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()

    vcs.release_checkout(os.fspath(private))

    assert not private.exists()


def test_the_vcs_candidate_is_learned_once(
    repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[str] = []
    from kpip.build import build_backend

    real = build_backend.prepare_project_metadata

    def counting(path: str, *args: Any, **kwargs: Any) -> Any:
        builds.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(build_backend, "prepare_project_metadata", counting)
    link = Link.from_url(repo, source_url=None)

    first = candidates.InstallationCandidate.from_vcs(link)
    second = candidates.InstallationCandidate.from_vcs(link)

    assert isinstance(first, candidates.InstallationCandidate)
    assert (first.name, str(first.version)) == ("shared-demo", "1.2.3")
    assert second is first
    assert len(builds) == 1
