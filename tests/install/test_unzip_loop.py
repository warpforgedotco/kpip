"""The C member extraction loop must extract what the Python path extracts.

``_kpip_unzip.c`` is built into the binary; built here as an extension with
zlib, the archive cache's member list is extracted through it and through
the Python path alone, and the two must write the same files, with the
same modes, and give the same entries. A member it hands back is extracted
by Python, raising what Python raises.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import types
import zipfile
from pathlib import Path

import pytest
from kpip.install import wheel_archive_cache as cache

SOURCE = Path(cache.__file__).parents[1] / "host" / "_accel" / "_kpip_unzip.c"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the C loop is built where there is pread"
)


@pytest.fixture(scope="module")
def unzip(tmp_path_factory: pytest.TempPathFactory) -> types.ModuleType:
    directory = tmp_path_factory.mktemp("unzip")
    output = directory / ("_kpip_unzip" + sysconfig.get_config_var("EXT_SUFFIX"))
    configured = shlex.split(sysconfig.get_config_var("CC") or "cc")
    compiler = shutil.which(configured[0]) or shutil.which("cc")
    if compiler is None:
        if os.environ.get("CI"):
            pytest.fail("no C compiler to build _kpip_unzip.c with")
        pytest.skip("no C compiler")
    command = [
        compiler,
        "-O2",
        "-Wall",
        "-Werror",
        "-fPIC",
        "-DKPIP_UNZIP_ZLIB",
        "-I",
        sysconfig.get_path("include"),
        str(SOURCE),
        "-o",
        str(output),
        "-lz",
        *(
            ["-bundle", "-undefined", "dynamic_lookup"]
            if sys.platform == "darwin"
            else ["-shared"]
        ),
    ]
    built = subprocess.run(command, capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stdout + built.stderr
    spec = importlib.util.spec_from_file_location("_kpip_unzip", output)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEMBERS = {
    "pkg/__init__.py": (b"VALUE = 1\n" * 200, zipfile.ZIP_DEFLATED, 0o644),
    "pkg/data.bin": (bytes(range(256)) * 40, zipfile.ZIP_STORED, 0o644),
    "pkg/empty.txt": (b"", zipfile.ZIP_DEFLATED, 0o644),
    "pkg/sub/deep.py": (b"x = 'deep'\n", zipfile.ZIP_DEFLATED, 0o644),
    "pkg/tool": (b"#!/bin/sh\necho hi\n", zipfile.ZIP_DEFLATED, 0o755),
    "pkg/ünïcode.py": (b"name = 1\n", zipfile.ZIP_DEFLATED, 0o644),
}


def _wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, (data, method, mode) in MEMBERS.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = method
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, data)
    return path


def _record(data: bytes) -> tuple[str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode()}", str(len(data))


def _work(archive: zipfile.ZipFile, root: Path, *, hints: bool = True) -> list:
    work = []
    for member in archive.infolist():
        destination = root / member.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        hint = _record(MEMBERS[member.filename][0]) if hints else None
        work.append((member, member.filename, str(destination), hint))
    return work


def _extract(
    monkeypatch: pytest.MonkeyPatch, wheel: Path, root: Path, loop, *, hints=True
) -> list:
    monkeypatch.setattr(cache, "_extract_loop", loop)
    with zipfile.ZipFile(wheel) as archive:
        fd = os.open(wheel, os.O_RDONLY)
        try:
            return cache._extract_members(
                archive, _work(archive, root, hints=hints), fd
            )
        finally:
            os.close(fd)


def _written(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("hints", [True, False])
def test_the_c_loop_extracts_what_python_extracts(
    unzip: types.ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hints: bool,
) -> None:
    """Without RECORD hashes every member is handed back to Python; with
    them, none is."""
    wheel = _wheel(tmp_path / "demo-1.0-py3-none-any.whl")
    handed_back: list[str] = []
    extract_member = cache._extract_member

    def counted(archive, item, fd=-1):
        handed_back.append(item[1])
        return extract_member(archive, item, fd)

    monkeypatch.setattr(cache, "_extract_member", counted)

    in_c = _extract(
        monkeypatch, wheel, tmp_path / "c", unzip.extract_members, hints=hints
    )
    assert len(handed_back) == (0 if hints else len(MEMBERS))
    in_python = _extract(monkeypatch, wheel, tmp_path / "python", None, hints=hints)

    assert in_c == in_python
    assert _written(tmp_path / "c") == _written(tmp_path / "python")
    assert set(_written(tmp_path / "c")) == set(MEMBERS)


def test_a_member_with_bad_data_is_left_to_python_to_refuse(
    unzip: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel(tmp_path / "demo-1.0-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as archive:
        member = archive.getinfo("pkg/data.bin")
    contents = bytearray(wheel.read_bytes())
    name_size = int.from_bytes(
        contents[member.header_offset + 26 : member.header_offset + 28], "little"
    )
    extra_size = int.from_bytes(
        contents[member.header_offset + 28 : member.header_offset + 30], "little"
    )
    data_start = member.header_offset + 30 + name_size + extra_size
    contents[data_start] ^= 0xFF
    wheel.write_bytes(bytes(contents))

    with pytest.raises(zipfile.BadZipFile):
        _extract(monkeypatch, wheel, tmp_path / "c", unzip.extract_members)


WHEEL_FILES = {
    "demo/__init__.py": (b"VALUE = 1\n" * 300, zipfile.ZIP_DEFLATED, 0o644),
    "demo/data.bin": (bytes(range(256)) * 50, zipfile.ZIP_STORED, 0o644),
    "demo/empty.txt": (b"", zipfile.ZIP_DEFLATED, 0o644),
    "demo/a/b/c/deep.py": (b"x = 'deep'\n", zipfile.ZIP_DEFLATED, 0o644),
    "demo/a/side.py": (b"y = 2\n", zipfile.ZIP_DEFLATED, 0o644),
    "demo/tool": (b"#!/bin/sh\necho hi\n", zipfile.ZIP_DEFLATED, 0o755),
    "demo/ünïcode.py": (b"name = 1\n", zipfile.ZIP_DEFLATED, 0o644),
    "demo_top.py": (b"top = 1\n", zipfile.ZIP_DEFLATED, 0o644),
    "demo-1.0.dist-info/METADATA": (
        b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        zipfile.ZIP_DEFLATED,
        0o644,
    ),
    "demo-1.0.dist-info/WHEEL": (
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        zipfile.ZIP_DEFLATED,
        0o644,
    ),
}


def _full_wheel(directory: Path, *, extra: dict | None = None, zip64=False) -> Path:
    """A wheel whose RECORD hashes all but one of its files, with directory
    entries as some builders write them."""
    wheel = directory / "demo-1.0-py3-none-any.whl"
    files = dict(WHEEL_FILES)
    rows = [
        f"{name},{','.join(_record(data))}"
        for name, (data, _, _) in files.items()
        if name != "demo/empty.txt"
    ]
    rows.append("demo/empty.txt,,")
    rows.append("demo-1.0.dist-info/RECORD,,")
    files["demo-1.0.dist-info/RECORD"] = (
        ("\n".join(rows) + "\n").encode(),
        zipfile.ZIP_DEFLATED,
        0o644,
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(zipfile.ZipInfo("demo/"), b"")
        archive.writestr(zipfile.ZipInfo("demo/a/"), b"")
        for name, (data, method, mode) in {**files, **(extra or {})}.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = method
            info.external_attr = (stat.S_IFREG | mode) << 16
            if zip64:
                with archive.open(info, "w", force_zip64=True) as member:
                    member.write(data)
            else:
                archive.writestr(info, data)
    return wheel


def _candidate(wheel: Path) -> object:
    from kpip.core.wheel import wheel_candidate

    return wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )


def _fill(
    monkeypatch: pytest.MonkeyPatch, wheel: Path, cache_dir: Path, unpack: object
) -> tuple[object, dict[str, tuple[bytes, int]], object]:
    monkeypatch.setattr(cache, "_unpack", unpack)
    (archive,) = cache.prepare_cached_wheels((_candidate(wheel),), str(cache_dir))
    listings = cache._read_listings(
        os.path.join(os.path.dirname(archive.tree), cache.LISTING_NAME)
    )
    return archive, _written(Path(archive.tree)), listings


@pytest.mark.parametrize("zip64", [False, True], ids=["zip", "zip64"])
def test_unpacking_in_c_fills_the_entry_python_fills(
    unzip: types.ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zip64: bool,
) -> None:
    wheel = _full_wheel(tmp_path, zip64=zip64)
    fd = os.open(wheel, os.O_RDONLY)
    try:
        tree = tmp_path / "direct"
        tree.mkdir()
        assert unzip.unpack(fd, str(tree), 1 << 24) is not None
    finally:
        os.close(fd)

    python = _fill(monkeypatch, wheel, tmp_path / "python", None)
    c = _fill(monkeypatch, wheel, tmp_path / "c", unzip.unpack)

    assert c[0].dist_info == python[0].dist_info == "demo-1.0.dist-info"
    assert c[0].entries == python[0].entries
    assert c[1] == python[1]
    assert c[2] == python[2]


@pytest.mark.parametrize(
    "name",
    ["../evil.py", "/abs.py", "demo\\back.py", "demo/./../../evil.py", "demo/"],
)
def test_a_path_python_refuses_is_left_to_python_and_nothing_is_written(
    unzip: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from kpip.core.errors import InstallationError

    wheel = _full_wheel(tmp_path, extra={name + "x": (b"x", zipfile.ZIP_STORED, 0o644)})
    tree = tmp_path / "tree"
    tree.mkdir()
    fd = os.open(wheel, os.O_RDONLY)
    try:
        refused = unzip.unpack(fd, str(tree), 1 << 24)
    finally:
        os.close(fd)

    if name == "demo/":
        # demo/x is an ordinary member.
        assert refused is not None
        return
    assert refused is None
    assert list(tree.iterdir()) == []

    errors = []
    for unpack in (None, unzip.unpack):
        monkeypatch.setattr(cache, "_unpack", unpack)
        with pytest.raises(InstallationError) as raised:
            cache.prepare_cached_wheels(
                (_candidate(wheel),), str(tmp_path / f"cache-{unpack is None}")
            )
        errors.append(str(raised.value))
    assert errors[0] == errors[1]


def test_a_duplicate_member_is_left_to_python(
    unzip: types.ModuleType, tmp_path: Path
) -> None:
    wheel = _full_wheel(tmp_path)
    with (
        zipfile.ZipFile(wheel, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr("demo/a/side.py", b"again\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    fd = os.open(wheel, os.O_RDONLY)
    try:
        assert unzip.unpack(fd, str(tree), 1 << 24) is None
    finally:
        os.close(fd)
    assert list(tree.iterdir()) == []


def test_data_before_the_archive_is_left_to_python(
    unzip: types.ModuleType, tmp_path: Path
) -> None:
    wheel = _full_wheel(tmp_path)
    prefixed = tmp_path / "prefixed.whl"
    prefixed.write_bytes(b"#!/bin/sh\n" + wheel.read_bytes())
    tree = tmp_path / "tree"
    tree.mkdir()
    fd = os.open(prefixed, os.O_RDONLY)
    try:
        assert unzip.unpack(fd, str(tree), 1 << 24) is None
    finally:
        os.close(fd)
