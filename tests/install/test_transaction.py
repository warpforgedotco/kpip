import os
import shutil
from pathlib import Path

from kpip.install import transaction
import pytest
from kpip.core.errors import InstallationError
from kpip.install.transaction import InstallTransaction
from kpip.install.wheel_state import discover_installed_wheels


def test_lightweight_installed_wheel_inventory_reads_dist_info(tmp_path: Path) -> None:
    metadata = tmp_path / "Demo_Pkg-1.2.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: Demo_Pkg\nVersion: 1.2\n\n",
        encoding="utf-8",
    )
    (metadata / "RECORD").write_text("demo.py,,\n", encoding="utf-8")

    installed = discover_installed_wheels(
        (str(tmp_path),),
        names={"demo-pkg"},
    )

    assert installed is not None
    distribution = installed["demo-pkg"]
    assert distribution.raw_version == "1.2"
    assert distribution.read_text("RECORD") == "demo.py,,\n"


@pytest.mark.parametrize("metadata_name", ["demo.egg-info", "demo.egg-link"])
def test_lightweight_installed_wheel_inventory_defers_legacy_metadata(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    path = tmp_path / metadata_name
    path.mkdir() if metadata_name.endswith(".egg-info") else path.touch()

    assert discover_installed_wheels((str(tmp_path),), names={"demo"}) is None


def test_lightweight_installed_wheel_inventory_defers_malformed_metadata(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "demo-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: demo\n", encoding="utf-8")

    assert discover_installed_wheels((str(tmp_path),), names={"demo"}) is None


def test_lightweight_installed_wheel_inventory_skips_unrelated_metadata(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "demo-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Name: demo\nVersion: 1.0\n\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.egg-link").touch()
    (tmp_path / "broken-1.0.dist-info").mkdir()

    installed = discover_installed_wheels((str(tmp_path),), names={"demo"})

    assert installed is not None
    assert set(installed) == {"demo"}


def test_transaction_commits_and_replaces_owned_file(tmp_path: Path) -> None:
    destination = tmp_path / "site" / "demo.py"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    with InstallTransaction(owned_paths=[destination]) as install_transaction:
        install_transaction.add(source, destination)
        install_transaction.commit()

    assert destination.read_text(encoding="utf-8") == "new"


def test_transaction_commits_staged_contents(tmp_path: Path) -> None:
    destination = tmp_path / "site" / "demo.py"

    with InstallTransaction() as install_transaction:
        install_transaction.add_contents(destination, b"new")
        install_transaction.commit()

    assert destination.read_bytes() == b"new"


def test_transaction_clones_without_consuming_cache_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under KPIP_LINK_MODE=clone the target must not share inodes with the
    # cache; the hardlink default on Linux and Windows deliberately does.
    from kpip.host import clone

    monkeypatch.setattr(clone, "_link_mode", None)
    monkeypatch.setenv("KPIP_LINK_MODE", "clone")
    source = tmp_path / "cache" / "source.txt"
    source.parent.mkdir()
    source.write_text("immutable")
    destination = tmp_path / "target" / "destination.txt"

    with InstallTransaction() as install_transaction:
        install_transaction.add_clone(str(source), str(destination))
        install_transaction.commit()

    assert source.read_text() == "immutable"
    assert destination.read_text() == "immutable"
    destination.write_text("changed")
    assert source.read_text() == "immutable"


def test_transaction_rejects_unowned_collision(tmp_path: Path) -> None:
    destination = tmp_path / "demo.py"
    destination.write_text("unrelated", encoding="utf-8")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    install_transaction = InstallTransaction()
    install_transaction.add(source, destination)
    with pytest.raises(InstallationError, match="unrelated file"):
        install_transaction.commit()

    assert destination.read_text(encoding="utf-8") == "unrelated"


def test_transaction_validation_does_not_recheck_destination_file_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "demo.py"
    destination.write_text("unrelated", encoding="utf-8")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")
    original_isfile = transaction.os.path.isfile
    checked: list[str] = []

    def counting_isfile(path: str | os.PathLike[str]) -> bool:
        checked.append(os.fspath(path))
        return original_isfile(path)

    monkeypatch.setattr(transaction.os.path, "isfile", counting_isfile)
    install_transaction = InstallTransaction()
    install_transaction.add(source, destination)

    with pytest.raises(InstallationError, match="unrelated file"):
        install_transaction.commit()

    assert os.fspath(source) in checked
    assert os.fspath(destination) not in checked


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_transaction_replaces_broken_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "demo.py"
    destination.symlink_to(tmp_path / "missing.py")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    install_transaction = InstallTransaction()
    install_transaction.add(source, destination)
    install_transaction.commit()

    assert destination.read_text(encoding="utf-8") == "new"


def test_transaction_rejects_duplicate_destination(tmp_path: Path) -> None:
    install_transaction = InstallTransaction()
    destination = tmp_path / "demo.py"

    install_transaction.add(tmp_path / "first.py", destination)
    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_transaction.add(tmp_path / "second.py", destination)


def test_transaction_stages_a_later_distributions_copy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two distributions shipping one file: the later one's copy is staged,
    as pip leaves it, with a warning naming both."""
    first = tmp_path / "first.py"
    first.write_text("first", encoding="utf-8")
    second = tmp_path / "second.py"
    second.write_text("second", encoding="utf-8")
    destination = tmp_path / "target" / "shared.py"

    install_transaction = InstallTransaction()
    install_transaction.owner = "jupyter-core"
    install_transaction.add(first, destination)
    install_transaction.owner = "jupyter"
    with caplog.at_level("WARNING"):
        install_transaction.add(second, destination)
    install_transaction.commit()

    assert destination.read_text(encoding="utf-8") == "second"
    assert "jupyter-core and jupyter both install" in caplog.text


def test_transaction_adopts_a_later_distributions_copy(tmp_path: Path) -> None:
    destination = tmp_path / "target" / "shared.py"
    batch = InstallTransaction()
    for owner in ("pkg-a", "pkg-b"):
        local = InstallTransaction()
        local.owner = owner
        local.add_contents(destination, owner.encode())
        batch.adopt(local)
    batch.commit()

    assert destination.read_bytes() == b"pkg-b"
    assert batch.owner is None


def test_transaction_rejects_one_distribution_staging_a_file_twice(
    tmp_path: Path,
) -> None:
    install_transaction = InstallTransaction()
    install_transaction.owner = "pkg"
    destination = tmp_path / "demo.py"

    install_transaction.add(tmp_path / "first.py", destination)
    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_transaction.add(tmp_path / "second.py", destination)


def test_transaction_rolls_back_previous_changes_on_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    first.write_text("old", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text("new", encoding="utf-8")

    install_transaction = InstallTransaction(owned_paths=[first])
    install_transaction.add(source, first)
    install_transaction.add(tmp_path / "missing.py", tmp_path / "second.py")

    with pytest.raises(InstallationError, match="staged file"):
        install_transaction.commit()

    assert first.read_text(encoding="utf-8") == "old"


def test_transaction_rolls_back_staged_contents_on_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    first.write_text("old", encoding="utf-8")

    install_transaction = InstallTransaction(owned_paths=[first])
    install_transaction.add_contents(first, b"new")
    install_transaction.add(tmp_path / "missing.py", tmp_path / "second.py")

    with pytest.raises(InstallationError, match="staged file"):
        install_transaction.commit()

    assert first.read_text(encoding="utf-8") == "old"


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_staged_contents_mode_exact_under_permissive_umask(
    tmp_path: Path,
    mode: int,
) -> None:
    """Explicit modes are reproduced exactly under a permissive umask."""
    destination = tmp_path / "demo.py"
    old_umask = os.umask(0o022)
    try:
        with InstallTransaction() as install_transaction:
            install_transaction.add_contents(str(destination), b"payload", mode=mode)
            install_transaction.commit()
    finally:
        os.umask(old_umask)

    assert stat_mode(destination) == mode


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
def test_staged_contents_mode_exact_under_stripping_umask(tmp_path: Path) -> None:
    """Explicit modes are restored after a restrictive umask masks creation."""
    destination = tmp_path / "demo.sh"
    old_umask = os.umask(0o077)
    try:
        with InstallTransaction() as install_transaction:
            install_transaction.add_contents(
                str(destination), b"#!/bin/sh\n", mode=0o755
            )
            install_transaction.commit()
    finally:
        os.umask(old_umask)

    assert stat_mode(destination) == 0o755


def test_commit_does_not_read_process_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "demo.txt"

    def unexpected_umask(_mode: int) -> int:
        raise AssertionError("commit must not mutate the process-global umask")

    monkeypatch.setattr(transaction.os, "umask", unexpected_umask)

    with InstallTransaction() as install_transaction:
        install_transaction.add_contents(destination, b"payload", mode=0o644)
        install_transaction.commit()

    assert destination.read_bytes() == b"payload"


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
def test_staged_contents_default_mode_matches_open_builtin(tmp_path: Path) -> None:
    """mode=None must produce the same permissions the old open(path, "wb")
    write produced: 0o666 masked by the umask.
    """
    destination = tmp_path / "demo.txt"
    old_umask = os.umask(0o022)
    try:
        with InstallTransaction() as install_transaction:
            install_transaction.add_contents(str(destination), b"payload")
            install_transaction.commit()
    finally:
        os.umask(old_umask)

    assert stat_mode(destination) == 0o666 & ~0o022


def test_staged_contents_short_os_write_calls_are_all_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = os.write

    def short_write(fd: int, data) -> int:  # noqa: ANN001
        return original_write(fd, bytes(data)[:3])

    monkeypatch.setattr(transaction.os, "write", short_write)
    destination = tmp_path / "demo.bin"
    payload = bytes(range(256)) * 40

    with InstallTransaction() as install_transaction:
        install_transaction.add_contents(str(destination), payload)
        install_transaction.commit()

    assert destination.read_bytes() == payload


def test_staged_contents_zero_write_raises_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def zero_write(fd: int, data) -> int:  # noqa: ANN001
        return 0

    monkeypatch.setattr(transaction.os, "write", zero_write)
    destination = tmp_path / "demo.bin"

    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(destination), b"payload")
    with pytest.raises(OSError, match="could not write staged file contents"):
        install_transaction.commit()

    assert not destination.exists()


def test_validate_live_symlink_to_identical_file_is_tolerated(tmp_path: Path) -> None:
    """A destination that is a live symlink to a byte-identical file must
    keep passing validation (the follow-the-link stat path), exactly as
    the old always-follow os.stat call behaved.
    """
    real = tmp_path / "real.py"
    real.write_bytes(b"same contents")
    destination = tmp_path / "demo.py"
    destination.symlink_to(real)

    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(destination), b"same contents")
    install_transaction.validate()


def test_validate_live_symlink_to_different_file_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.py"
    real.write_bytes(b"other contents")
    destination = tmp_path / "demo.py"
    destination.symlink_to(real)

    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(destination), b"new contents")
    with pytest.raises(InstallationError, match="an unrelated file already exists"):
        install_transaction.validate()


def test_validate_fresh_destination_uses_a_single_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh install's absent destination must be answered by one lstat,
    not the old stat-then-lexists pair.
    """
    calls = {"lstat": 0, "stat": 0}
    original_lstat = os.lstat
    original_stat = os.stat

    def counting_lstat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["lstat"] += 1
        return original_lstat(path, *args, **kwargs)

    def counting_stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["stat"] += 1
        return original_stat(path, *args, **kwargs)

    destination = str(tmp_path / "absent.py")
    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(destination), b"payload")
    monkeypatch.setattr(transaction.os, "lstat", counting_lstat)
    monkeypatch.setattr(transaction.os, "stat", counting_stat)
    install_transaction.validate()
    monkeypatch.undo()

    assert calls == {"lstat": 1, "stat": 0}


