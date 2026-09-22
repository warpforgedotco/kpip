"""A wheel built from a git source is reused under the commit it was built at.

Built wheels were cached only for git links pinned to a full commit. A
branch or tag resolves to a commit without a clone (``resolve_git_commit``),
so the wheel built from it is cached under that commit, and a later run
finds it before cloning at all. The wheel is not cached under a commit the
checkout did not check out.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index import candidate_materialization, candidates, vcs
from kpip.index.candidate_cache import built_wheel_cache_key
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
        '[project]\nname = "wheel-demo"\nversion = "1.0"\n'
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
    )
    (root / "wheel_demo.py").write_text("")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return f"git+file://{root}", root


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcs, "_resolved_commits", {})
    monkeypatch.setattr(vcs, "_shared_checkouts", {})
    monkeypatch.setattr(candidates, "_vcs_candidates", {})


def _fake_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for the build backend, which the test environment lacks.

    Writes a minimal valid wheel for the source tree and records each build.
    """
    builds: list[str] = []

    def build(source: str, **kwargs: object) -> str:
        builds.append(source)
        directory = tempfile.mkdtemp(prefix="kpip-test-wheel-")
        path = os.path.join(directory, "wheel_demo-1.0-py3-none-any.whl")
        with zipfile.ZipFile(path, "w") as wheel:
            wheel.writestr("wheel_demo.py", "")
            wheel.writestr(
                "wheel_demo-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: wheel-demo\nVersion: 1.0\n",
            )
            wheel.writestr(
                "wheel_demo-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr("wheel_demo-1.0.dist-info/RECORD", "")
        return path

    monkeypatch.setattr(candidate_materialization, "build_wheel_from_source", build)
    return builds


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


def test_the_key_admits_a_resolved_commit_and_carries_it() -> None:
    record = CandidateRecord(
        "wheel-demo",
        Version("1.0"),
        Link.from_url("git+https://example.invalid/demo.git", source_url=None),
    )
    common = dict(
        source_hashes=None,
        config_settings=None,
        build_constraints=(),
        build_isolation=False,
        target_key="t",
    )

    assert built_wheel_cache_key(record, **common) is None
    key = built_wheel_cache_key(record, vcs_commit="a" * 40, **common)
    other = built_wheel_cache_key(record, vcs_commit="b" * 40, **common)

    assert key is not None
    assert '"vcs_commit":"' + "a" * 40 in key
    assert key != other


def test_a_second_run_reuses_the_wheel_without_cloning(
    repo: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, root = repo
    link = Link.from_url(url, source_url=None)
    requirement = parse_requirement("wheel-demo")
    record = CandidateRecord("wheel-demo", Version("1.0"), link)
    builds = _fake_builds(monkeypatch)

    first = _materializer(tmp_path)
    built = list(first.iter_materialize(requirement, [record]))
    assert [w.name for w in built] == ["wheel-demo"]
    assert len(builds) == 1
    first.close()
    vcs._remove_shared_checkouts()

    monkeypatch.setattr(vcs, "_clone_vcs", lambda u, **k: pytest.fail(f"cloned {u}"))
    second = _materializer(tmp_path)
    reused = list(second.iter_materialize(requirement, [record]))

    assert [w.name for w in reused] == ["wheel-demo"]
    assert reused[0].from_cache
    assert len(builds) == 1
    assert second.vcs_revision(url) == _git(root, "rev-parse", "HEAD")
    second.close()


def test_a_wheel_is_not_cached_under_a_commit_the_checkout_did_not_have(
    repo: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, root = repo
    link = Link.from_url(url, source_url=None)
    requirement = parse_requirement("wheel-demo")
    record = CandidateRecord("wheel-demo", Version("1.0"), link)
    builds = _fake_builds(monkeypatch)
    monkeypatch.setattr(
        candidate_materialization, "resolve_git_commit", lambda u, **k: "0" * 40
    )

    first = _materializer(tmp_path)
    list(first.iter_materialize(requirement, [record]))
    first.close()
    vcs._remove_shared_checkouts()
    monkeypatch.setattr(vcs, "_resolved_commits", {})

    clones: list[str] = []
    real = vcs._clone_vcs

    def counting(u: str, **k: object) -> str:
        clones.append(u)
        return real(u, **k)

    monkeypatch.setattr(vcs, "_clone_vcs", counting)
    second = _materializer(tmp_path)
    list(second.iter_materialize(requirement, [record]))

    assert clones == [url], "a stale commit must not serve a cached wheel"
    assert len(builds) == 2
    second.close()
