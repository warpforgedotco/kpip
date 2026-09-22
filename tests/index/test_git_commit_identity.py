"""A git source is identified by its commit, so its metadata persists.

A git requirement's identity used to be its bare URL, which proves nothing
across runs, so every lock cloned and built it. One ``git ls-remote``
resolves the reference to a commit without a clone; metadata persists under
that commit, and a later run reads the name, version and dependencies back
without cloning at all. A remote that moved between resolving and cloning
is not persisted under the stale commit.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index import candidate_materialization, candidates, vcs
from kpip.index.candidate_materialization import CandidateMaterializer
from kpip.index.links import Link
from kpip.index.source_models import CandidateRecord

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, env=ENV, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "commit-demo"\nversion = "2.0"\n'
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
    )
    (root / "commit_demo.py").write_text("")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "tag", "v2.0")
    return f"git+file://{root}", root


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcs, "_resolved_commits", {})
    monkeypatch.setattr(vcs, "_shared_checkouts", {})
    monkeypatch.setattr(candidates, "_vcs_candidates", {})


def test_a_reference_resolves_to_its_commit_without_a_clone(
    repo: tuple[str, Path],
) -> None:
    url, root = repo
    head = _git(root, "rev-parse", "HEAD")

    assert vcs.resolve_git_commit(url) == head
    assert vcs.resolve_git_commit(f"{url}@main") == head
    assert vcs.resolve_git_commit(f"{url}@v2.0") == head
    assert vcs.resolve_git_commit(f"{url}@{head.upper()}") == head
    assert vcs.resolve_git_commit(f"{url}@no-such-ref") is None
    assert vcs.resolve_git_commit("git+file:///nonexistent/repo.git") is None


def _materializer(tmp_path: Path) -> CandidateMaterializer:
    return CandidateMaterializer(
        build_options=None,
        build_constraints=(),
        wheel_cache_dir=os.fspath(tmp_path / "wheels"),
        target_key="t",
        build_isolation=False,
        dry_run=False,
        compute_source_hashes=False,
        session=SimpleNamespace(auth=SimpleNamespace(prompting=True), cache=None),
    )


def test_a_second_run_learns_the_candidate_and_its_metadata_without_cloning(
    repo: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, root = repo
    link = Link.from_url(url, source_url=None)

    first = _materializer(tmp_path)
    candidate = candidates.InstallationCandidate.from_vcs(
        link, lookup=first.persisted_vcs_candidate
    )
    assert isinstance(candidate, candidates.InstallationCandidate)
    record = CandidateRecord(candidate.name, candidate.version, link)
    metadata = first.metadata_loader(record, parse_requirement("commit-demo")).load()
    assert (metadata.name, str(metadata.version)) == ("commit-demo", "2.0")
    assert first.vcs_revision(url) == _git(root, "rev-parse", "HEAD")
    first.close()
    vcs._remove_shared_checkouts()
    monkeypatch.setattr(candidates, "_vcs_candidates", {})

    clones: list[str] = []
    monkeypatch.setattr(
        vcs, "_clone_vcs", lambda u, **k: clones.append(u) or pytest.fail(f"cloned {u}")
    )
    second = _materializer(tmp_path)

    assert second.persisted_vcs_candidate(link) == ("commit-demo", Version("2.0"))
    again = candidates.InstallationCandidate.from_vcs(
        link, lookup=second.persisted_vcs_candidate
    )
    assert isinstance(again, candidates.InstallationCandidate)
    assert (again.name, str(again.version)) == ("commit-demo", "2.0")
    loaded = second.metadata_loader(
        CandidateRecord(again.name, again.version, link),
        parse_requirement("commit-demo"),
    ).load()
    assert (loaded.name, str(loaded.version)) == ("commit-demo", "2.0")
    assert second.vcs_revision(url) == _git(root, "rev-parse", "HEAD")
    assert clones == []
    second.close()


def test_metadata_is_not_persisted_under_a_commit_the_clone_did_not_check_out(
    repo: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, root = repo
    link = Link.from_url(url, source_url=None)
    stale = "0" * 40
    # The materializer imports the resolver by name, so patch it there;
    # patching ``vcs`` would leave the real resolver in place and this test
    # would pass without exercising the guard.
    monkeypatch.setattr(
        candidate_materialization, "resolve_git_commit", lambda u, **k: stale
    )
    materializer = _materializer(tmp_path)
    cache = materializer.persistent_candidate_metadata_cache
    assert cache is not None

    record = CandidateRecord("commit-demo", Version("2.0"), link)
    materializer.metadata_loader(record, parse_requirement("commit-demo")).load()

    assert cache.get((url, "", (), f"git:{stale}")) is None
    assert materializer.vcs_revision(url) == _git(root, "rev-parse", "HEAD")
    materializer.close()
