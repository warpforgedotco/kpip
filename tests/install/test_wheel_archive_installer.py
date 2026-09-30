"""Unit tests for src/kpip/install/wheel_archive_installer.py helpers."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.core.wheel import wheel_candidate
from kpip.install.target import InstallTarget
from kpip.install.wheel_archive_cache import prepare_cached_wheels
from kpip.install.wheel_archive_installer import install_wheels_from_archive_cache


@pytest.mark.parametrize("route", ["archive-cache", "staged", "direct"])
def test_installed_metadata_is_the_wheels_own(tmp_path: Path, route: str) -> None:
    """An installer copies METADATA as the wheel ships it, as pip does: its
    Name keeps the project's own spelling, and RECORD holds the wheel's
    hash for it."""
    import base64

    from kpip.install.wheel_transaction import install_wheels_transactionally

    wheel = tmp_path / "Owner_Demo-1.0-py3-none-any.whl"
    metadata = "Metadata-Version: 2.1\nName: Owner_Demo\nVersion: 1.0\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("owner_demo/__init__.py", "")
        archive.writestr("Owner_Demo-1.0.dist-info/METADATA", metadata)
        archive.writestr(
            "Owner_Demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("Owner_Demo-1.0.dist-info/RECORD", "")
    target = tmp_path / "target"

    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options("owner-demo", target=str(target)),
        pycompile=False,
        force=route == "staged",
        cache_dir=str(tmp_path / "cache") if route == "archive-cache" else None,
    )

    installed = target / "Owner_Demo-1.0.dist-info"
    assert (installed / "METADATA").read_text() == metadata
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(metadata.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert (
        f"Owner_Demo-1.0.dist-info/METADATA,sha256={digest},{len(metadata)}"
        in (installed / "RECORD").read_text()
    )


def _make_wheel(
    directory: Path,
    name: str,
    *,
    shared_module: str | None = None,
    entry_points: str | None = None,
    module_text: str | None = None,
) -> Path:
    """Build a minimal wheel, optionally with a file at ``shared_module`` or

    an ``entry_points.txt``. Two wheels sharing a ``shared_module`` path or a
    console-script name simulate a colliding destination -- both would try
    to write the same file into the target.
    """
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if shared_module is not None:
            archive.writestr(shared_module, module_text or f"# from {name}\n")
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if entry_points is not None:
            archive.writestr(f"{name}-1.0.dist-info/entry_points.txt", entry_points)
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return wheel


def _prevalidated_candidates(
    tmp_path: Path,
    cache_dir: Path,
    wheel_a: Path,
    wheel_b: Path,
) -> tuple[object, object]:
    """Build two candidates with ``wheel_layout`` already set to a

    CachedWheelArchive, matching what the normal candidate-materialization
    path (``install/output.py:prepare_install_candidates``) already does
    before ``install_wheels_from_archive_cache`` ever runs.
    """
    candidate_a = wheel_candidate(wheel_a).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel_a.read_bytes()).hexdigest()},
        source_kind="wheel",
    )
    candidate_b = wheel_candidate(wheel_b).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel_b.read_bytes()).hexdigest()},
        source_kind="wheel",
    )

    archives = prepare_cached_wheels((candidate_a, candidate_b), str(cache_dir))

    return (
        candidate_a.copy_with(wheel_layout=archives[0]),
        candidate_b.copy_with(wheel_layout=archives[1]),
    )


@pytest.mark.parametrize("pycompile", [False, True])
@pytest.mark.parametrize("wheels", [2, 3])
def test_colliding_files_install_the_later_copy_from_the_archive_cache(
    tmp_path: Path, pycompile: bool, wheels: int
) -> None:
    """Wheels shipping one file both install under pip, the later one's copy
    last. The archive-cache route clones them in parallel all the same: the
    earlier copies are skipped and the later one replaces whichever landed,
    and bytecode is compiled from the copy left."""
    import importlib.util
    import marshal

    cache_dir = tmp_path / "cache"
    names = [f"pkg_{letter}" for letter in "abc"[:wheels]]
    built = [
        _make_wheel(
            tmp_path,
            name,
            shared_module="shared_thing.py",
            module_text=f"ORIGIN = {name!r}\n",
        )
        for name in names
    ]
    requests = [(wheel, True, None) for wheel in built]
    candidates = tuple(wheel_candidate(wheel) for wheel in built)
    archives = prepare_cached_wheels(candidates, str(cache_dir), pycompile=pycompile)
    candidates = tuple(
        candidate.copy_with(wheel_layout=archive)
        for candidate, archive in zip(candidates, archives, strict=True)
    )

    target = tmp_path / "target"
    installed = install_wheels_from_archive_cache(
        requests,
        candidates,
        target=InstallTarget.from_options("pkg_a", target=str(target)),
        cache_dir=str(cache_dir),
        pycompile=pycompile,
    )

    assert installed is not None
    assert (target / "shared_thing.py").read_text() == f"ORIGIN = {names[-1]!r}\n"
    for name in names:
        assert (target / f"{name}-1.0.dist-info" / "RECORD").exists()
    if pycompile:
        tag = importlib.util.cache_from_source("shared_thing.py")
        pyc = (target / tag).read_bytes()
        code = marshal.loads(pyc[16:])
        assert code.co_filename.endswith("shared_thing.py")
        assert names[-1] in code.co_consts
        source_stat = (target / "shared_thing.py").stat()
        assert int.from_bytes(pyc[8:12], "little") == int(source_stat.st_mtime)
        assert int.from_bytes(pyc[12:16], "little") == source_stat.st_size


def test_colliding_console_scripts_leave_the_batch_to_the_transactional_installer(
    tmp_path: Path,
) -> None:
    """Two packages providing a ``mytool`` console script: script generation
    writes via ``os.rename``, which overwrites silently on POSIX, so the
    batch-level reservation (``_reserve_destination``) must see the clash
    and hand the batch to the installer that orders it."""
    cache_dir = tmp_path / "cache"
    entry_points = "[console_scripts]\nmytool = pkg:main\n"
    wheel_a = _make_wheel(tmp_path, "pkg_a", entry_points=entry_points)
    wheel_b = _make_wheel(tmp_path, "pkg_b", entry_points=entry_points)
    candidate_a, candidate_b = _prevalidated_candidates(
        tmp_path,
        cache_dir,
        wheel_a,
        wheel_b,
    )

    target = tmp_path / "target"
    install_target = InstallTarget.from_options("pkg_a", target=str(target))

    assert (
        install_wheels_from_archive_cache(
            [(wheel_a, True, None), (wheel_b, True, None)],
            (candidate_a, candidate_b),
            target=install_target,
            cache_dir=str(cache_dir),
        )
        is None
    )
    assert not target.exists()


@pytest.mark.parametrize("with_cache", [False, True])
@pytest.mark.parametrize("extra_wheels", [0, 3])
def test_colliding_files_install_the_later_wheels_copy(
    tmp_path: Path,
    with_cache: bool,
    extra_wheels: int,
) -> None:
    """``jupyter`` and ``jupyter-core`` both ship ``jupyter.py``: pip installs
    both and the file is the later one's. Serial and parallel batches (four
    wheels or more), with and without the archive cache, keep the same one."""
    from kpip.install.wheel_transaction import install_wheels_transactionally

    wheels = [
        _make_wheel(tmp_path, "pkg_a", shared_module="shared_thing.py"),
        *(_make_wheel(tmp_path, f"pkg_x{index}") for index in range(extra_wheels)),
        _make_wheel(tmp_path, "pkg_b", shared_module="shared_thing.py"),
    ]
    target = tmp_path / "target"

    install_wheels_transactionally(
        [(wheel, True, None) for wheel in wheels],
        target=InstallTarget.from_options("pkg_a", target=str(target)),
        pycompile=False,
        cache_dir=str(tmp_path / "cache") if with_cache else None,
    )

    assert (target / "shared_thing.py").read_text() == "# from pkg_b\n"
    for name in ("pkg_a", "pkg_b"):
        assert (target / f"{name}-1.0.dist-info" / "METADATA").exists()


def test_record_rows_match_the_files_on_disk(tmp_path: Path) -> None:
    """Rows whose hash is computed from the bytes written (INSTALLER,
    REQUESTED, a rewritten METADATA) must agree with the files themselves."""
    import base64
    import csv

    cache_dir = tmp_path / "cache"
    wheel = _make_wheel(tmp_path, "Mixed_Case", shared_module="mixed_case.py")
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)
    target = tmp_path / "target"
    install_target = InstallTarget.from_options("mixed-case", target=str(target))

    installed = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=install_target,
        cache_dir=str(cache_dir),
    )
    assert installed is not None

    dist_info = next(target.glob("*.dist-info"))
    rows = list(csv.reader((dist_info / "RECORD").read_text().splitlines()))
    checked = 0
    for relative, digest, size in rows:
        if not digest:
            continue
        path = target / relative
        data = path.read_bytes()
        expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        assert digest == f"sha256={expected.rstrip(b'=').decode('ascii')}", relative
        assert size == str(len(data)), relative
        checked += 1
    assert {row[0].rsplit("/", 1)[-1] for row in rows} >= {
        "INSTALLER",
        "REQUESTED",
        "METADATA",
        "RECORD",
    }
    assert checked >= 3


def _prevalidated_candidates_for(
    tmp_path: Path,
    cache_dir: Path,
    *wheels: Path,
) -> tuple[object, ...]:
    candidates = tuple(
        wheel_candidate(wheel).copy_with(
            source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
            source_kind="wheel",
        )
        for wheel in wheels
    )
    archives = prepare_cached_wheels(candidates, str(cache_dir))
    return tuple(
        candidate.copy_with(wheel_layout=archive)
        for candidate, archive in zip(candidates, archives, strict=True)
    )


def test_archive_route_compiles_bytecode_and_records_it(tmp_path: Path) -> None:
    """With compilation on, the clone route writes the .pyc files and lists
    them in RECORD with real hashes, like the transactional route does."""
    import base64
    import csv

    cache_dir = tmp_path / "cache"
    wheel = _make_wheel(tmp_path, "pkg_compiled", shared_module="compiled_mod.py")
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)
    target = tmp_path / "target"
    install_target = InstallTarget.from_options("pkg-compiled", target=str(target))

    installed = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=install_target,
        cache_dir=str(cache_dir),
        pycompile=True,
    )
    assert installed is not None

    compiled = sorted(p.relative_to(target).as_posix() for p in target.rglob("*.pyc"))
    assert compiled, "no bytecode was written"
    assert all("__pycache__/" in path for path in compiled)

    dist_info = next(target.glob("*.dist-info"))
    rows = {
        row[0]: row[1:]
        for row in csv.reader((dist_info / "RECORD").read_text().splitlines())
    }
    for path in compiled:
        digest, size = rows[path]
        data = (target / path).read_bytes()
        expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        assert digest == f"sha256={expected.rstrip(b'=').decode('ascii')}"
        assert size == str(len(data))


def test_transactional_install_with_compilation_takes_the_clone_route(
    tmp_path: Path,
) -> None:
    from kpip.install.wheel_archive_cache import ARCHIVE_CACHE_BUCKET
    from kpip.install.wheel_transaction import install_wheels_transactionally

    cache_dir = tmp_path / "cache"
    wheel = _make_wheel(tmp_path, "pkg_default", shared_module="default_mod.py")
    candidate = wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )
    target = tmp_path / "target"

    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options("pkg-default", target=str(target)),
        pycompile=True,
        lookup_existing=False,
        candidates=[candidate],
        cache_dir=str(cache_dir),
    )

    assert (cache_dir / ARCHIVE_CACHE_BUCKET).is_dir(), "the clone route was not used"
    assert list(target.rglob("*.pyc")), "bytecode was not compiled"


def _make_wheel_with_members(
    directory: Path, name: str, members: dict[str, str]
) -> Path:
    """A wheel carrying arbitrary members besides its dist-info scaffolding."""
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in members.items():
            archive.writestr(path, text)
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return wheel


@pytest.mark.parametrize("force", [False, True])
def test_vendored_dist_info_is_installed_as_ordinary_files(
    tmp_path: Path, force: bool
) -> None:
    """debugpy vendors a whole ``bytecode-*.dist-info`` inside its package.
    Only the wheel's own dist-info has the RECORD kpip writes and the
    METADATA it normalizes; the vendored ones are files like any other.
    ``force`` takes the staged route, its absence the direct one."""
    from kpip.install.wheel_transaction import install_wheels_transactionally

    vendored = "vendorer/_vendored/thing-0.1.dist-info"
    wheel = _make_wheel_with_members(
        tmp_path,
        "vendorer",
        {
            "vendorer/__init__.py": "",
            f"{vendored}/METADATA": "Metadata-Version: 2.1\nName: Thing\n",
            f"{vendored}/RECORD": "thing/__init__.py,,\n",
        },
    )
    target = tmp_path / "target"

    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options("vendorer", target=str(target)),
        pycompile=False,
        force=force,
    )

    assert (target / vendored / "RECORD").read_text() == "thing/__init__.py,,\n"
    assert (target / vendored / "METADATA").read_text() == (
        "Metadata-Version: 2.1\nName: Thing\n"
    )
    record = (target / "vendorer-1.0.dist-info" / "RECORD").read_text()
    assert f"{vendored}/RECORD,sha256=" in record
    assert "vendorer-1.0.dist-info/RECORD,," in record


def test_wheel_shipping_its_own_generated_pyc_is_rejected_under_compilation(
    tmp_path: Path,
) -> None:
    """A wheel that ships both a module and the .pyc byte-compilation would
    generate collides on one destination; the clone route rejects it rather
    than overwrite the member and emit a duplicate RECORD row."""
    import sys

    cache_dir = tmp_path / "cache"
    tag = sys.implementation.cache_tag
    wheel = _make_wheel_with_members(
        tmp_path,
        "pkg_self_pyc",
        {"selfmod.py": "x = 1\n", f"__pycache__/selfmod.{tag}.pyc": "stale\n"},
    )
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)
    install_target = InstallTarget.from_options(
        "pkg-self-pyc", target=str(tmp_path / "target")
    )

    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_wheels_from_archive_cache(
            [(wheel, True, None)],
            (candidate,),
            target=install_target,
            cache_dir=str(cache_dir),
            pycompile=True,
        )

    installed = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=InstallTarget.from_options("pkg-self-pyc", target=str(tmp_path / "t2")),
        cache_dir=str(cache_dir),
        pycompile=False,
    )
    assert installed is not None


def test_unowned_target_pyc_declines_the_clone_route(tmp_path: Path) -> None:
    """An unowned file already at a generated .pyc path makes the route
    decline (to the transactional path) instead of overwriting it in the
    clone after preflight."""
    import sys

    cache_dir = tmp_path / "cache"
    tag = sys.implementation.cache_tag
    wheel = _make_wheel_with_members(tmp_path, "pkg_new", {"newmod.py": "x = 1\n"})
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)

    target = tmp_path / "target"
    (target / "other-9.9.dist-info").mkdir(parents=True)
    (target / "other-9.9.dist-info" / "RECORD").write_text("")
    stray = target / "__pycache__" / f"newmod.{tag}.pyc"
    stray.parent.mkdir(parents=True)
    stray.write_text("not ours\n")

    install_target = InstallTarget.from_options("pkg-new", target=str(target))

    declined = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=install_target,
        cache_dir=str(cache_dir),
        pycompile=True,
    )
    assert declined is None
    assert stray.read_text() == "not ours\n"


def _install_one(
    tmp_path: Path,
    wheel: Path,
    name: str,
    *,
    pycompile: bool = True,
) -> tuple[Path, Path]:
    """Install ``wheel`` through the clone route; return (target, cache_dir)."""
    from kpip.install.wheel_transaction import install_wheels_transactionally

    cache_dir = tmp_path / "cache"
    target = tmp_path / "target"
    candidate = wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )

    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options(name, target=str(target)),
        pycompile=pycompile,
        lookup_existing=False,
        candidates=[candidate],
        cache_dir=str(cache_dir),
    )

    return target, cache_dir


def _loaded_code(pyc: Path) -> object:
    import marshal

    return marshal.loads(pyc.read_bytes()[16:])


def test_archive_cache_byte_compiles_at_fill_time(tmp_path: Path) -> None:
    """The cache entry carries its own ``pyc/`` tree, laid out by mapped path."""
    from kpip.install.wheel_archive_cache import (
        ARCHIVE_CACHE_BUCKET,
        PYC_CACHE_SUBDIR,
    )

    wheel = _make_wheel_with_members(tmp_path, "fillpkg", {"fillpkg/mod.py": "X = 1\n"})
    _, cache_dir = _install_one(tmp_path, wheel, "fillpkg")

    cached = list((cache_dir / ARCHIVE_CACHE_BUCKET).rglob(f"{PYC_CACHE_SUBDIR}/*"))

    assert cached, "the archive cache did not byte-compile at fill time"
    assert list((cache_dir / ARCHIVE_CACHE_BUCKET).rglob("fillpkg/__pycache__/*.pyc"))


def test_installed_pyc_names_the_installed_path_not_the_staging_directory(
    tmp_path: Path,
) -> None:
    """``co_filename`` must be where the module actually lives.

    The staging directory is renamed away at the end of the install, so a
    ``.pyc`` naming it leaves every traceback from that module without source.
    """
    wheel = _make_wheel_with_members(
        tmp_path,
        "namepkg",
        {"namepkg/mod.py": "def f():\n    return 1\n"},
    )
    target, _ = _install_one(tmp_path, wheel, "namepkg")

    pyc = next((target / "namepkg" / "__pycache__").glob("mod.*.pyc"))
    code = _loaded_code(pyc)

    assert code.co_filename == str(target / "namepkg" / "mod.py")
    assert Path(code.co_filename).is_file()


def test_installed_pyc_rebinds_nested_code_objects(tmp_path: Path) -> None:
    """Functions and classes carry their own code objects; all must be rebound."""
    from types import CodeType

    wheel = _make_wheel_with_members(
        tmp_path,
        "nestpkg",
        {
            "nestpkg/mod.py": (
                "class C:\n"
                "    def method(self):\n"
                "        def inner():\n"
                "            return 3\n"
                "        return inner\n"
            ),
        },
    )
    target, _ = _install_one(tmp_path, wheel, "nestpkg")

    pyc = next((target / "nestpkg" / "__pycache__").glob("mod.*.pyc"))
    expected = str(target / "nestpkg" / "mod.py")

    seen = 0

    def walk(code: CodeType) -> None:
        nonlocal seen
        seen += 1
        assert code.co_filename == expected
        for const in code.co_consts:
            if isinstance(const, CodeType):
                walk(const)

    walk(_loaded_code(pyc))

    assert seen >= 4, "expected module, class body, method and closure"


def test_installed_pyc_is_not_stale_for_the_interpreter(tmp_path: Path) -> None:
    """The header must validate against the installed source, or every import
    silently recompiles and the cached bytecode buys nothing."""
    import importlib.util

    wheel = _make_wheel_with_members(
        tmp_path,
        "freshpkg",
        {"freshpkg/mod.py": "VALUE = 42\n"},
    )
    target, _ = _install_one(tmp_path, wheel, "freshpkg")

    source = target / "freshpkg" / "mod.py"
    pyc = Path(importlib.util.cache_from_source(str(source)))
    header = pyc.read_bytes()[:16]
    stat = source.stat()

    assert header[:4] == importlib.util.MAGIC_NUMBER
    assert int.from_bytes(header[4:8], "little") == 0, "expected timestamp mode"
    assert int.from_bytes(header[8:12], "little") == int(stat.st_mtime) & 0xFFFFFFFF
    assert int.from_bytes(header[12:16], "little") == stat.st_size & 0xFFFFFFFF


def test_data_purelib_modules_are_compiled_at_their_relocated_path(
    tmp_path: Path,
) -> None:
    """``.data/purelib`` members move to the target root during install; their
    bytecode has to land beside them, not beside the pre-relocation path."""
    wheel = _make_wheel_with_members(
        tmp_path,
        "datapkg",
        {"datapkg-1.0.data/purelib/datapkg/mod.py": "Y = 2\n"},
    )
    target, _ = _install_one(tmp_path, wheel, "datapkg")

    pyc = next((target / "datapkg" / "__pycache__").glob("mod.*.pyc"))

    assert _loaded_code(pyc).co_filename == str(target / "datapkg" / "mod.py")
    assert not (target / "datapkg-1.0.data").exists()


def test_a_module_that_cannot_compile_does_not_fail_the_install(
    tmp_path: Path,
) -> None:
    """Wheels ship unbuildable Python (vendored Python 2, most often). The
    module still installs; only its bytecode is missing."""
    wheel = _make_wheel_with_members(
        tmp_path,
        "badpkg",
        {
            "badpkg/good.py": "OK = 1\n",
            "badpkg/broken.py": "print 'python 2'\n",
        },
    )
    target, _ = _install_one(tmp_path, wheel, "badpkg")

    assert (target / "badpkg" / "broken.py").is_file()
    assert list((target / "badpkg" / "__pycache__").glob("good.*.pyc"))
    assert not list((target / "badpkg" / "__pycache__").glob("broken.*.pyc"))


def test_no_compile_installs_no_bytecode(tmp_path: Path) -> None:
    wheel = _make_wheel_with_members(
        tmp_path,
        "plainpkg",
        {"plainpkg/mod.py": "Z = 3\n"},
    )
    target, _ = _install_one(tmp_path, wheel, "plainpkg", pycompile=False)

    assert (target / "plainpkg" / "mod.py").is_file()
    assert not list(target.rglob("*.pyc"))


def test_install_falls_back_when_the_cache_has_no_bytecode(tmp_path: Path) -> None:
    """Entries written before the cache learned to compile have no ``pyc/``.
    They must still install with bytecode, compiled in the stage."""
    import shutil as _shutil

    from kpip.install.wheel_archive_cache import (
        ARCHIVE_CACHE_BUCKET,
        PYC_CACHE_SUBDIR,
    )

    wheel = _make_wheel_with_members(
        tmp_path,
        "oldpkg",
        {"oldpkg/mod.py": "W = 4\n"},
    )
    target, cache_dir = _install_one(tmp_path, wheel, "oldpkg")
    _shutil.rmtree(target)

    for stale in (cache_dir / ARCHIVE_CACHE_BUCKET).rglob(PYC_CACHE_SUBDIR):
        _shutil.rmtree(stale)

    target, _ = _install_one(tmp_path, wheel, "oldpkg")

    assert list((target / "oldpkg" / "__pycache__").glob("mod.*.pyc"))


def test_a_cold_install_never_compiles_in_the_stage(tmp_path: Path) -> None:
    """Compilation moved out of extraction into a pass that runs afterwards.
    If that pass ran too late, the install would find no bytecode, compile in
    the stage, and the cache would compile the same modules again -- strictly
    worse than doing it inline. This pins the ordering.
    """
    from kpip.install import wheel_archive_installer as installer_module

    wheel = _make_wheel_with_members(
        tmp_path,
        "orderpkg",
        {f"orderpkg/mod_{index}.py": f"V = {index}\n" for index in range(6)},
    )

    fell_back: list[tuple[str, tuple[str, ...]]] = []
    real = installer_module._compile_uncached

    def recording(stage, members):  # noqa: ANN001, ANN202
        fell_back.extend(members)
        return real(stage, members)

    installer_module._compile_uncached = recording
    try:
        target, _ = _install_one(tmp_path, wheel, "orderpkg")
    finally:
        installer_module._compile_uncached = real

    assert list((target / "orderpkg" / "__pycache__").glob("mod_0.*.pyc"))
    assert not fell_back, (
        f"{len(fell_back)} modules were compiled in the stage, so the cache "
        "had no bytecode ready when the install ran"
    )


def test_a_wheel_built_from_an_sdist_installs_from_the_archive_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch holding one wheel built from a source archive took the staging
    route whole. The built wheel is hashed from its own bytes -- the sdist's
    hash names the archive, not the wheel -- and installs as any wheel does."""
    import filecmp

    from kpip.install import wheel_archive_installer, wheel_transaction
    from kpip.install.wheel_transaction import install_wheels_transactionally

    built = _make_wheel(tmp_path, "builtpkg", shared_module="builtpkg.py")
    plain = _make_wheel(tmp_path, "plainpkg", shared_module="plainpkg.py")
    candidates = [
        wheel_candidate(built).copy_with(
            source_kind="sdist", source_hashes={"sha256": "0" * 64}
        ),
        wheel_candidate(plain).copy_with(source_kind="wheel"),
    ]
    routes: list[str] = []
    real = wheel_archive_installer.install_wheels_from_archive_cache

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        routes.append("archive" if result is not None else "declined")
        return result

    monkeypatch.setattr(wheel_transaction, "install_wheels_from_archive_cache", spy)
    requests = [(built, True, None), (plain, True, None)]

    install_wheels_transactionally(
        requests,
        target=InstallTarget.from_options("builtpkg", target=str(tmp_path / "cached")),
        pycompile=False,
        candidates=candidates,
        cache_dir=str(tmp_path / "cache"),
    )
    install_wheels_transactionally(
        requests,
        target=InstallTarget.from_options("builtpkg", target=str(tmp_path / "staged")),
        pycompile=False,
        candidates=candidates,
    )

    assert routes == ["archive"]
    comparison = filecmp.dircmp(tmp_path / "cached", tmp_path / "staged")
    assert not comparison.left_only
    assert not comparison.right_only
    assert (tmp_path / "cached" / "builtpkg.py").read_text() == "# from builtpkg\n"