def test_validate_propagates_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission failure probing the destination must not be recorded as
    "absent" -- that record is what lets backup_if_needed skip the backup,
    so swallowing it would authorize an unprotected overwrite later.
    """

    def denied_lstat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise PermissionError(13, "Permission denied", path)

    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(tmp_path / "demo.py"), b"payload")
    monkeypatch.setattr(transaction.os, "lstat", denied_lstat)
    with pytest.raises(PermissionError):
        install_transaction.validate()


def test_fresh_transaction_does_not_allocate_backup_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_backup_directory(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("fresh transaction should not need backup storage")

    monkeypatch.setattr(transaction.tempfile, "mkdtemp", unexpected_backup_directory)
    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(tmp_path / "demo.py"), b"payload")

    install_transaction.commit()

    assert (tmp_path / "demo.py").read_bytes() == b"payload"


def test_validate_treats_file_parent_component_as_absent(tmp_path: Path) -> None:
    """A destination whose parent path component is a regular file lstats
    to ENOTDIR -- nothing installable exists there, matching the old
    lexists fallback, and the eventual commit failure (with rollback)
    happens at directory creation exactly as before.
    """
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file where a directory is expected")
    destination = blocker / "demo.py"

    install_transaction = InstallTransaction()
    install_transaction.add_contents(str(destination), b"payload")
    install_transaction.validate()

    assert install_transaction.destination_presence[str(destination)] is False


def test_chmod_failure_after_replace_rolls_back_a_fresh_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chmod failure right after os.replace installed a fresh destination
    must leave rollback able to remove it. (A pre-existing destination is
    covered by the backup-restore path regardless; a fresh one has no
    backup, so only the created-paths record can undo the install.)
    """
    destination = tmp_path / "site" / "demo.py"
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    def failing_chmod(path, mode, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise PermissionError(13, "Operation not permitted", path)

    install_transaction = InstallTransaction()
    install_transaction.add(str(source), str(destination), mode=0o755)
    monkeypatch.setattr(transaction.os, "chmod", failing_chmod)
    with pytest.raises(PermissionError):
        install_transaction.commit()

    assert not destination.exists()


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_a_backup_on_another_volume_is_renamed_beside_its_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Across volumes a move deletes after copying, which Windows refuses
    for a loaded .pyd; a rename beside it it allows."""
    from kpip.install import transaction as transaction_module
    from kpip.install.transaction import InstallTransaction

    monkeypatch.setattr(
        transaction_module, "_same_volume", lambda path, directory: False
    )
    original = tmp_path / "module.pyd"

    original.write_text("old")
    rolled_back = InstallTransaction()
    rolled_back.backup_if_needed(str(original))
    assert not original.exists()
    rolled_back.rollback()
    assert original.read_text() == "old"

    kept = InstallTransaction()
    kept.backup_if_needed(str(original))
    kept.finish_successfully()
    assert not original.exists()
    assert not list(tmp_path.glob("module.pyd.kpip-backup-*"))


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
def test_a_hard_linked_clone_never_loosens_the_cache_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone may be a hard link into the archive cache: a chmod of it is a
    chmod of the cache's file, which is only ever tightened."""
    source = tmp_path / "cache" / "module.py"
    source.parent.mkdir()
    source.write_text("cached")
    source.chmod(0o600)
    monkeypatch.setattr(transaction, "clone_path", os.link)
    destination = tmp_path / "site" / "module.py"

    with InstallTransaction() as install_transaction:
        install_transaction.add_clone(str(source), str(destination), mode=0o644)
        install_transaction.commit()

    assert stat_mode(source) == 0o600
    assert stat_mode(destination) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
def test_an_unshared_clone_takes_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cache" / "tool"
    source.parent.mkdir()
    source.write_text("cached")
    source.chmod(0o644)
    monkeypatch.setattr(transaction, "clone_path", shutil.copyfile)
    destination = tmp_path / "site" / "tool"

    with InstallTransaction() as install_transaction:
        install_transaction.add_clone(str(source), str(destination), mode=0o755)
        install_transaction.commit()

    assert stat_mode(source) == 0o644
    assert stat_mode(destination) == 0o755


def _cache_tree(root: Path) -> Path:
    """A cached package directory: pkg/__init__.py and pkg/sub/mod.py."""
    tree = root / "cache" / "pkg"
    (tree / "sub").mkdir(parents=True)
    (tree / "__init__.py").write_text("init")
    (tree / "sub" / "mod.py").write_text("mod")
    return tree


def _files(tree: Path, destination: Path) -> list[tuple[str, str, int | None]]:
    return [
        (str(tree / "__init__.py"), str(destination / "__init__.py"), None),
        (str(tree / "sub" / "mod.py"), str(destination / "sub" / "mod.py"), None),
    ]


def _installed(destination: Path) -> dict[str, str]:
    return {
        os.path.relpath(os.path.join(directory, name), destination): open(
            os.path.join(directory, name)
        ).read()
        for directory, _, names in os.walk(destination)
        for name in names
    }


def test_a_lazy_tree_new_to_the_target_is_cloned_whole(tmp_path: Path) -> None:
    tree = _cache_tree(tmp_path)
    destination = tmp_path / "site" / "pkg"
    expanded: list[bool] = []

    def expand():
        expanded.append(True)
        return _files(tree, destination)

    transaction = InstallTransaction()
    transaction.add_lazy_tree(str(tree), str(destination), expand)
    transaction.commit()

    assert _installed(destination) == {"__init__.py": "init", "sub/mod.py": "mod"}
    assert expanded == []


def test_a_lazy_tree_whose_destination_exists_is_staged_file_by_file(
    tmp_path: Path,
) -> None:
    tree = _cache_tree(tmp_path)
    destination = tmp_path / "site" / "pkg"
    destination.mkdir(parents=True)
    (destination / "other.py").write_text("other")

    transaction = InstallTransaction()
    transaction.add_lazy_tree(
        str(tree), str(destination), lambda: _files(tree, destination)
    )
    transaction.commit()

    assert _installed(destination) == {
        "__init__.py": "init",
        "other.py": "other",
        "sub/mod.py": "mod",
    }


def test_lazy_trees_sharing_a_destination_are_both_staged(tmp_path: Path) -> None:
    first = _cache_tree(tmp_path / "first")
    second = tmp_path / "second" / "cache" / "pkg"
    second.mkdir(parents=True)
    (second / "extra.py").write_text("extra")
    destination = tmp_path / "site" / "pkg"

    transaction = InstallTransaction()
    transaction.owner = "first"
    transaction.add_lazy_tree(
        str(first), str(destination), lambda: _files(first, destination)
    )
    transaction.owner = "second"
    transaction.add_lazy_tree(
        str(second),
        str(destination),
        lambda: [(str(second / "extra.py"), str(destination / "extra.py"), None)],
    )
    transaction.commit()

    assert _installed(destination) == {
        "__init__.py": "init",
        "extra.py": "extra",
        "sub/mod.py": "mod",
    }


def test_a_lazy_tree_another_distribution_stages_into_is_staged_file_by_file(
    tmp_path: Path,
) -> None:
    tree = _cache_tree(tmp_path)
    destination = tmp_path / "site" / "pkg"

    transaction = InstallTransaction()
    transaction.owner = "first"
    transaction.add_lazy_tree(
        str(tree), str(destination), lambda: _files(tree, destination)
    )
    transaction.owner = "second"
    transaction.add_contents(str(destination / "plugin.py"), b"plugin")
    transaction.commit()

    assert _installed(destination) == {
        "__init__.py": "init",
        "plugin.py": "plugin",
        "sub/mod.py": "mod",
    }


def _old_package(site: Path) -> tuple[Path, set[str]]:
    """An installed pkg/: two files of its own and their bytecode."""
    old = site / "pkg"
    (old / "gone").mkdir(parents=True)
    (old / "__init__.py").write_text("old init")
    (old / "gone" / "old.py").write_text("old module")
    (old / "__pycache__").mkdir()
    (old / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"pyc")
    return old, {str(old / "__init__.py"), str(old / "gone" / "old.py")}


def test_a_replacing_lazy_tree_moves_the_old_directory_aside(tmp_path: Path) -> None:
    tree = _cache_tree(tmp_path)
    old, stale = _old_package(tmp_path / "site")

    transaction = InstallTransaction()
    transaction.add_lazy_tree(
        str(tree), str(old), lambda: _files(tree, old), stale=stale
    )
    transaction.commit()

    assert _installed(old) == {"__init__.py": "init", "sub/mod.py": "mod"}
    assert sorted(os.listdir(old.parent)) == ["pkg"]


def test_a_failed_commit_puts_the_replaced_directory_back(tmp_path: Path) -> None:
    tree = _cache_tree(tmp_path)
    old, stale = _old_package(tmp_path / "site")
    before = _installed(old)
    (tmp_path / "site" / "blocker").write_text("a file, not a directory")

    transaction = InstallTransaction()
    transaction.add_lazy_tree(
        str(tree), str(old), lambda: _files(tree, old), stale=stale
    )
    # Fails after the tree is replaced: its parent is a file.
    transaction.add_contents(str(tmp_path / "site" / "blocker" / "x"), b"x")
    with pytest.raises(OSError):
        transaction.commit()

    assert _installed(old) == before
    assert sorted(os.listdir(old.parent)) == ["blocker", "pkg"]


def test_a_replacing_lazy_tree_staged_file_by_file_deletes_what_it_drops(
    tmp_path: Path,
) -> None:
    tree = _cache_tree(tmp_path)
    old, stale = _old_package(tmp_path / "site")

    transaction = InstallTransaction()
    transaction.owner = "first"
    transaction.add_lazy_tree(
        str(tree), str(old), lambda: _files(tree, old), stale=stale
    )
    transaction.owner = "second"
    transaction.add_contents(str(old / "plugin.py"), b"plugin")
    transaction.commit()

    installed = _installed(old)
    assert installed["__init__.py"] == "init"
    assert installed["sub/mod.py"] == "mod"
    assert installed["plugin.py"] == "plugin"
    assert "gone/old.py" not in installed
