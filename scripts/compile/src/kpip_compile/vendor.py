"""Fetch the latest upstream Nuitka ``develop`` and apply kpip's patches on top of it."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NUITKA_REPOSITORY = "https://github.com/Nuitka/Nuitka.git"
# Always its latest commit; the patches under PATCHES_DIR follow it.
NUITKA_BRANCH = "develop"
# Local branch in the checkout marking the upstream commit, the base for
# exporting patches.
BASE_BRANCH = "upstream"

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE_ROOT.parents[1]
# A subdirectory, so ``vendoring sync`` (which applies ``patches/*.patch`` to
# ``src/kpip/_vendor``) never sees them.
PATCHES_DIR = REPO_ROOT / "tools" / "vendoring" / "patches" / "nuitka"
VENDOR_DIR = PACKAGE_ROOT / "_vendor" / "nuitka"
STAMP_NAME = ".kpip-compile-stamp"


@dataclass(frozen=True)
class VendorSpec:
    repository: str
    commit: str
    patches: tuple[Path, ...]

    def fingerprint(self) -> str:
        """Identify the tree this spec produces: the commit and each patch's bytes."""
        digest = hashlib.sha256()
        digest.update(self.repository.encode())
        digest.update(b"\0")
        digest.update(self.commit.encode())
        for patch in self.patches:
            digest.update(b"\0")
            digest.update(patch.name.encode())
            digest.update(b"\0")
            digest.update(patch.read_bytes())
        return digest.hexdigest()


def find_patches(patches_dir: Path = PATCHES_DIR) -> tuple[Path, ...]:
    """Patches apply in name order, hence the numeric prefixes."""
    if not patches_dir.is_dir():
        return ()
    return tuple(sorted(patches_dir.glob("*.patch")))


class PatchError(RuntimeError):
    pass


def resolve_commit(repository: str, branch: str) -> str:
    """The branch's current commit, without fetching anything."""
    output = subprocess.run(
        ["git", "ls-remote", "--exit-code", repository, f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return output.split()[0]


def default_spec() -> VendorSpec:
    return VendorSpec(
        repository=NUITKA_REPOSITORY,
        commit=resolve_commit(NUITKA_REPOSITORY, NUITKA_BRANCH),
        patches=find_patches(),
    )


def is_current(spec: VendorSpec, vendor_dir: Path = VENDOR_DIR) -> bool:
    stamp = vendor_dir / STAMP_NAME
    try:
        return stamp.read_text(encoding="utf-8").strip() == spec.fingerprint()
    except FileNotFoundError:
        return False


def _remove_tree(path: Path) -> None:
    """``shutil.rmtree`` that also removes read-only files.

    Git writes its objects read-only, and Windows refuses to delete those.
    """

    def make_writable_and_retry(function, target, _error) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def vendor_nuitka(
    spec: VendorSpec | None = None,
    vendor_dir: Path = VENDOR_DIR,
    *,
    force: bool = False,
) -> Path:
    """Provide a patched Nuitka checkout at ``vendor_dir``, reusing a current one.

    The checkout is its own git repository, so ``git diff`` inside it shows
    exactly what the patches changed.
    """
    spec = spec or default_spec()
    if not force and is_current(spec, vendor_dir):
        return vendor_dir

    if vendor_dir.exists():
        _remove_tree(vendor_dir)
    vendor_dir.mkdir(parents=True)

    _git("init", "--quiet", cwd=vendor_dir)
    _git(
        "fetch", "--quiet", "--depth", "1", spec.repository, spec.commit, cwd=vendor_dir
    )
    _git("checkout", "--quiet", "-b", BASE_BRANCH, "FETCH_HEAD", cwd=vendor_dir)

    for patch in spec.patches:
        print(f"Applying {patch.name}", flush=True)
        try:
            _git("apply", "--verbose", str(patch), cwd=vendor_dir)
        except subprocess.CalledProcessError as error:
            raise PatchError(
                f"{patch.name} no longer applies to Nuitka {spec.commit[:12]}; "
                "rebase it onto that commit"
            ) from error

    with (vendor_dir / ".git" / "info" / "exclude").open(
        "a", encoding="utf-8"
    ) as exclude:
        exclude.write(f"/{STAMP_NAME}\n")
    # Written last: an interrupted run leaves no stamp and is redone.
    (vendor_dir / STAMP_NAME).write_text(spec.fingerprint() + "\n", encoding="utf-8")
    return vendor_dir
