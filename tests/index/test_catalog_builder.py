"""The C catalog builder must compile what the Python path compiles.

``_kpip_catalog.c`` is built into the binary; built here as an extension, a
page is compiled through it and through Python alone, whole and for one
release, and the two must give the same catalog, value and type, and the
same summary.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import types
import zipfile
from pathlib import Path

import pytest
from kpip.core.versions import Version
from kpip.index import catalog_cache, page_parsing
from kpip.index.page_parsing import IndexPageParser

SOURCE = Path(page_parsing.__file__).parents[1] / "_acceleration" / "_kpip_catalog.c"

CORPUS = Path(__file__).parents[1] / "benchmarks" / "corpus" / "uv_graphs"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory: pytest.TempPathFactory) -> types.ModuleType:
    directory = tmp_path_factory.mktemp("catalog")
    output = directory / ("_kpip_catalog" + sysconfig.get_config_var("EXT_SUFFIX"))
    include = sysconfig.get_path("include")
    if os.name == "nt":
        compiler = shutil.which("cl")
        command = [
            "/nologo",
            "/O2",
            "/W3",
            "/WX",
            "/LD",
            f"/I{include}",
            str(SOURCE),
            f"/Fo{directory}\\",
            f"/Fe{output}",
            "/link",
            f"/LIBPATH:{Path(sys.base_prefix) / 'libs'}",
        ]
    else:
        configured = shlex.split(sysconfig.get_config_var("CC") or "cc")
        compiler = shutil.which(configured[0]) or shutil.which("cc")
        command = [
            "-O2",
            "-Wall",
            "-Werror",
            "-fPIC",
            "-I",
            include,
            str(SOURCE),
            "-o",
            str(output),
            *(
                ["-bundle", "-undefined", "dynamic_lookup"]
                if sys.platform == "darwin"
                else ["-shared"]
            ),
        ]
    if compiler is None:
        if os.environ.get("CI"):
            pytest.fail("no C compiler to build _kpip_catalog.c with")
        pytest.skip("no C compiler")
    built = subprocess.run(
        [compiler, *command], capture_output=True, text=True, check=False
    )
    assert built.returncode == 0, built.stdout + built.stderr
    spec = importlib.util.spec_from_file_location("_kpip_catalog", output)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pages(step: int) -> list[tuple[str, str]]:
    pages = []
    for path in sorted(glob.glob(str(CORPUS / "*" / "pages.zip"))):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                body = archive.read(name).decode()
                if body.lstrip().startswith("{"):
                    pages.append((name, body))
    return pages[::step]


def _same(a: object, b: object, path: str = "catalog") -> str | None:
    """Where ``a`` and ``b`` first differ, in value or in type, or None."""
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple))
        if len(a) != len(b):
            return f"{path}: length {len(a)} vs {len(b)}"
        for index, (x, y) in enumerate(zip(a, b)):
            found = _same(x, y, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(a, dict):
        assert isinstance(b, dict)
        return None if list(a.items()) == list(b.items()) else f"{path}: dicts"
    return None if a == b else f"{path}: {a!r} vs {b!r}"


def _compiled(
    monkeypatch: pytest.MonkeyPatch,
    module: types.ModuleType | None,
    body: str,
    url: str,
    release: Version | None = None,
) -> tuple[object, object]:
    monkeypatch.setattr(
        page_parsing, "_compile_page", None if module is None else module.compile_page
    )
    monkeypatch.setattr(
        catalog_cache,
        "_earliest_upload_text",
        None if module is None else module.earliest_upload_text,
    )
    parser = IndexPageParser(trusted_hosts=(), session=None)
    built = parser.catalog_from_json(body, url, release=release)
    return built, catalog_cache.summary_from_catalog(built, "0" * 64)


def _releases(built: object) -> list[Version]:
    texts = sorted({group[1] for group in built[0]}, key=Version)  # ty: ignore[not-subscriptable]
    if len(texts) <= 3:
        return [Version(text) for text in texts]
    return [Version(texts[0]), Version(texts[len(texts) // 2]), Version(texts[-1])]


@pytest.mark.skipif(not CORPUS.is_dir(), reason="no page corpus")
def test_the_c_builder_compiles_the_corpus_as_python_does(
    catalog: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    differences = []
    for name, body in _pages(7):
        url = f"https://pypi.org/simple/{name.rsplit('.', 1)[0]}/"
        python = _compiled(monkeypatch, None, body, url)
        found = _same(_compiled(monkeypatch, catalog, body, url), python, name)
        if found:
            differences.append(found)
        for release in _releases(python[0]):
            pinned = _same(
                _compiled(monkeypatch, catalog, body, url, release),
                _compiled(monkeypatch, None, body, url, release),
                f"{name}=={release}",
            )
            if pinned:
                differences.append(pinned)
    assert differences == []


def _file(name: str, url: str | None = None, **fields: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "filename": name,
        "url": url or f"https://files.example.org/packages/{name}",
        "hashes": {"sha256": "a" * 64},
    }
    entry.update(fields)
    return entry


EDGE_FILES = [
    _file("demo-1.0-py3-none-any.whl", **{"core-metadata": {"sha256": "b" * 64}}),
    _file("demo-1.0-1-py3-none-any.whl", yanked="broken"),
    _file("demo-1.0.tar.gz", yanked=True, **{"requires-python": ">=3.9"}),
    _file("demo-1.0+local-cp312-cp312-linux_x86_64.whl", size=12),
    _file("my-demo-pkg-1.0.zip", **{"upload-time": "2024-01-02T03:04:05.000000Z"}),
    _file("demo-2.0-py3-none-any.whl", **{"dist-info-metadata": True}),
    _file("demo-2.0rc1-py3-none-any.whl", hashes={"sha256": "c" * 64, "md5": "d"}),
    _file("demo-latest.tar.gz"),
    _file("demo-1.0-weird.whl"),
    _file("Demo_Pkg-1.0-py3-none-any.whl", hashes={}),
    _file("demo-1.0-py3-none-any.whl.metadata"),
    _file(
        "demo-1.0-py3-none-any.whl",
        url="https://files.example.org/a%2Bb/demo-1.0-py3-none-any.whl",
    ),
    _file(
        "demo-1.0-py3-none-any.whl",
        url="https://files.example.org/x/demo-1.0-py3-none-any.whl#sha256=" + "e" * 64,
    ),
    _file("demo-1.0-py3-none-any.whl", url="../relative/demo-1.0-py3-none-any.whl"),
    _file("demo-1.0.tar.gz", url="HTTPS://files.example.org/upper/demo-1.0.tar.gz"),
    _file("", url="https://files.example.org/packages/demo-3.0-py3-none-any.whl"),
    _file("demo-not.a.version-py3-none-any.whl"),
    {"filename": "demo-4.0-py3-none-any.whl", "url": None},
    _file("demo-1.0-py3-none-any.whl", size=-1, **{"upload-time": 5}),
]


@pytest.mark.parametrize(
    "release", [None, Version("1.0"), Version("2.0"), Version("1.0.0")]
)
def test_the_c_builder_compiles_odd_files_as_python_does(
    catalog: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    release: Version | None,
) -> None:
    body = json.dumps({"meta": {"api-version": "1.1"}, "files": EDGE_FILES})
    url = "https://pypi.org/simple/demo/"

    python = _compiled(monkeypatch, None, body, url, release)
    c = _compiled(monkeypatch, catalog, body, url, release)

    assert _same(c, python) is None
