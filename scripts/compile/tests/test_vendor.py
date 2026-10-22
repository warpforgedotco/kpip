from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from kpip_compile.vendor import (
    APPLIES,
    CONFLICTS,
    STAMP_NAME,
    UPSTREAM,
    PatchError,
    VendorSpec,
    check_patches,
    find_patches,
    is_current,
    resolve_commit,
    vendor_nuitka,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "--quiet", cwd=repo)
    (repo / "bootstrap.c").write_text("one\ntwo\nthree\n")
    _git("add", ".", cwd=repo)
    _git("commit", "--quiet", "-m", "base", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo)


def _patch(
    upstream: Path, patches_dir: Path, name: str, before: str, after: str
) -> Path:
    """Make a patch by editing the upstream checkout, then undo the edit."""
    source = upstream / "bootstrap.c"
    original = source.read_text()
    source.write_text(original.replace(before, after))
    diff = _git("diff", cwd=upstream) + "\n"
    source.write_text(original)
    patches_dir.mkdir(exist_ok=True)
    patch = patches_dir / name
    patch.write_text(diff)
    return patch


def test_patches_apply_in_name_order(
    upstream: tuple[Path, str], tmp_path: Path
) -> None:
    repo, commit = upstream
    patches = tmp_path / "patches"
    _patch(repo, patches, "0001-two.patch", "two", "TWO")
    # Made against the base, but its context only matches once 0001 applied.
    second = _patch(repo, patches, "0002-three.patch", "three", "THREE")
    second.write_text(second.read_text().replace(" two", " TWO"))

    spec = VendorSpec(
        repository=str(repo), commit=commit, patches=find_patches(patches)
    )
    vendor_dir = vendor_nuitka(spec, tmp_path / "vendor")

    assert (vendor_dir / "bootstrap.c").read_text() == "one\nTWO\nTHREE\n"
    assert is_current(spec, vendor_dir)
    # The stamp is kept out of the checkout's own diff.
    assert STAMP_NAME not in _git("status", "--porcelain", cwd=vendor_dir)


def test_current_checkout_is_reused(upstream: tuple[Path, str], tmp_path: Path) -> None:
    repo, commit = upstream
    spec = VendorSpec(repository=str(repo), commit=commit, patches=())
    vendor_dir = vendor_nuitka(spec, tmp_path / "vendor")
    marker = vendor_dir / "local-edit"
    marker.write_text("kept")

    vendor_nuitka(spec, vendor_dir)

    assert marker.exists()


def test_changed_patch_refetches(upstream: tuple[Path, str], tmp_path: Path) -> None:
    repo, commit = upstream
    patches = tmp_path / "patches"
    patch = _patch(repo, patches, "0001.patch", "two", "TWO")
    spec = VendorSpec(repository=str(repo), commit=commit, patches=(patch,))
    vendor_dir = vendor_nuitka(spec, tmp_path / "vendor")

    _patch(repo, patches, "0001.patch", "two", "2")

    assert not is_current(spec, vendor_dir)
    vendor_nuitka(spec, vendor_dir)
    assert (vendor_dir / "bootstrap.c").read_text() == "one\n2\nthree\n"


def test_failing_patch_leaves_no_stamp(
    upstream: tuple[Path, str], tmp_path: Path
) -> None:
    repo, commit = upstream
    patches = tmp_path / "patches"
    patch = _patch(repo, patches, "0001.patch", "two", "TWO")
    patch.write_text(patch.read_text().replace(" one", " missing"))
    spec = VendorSpec(repository=str(repo), commit=commit, patches=(patch,))

    with pytest.raises(PatchError, match="0001.patch"):
        vendor_nuitka(spec, tmp_path / "vendor")

    assert not is_current(spec, tmp_path / "vendor")


def test_branch_resolves_to_its_latest_commit(upstream: tuple[Path, str]) -> None:
    repo, commit = upstream
    branch = _git("branch", "--show-current", cwd=repo)

    assert resolve_commit(str(repo), branch) == commit

    (repo / "bootstrap.c").write_text("moved\n")
    _git("commit", "--quiet", "-am", "move", cwd=repo)

    assert resolve_commit(str(repo), branch) == _git("rev-parse", "HEAD", cwd=repo)


def test_checkout_marks_the_upstream_commit(
    upstream: tuple[Path, str], tmp_path: Path
) -> None:
    repo, commit = upstream
    spec = VendorSpec(repository=str(repo), commit=commit, patches=())
    vendor_dir = vendor_nuitka(spec, tmp_path / "vendor")

    assert _git("rev-parse", "upstream", cwd=vendor_dir) == commit


def test_refetch_removes_read_only_files(
    upstream: tuple[Path, str], tmp_path: Path
) -> None:
    """Git objects are read-only; Windows would refuse to delete them."""
    repo, commit = upstream
    spec = VendorSpec(repository=str(repo), commit=commit, patches=())
    vendor_dir = vendor_nuitka(spec, tmp_path / "vendor")
    read_only = vendor_dir / "read-only"
    read_only.write_text("x")
    read_only.chmod(0o444)

    vendor_nuitka(spec, vendor_dir, force=True)

    assert not read_only.exists()


def test_check_reports_every_patch(upstream: tuple[Path, str], tmp_path: Path) -> None:
    repo, _commit = upstream
    # Edits far enough apart that each patch's context is upstream's alone.
    lines = [f"line{i:02}\n" for i in range(40)]
    (repo / "bootstrap.c").write_text("".join(lines))
    _git("commit", "--quiet", "-am", "longer", cwd=repo)
    patches = tmp_path / "patches"
    _patch(repo, patches, "0001-a.patch", "line05", "LINE05")
    _patch(repo, patches, "0002-b.patch", "line15", "LINE15")
    conflicting = _patch(repo, patches, "0003-c.patch", "line25", "LINE25")
    conflicting.write_text(conflicting.read_text().replace(" line24", " missing"))
    _patch(repo, patches, "0004-d.patch", "line35", "LINE35")
    # Upstream takes 0002's change itself.
    (repo / "bootstrap.c").write_text("".join(lines).replace("line15", "LINE15"))
    _git("commit", "--quiet", "-am", "take b", cwd=repo)
    spec = VendorSpec(
        repository=str(repo),
        commit=_git("rev-parse", "HEAD", cwd=repo),
        patches=find_patches(patches),
    )

    results = check_patches(spec, tmp_path / "work")

    # A conflict does not stop the patches after it.
    assert [(patch.name, status) for patch, status in results] == [
        ("0001-a.patch", APPLIES),
        ("0002-b.patch", UPSTREAM),
        ("0003-c.patch", CONFLICTS),
        ("0004-d.patch", APPLIES),
    ]
    patched = (tmp_path / "work" / "bootstrap.c").read_text()
    assert "LINE05" in patched and "LINE35" in patched and "LINE25" not in patched
