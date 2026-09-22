from __future__ import annotations

import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from kpip.build import build
from kpip.build.build_backend import (
    BackendSpec,
    ProjectBuilder,
    ProjectMetadataReader,
    build_sdist,
    prepare_project_metadata,
)
from kpip.core.errors import BuildError
from kpip.index.candidate_materialization import validate_build_requirements


def test_build_backend_builds_static_wheel_with_typed_marker(tmp_path: Path) -> None:
    project = write_project(tmp_path, "typed-pkg", "typed_pkg", "1.0")
    (project / "src" / "typed_pkg" / "py.typed").write_text("", encoding="utf-8")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    assert wheel_name == "typed_pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        assert "typed_pkg/__init__.py" in archive.namelist()
        assert "typed_pkg/py.typed" in archive.namelist()
        metadata = archive.read("typed_pkg-1.0.dist-info/METADATA").decode()
    assert "Name: typed-pkg\n" in metadata
    assert "Version: 1.0\n" in metadata


def test_legacy_metadata_reads_egg_info_requirements(tmp_path: Path) -> None:
    project = tmp_path / "legacy-pkg"
    egg_info = project / "legacy_pkg.egg-info"
    egg_info.mkdir(parents=True)
    (project / "PKG-INFO").write_text(
        "Metadata-Version: 1.0\nName: legacy-pkg\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (egg_info / "PKG-INFO").write_text(
        "Metadata-Version: 1.0\nName: legacy-pkg\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (egg_info / "requires.txt").write_text(
        "dependency>=2\n[extra]\noptional>=1\n",
        encoding="utf-8",
    )

    metadata = ProjectMetadataReader(project).read()

    assert metadata.dependencies == (
        "dependency>=2",
        'optional>=1; extra == "extra"',
    )


def test_build_backend_includes_package_data(tmp_path: Path) -> None:
    project = write_project(tmp_path, "data-pkg", "data_pkg", "1.0")
    package_dir = project / "src" / "data_pkg"
    package_dir.joinpath("payload.dat").write_text("Data\n", encoding="utf-8")
    package_dir.joinpath(".hidden").write_text("Hidden\n", encoding="utf-8")
    pycache_dir = package_dir / "__pycache__"
    pycache_dir.mkdir()
    pycache_dir.joinpath("module.pyc").write_bytes(b"bytecode")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        names = set(archive.namelist())
        assert "data_pkg/payload.dat" in names
        assert "data_pkg/.hidden" not in names
        assert "data_pkg/__pycache__/module.pyc" not in names


def test_build_backend_packages_pep639_license_metadata(tmp_path: Path) -> None:
    project = write_project(tmp_path, "licensed-pkg", "licensed_pkg", "1.0")
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8")
        + 'license = "MIT"\nlicense-files = ["LICENSE.txt"]\n',
        encoding="utf-8",
    )
    project.joinpath("LICENSE.txt").write_text("license text\n", encoding="utf-8")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        metadata = archive.read("licensed_pkg-1.0.dist-info/METADATA").decode()
        license_text = archive.read(
            "licensed_pkg-1.0.dist-info/licenses/LICENSE.txt",
        ).decode()
    assert "Metadata-Version: 2.4\n" in metadata
    assert "License-Expression: MIT\n" in metadata
    assert "License-File: LICENSE.txt\n" in metadata
    assert license_text == "license text\n"


def test_build_backend_packages_readme_and_project_metadata(tmp_path: Path) -> None:
    project = write_project(tmp_path, "described-pkg", "described_pkg", "1.0")
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8")
        + 'readme = "README.md"\n'
        + 'authors = [{ name = "Example Author", email = "author@example.com" }]\n'
        + 'classifiers = ["Development Status :: 3 - Alpha"]\n'
        + '[project.urls]\nSource = "https://example.com/source"\n',
        encoding="utf-8",
    )
    project.joinpath("README.md").write_text(
        "# Described package\n",
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        metadata = archive.read("described_pkg-1.0.dist-info/METADATA").decode()
    assert "Author-email: Example Author <author@example.com>\n" in metadata
    assert "Classifier: Development Status :: 3 - Alpha\n" in metadata
    assert "Project-URL: Source, https://example.com/source\n" in metadata
    assert "Description-Content-Type: text/markdown\n" in metadata
    assert metadata.endswith("\n\n# Described package\n")


def test_build_backend_builds_editable_wheel(tmp_path: Path) -> None:
    project = write_project(tmp_path, "editable-pkg", "editable_pkg", "1.0")
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_editable(wheel_dir)

    assert wheel_name == "editable_pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        pth = archive.read("__editable__.editable_pkg.pth").decode()
        assert pth == str((project / "src").resolve()) + "\n"
        assert "editable_pkg-1.0.dist-info/METADATA" in archive.namelist()
        assert "editable_pkg-1.0.dist-info/RECORD" in archive.namelist()


def test_build_sdist_honors_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = write_project(tmp_path, "source-pkg", "source_pkg", "1.0")
    project.joinpath(".gitignore").write_text(".venv/\ndist/\n", encoding="utf-8")
    ignored_python = project / ".venv" / "bin" / "python"
    ignored_python.parent.mkdir(parents=True)
    ignored_python.write_text("not part of the source release", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    sdist_dir = tmp_path / "artifacts"
    monkeypatch.chdir(project)

    sdist_name = build_sdist(str(sdist_dir))

    with tarfile.open(sdist_dir / sdist_name) as archive:
        names = set(archive.getnames())
        pkg_info = archive.extractfile("source_pkg-1.0/PKG-INFO")
        assert pkg_info is not None
        metadata = pkg_info.read().decode()
    assert "source_pkg-1.0/pyproject.toml" in names
    assert "source_pkg-1.0/src/source_pkg/__init__.py" in names
    assert "Name: source-pkg\n" in metadata
    assert "Version: 1.0\n" in metadata
    assert "Metadata-Version: 2.2\n" in metadata
    assert not any("/.venv/" in name for name in names)


def test_build_sdist_honors_project_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = write_project(tmp_path, "source-pkg", "source_pkg", "1.0")
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8")
        + '[tool.kpip.build-backend]\nsdist-exclude = ["fixtures/**"]\n',
        encoding="utf-8",
    )
    project.joinpath("fixtures").mkdir()
    project.joinpath("fixtures", "large.whl").write_bytes(b"fixture")
    project.joinpath("tests").mkdir()
    project.joinpath("tests", "test_source.py").write_text("", encoding="utf-8")
    sdist_dir = tmp_path / "artifacts"
    monkeypatch.chdir(project)

    sdist_name = build_sdist(str(sdist_dir))

    with tarfile.open(sdist_dir / sdist_name) as archive:
        names = set(archive.getnames())
    assert "source_pkg-1.0/tests/test_source.py" in names
    assert "source_pkg-1.0/fixtures/large.whl" not in names


def test_build_backend_reads_setup_py_console_scripts(tmp_path: Path) -> None:
    project = tmp_path / "script-pkg"
    project.mkdir()
    project.joinpath("script_pkg.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "\n".join(
            [
                "from setuptools import setup",
                "setup(",
                '    name="script-pkg",',
                '    version="1.0",',
                '    py_modules=["script_pkg"],',
                "    entry_points=dict("
                'console_scripts=["script-pkg=script_pkg:main"]),',
                ")",
                "",
            ],
        ),
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    with zipfile.ZipFile(wheel_dir / wheel_name) as archive:
        entry_points = archive.read(
            "script_pkg-1.0.dist-info/entry_points.txt",
        ).decode()
    assert "script-pkg = script_pkg:main" in entry_points


def test_build_backend_uses_setuptools_for_dynamic_legacy_metadata(
    tmp_path: Path,
) -> None:
    project = tmp_path / "dynamic-pkg"
    project.mkdir()
    project.joinpath("dynamic_pkg.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "\n".join(
            [
                "from setuptools import setup",
                "",
                "def project_version():",
                "    return '4.1.3'",
                "",
                "setup(",
                "    name='dynamic-pkg',",
                "    version=project_version(),",
                "    py_modules=['dynamic_pkg'],",
                ")",
                "",
            ],
        ),
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    metadata = prepare_project_metadata(project)
    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    assert metadata.name == "dynamic-pkg"
    assert metadata.version == "4.1.3"
    assert wheel_name == "dynamic_pkg-4.1.3-py3-none-any.whl"


def test_build_backend_defaults_to_setuptools_when_backend_is_omitted(
    tmp_path: Path,
) -> None:
    project = tmp_path / "default-backend-pkg"
    project.mkdir()
    project.joinpath("default_backend_pkg.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='default-backend-pkg', version='2.3.4', py_modules=['default_backend_pkg'])\n",
        encoding="utf-8",
    )
    wheel_dir = tmp_path / "wheelhouse"

    metadata = prepare_project_metadata(project)
    wheel_name = ProjectBuilder(project).build_wheel(wheel_dir)

    assert metadata.name == "default-backend-pkg"
    assert metadata.version == "2.3.4"
    assert wheel_name == "default_backend_pkg-2.3.4-py3-none-any.whl"


def test_declared_setuptools_backend_keeps_pkg_resources_available(
    tmp_path: Path,
) -> None:
    project = tmp_path / "declared-backend-pkg"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools>=40.8.0', 'wheel']\n"
        "build-backend = 'setuptools.build_meta:__legacy__'\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "from pkg_resources import parse_version\n"
        "from setuptools import setup\n"
        "setup(name='declared-backend-pkg', version=str(parse_version('1.0')))\n",
        encoding="utf-8",
    )

    spec = BackendSpec.from_project(project)

    assert spec is not None
    assert spec.requirements == (
        "setuptools>=40.8.0",
        "wheel",
        "setuptools<82",
    )


def test_newer_setuptools_build_requirement_is_valid(tmp_path: Path) -> None:
    project = tmp_path / "new-setuptools-pkg"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools>=64']\n",
        encoding="utf-8",
    )

    validate_build_requirements(project)


def test_static_source_metadata_precedes_backend_execution(tmp_path: Path) -> None:
    project = tmp_path / "static-metadata-pkg"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'missing_backend'\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "raise RuntimeError('backend should not execute for metadata')\n",
        encoding="utf-8",
    )
    project.joinpath("PKG-INFO").write_text(
        # 2.2 is where PEP 643 makes these fields binding on the built wheel;
        # anything older is a guess the backend is free to contradict.
        "Metadata-Version: 2.2\nName: static-metadata-pkg\nVersion: 1.2.3\n"
        "Requires-Dist: dependency>=2\n",
        encoding="utf-8",
    )

    metadata = prepare_project_metadata(project)

    assert metadata.name == "static-metadata-pkg"
    assert metadata.version == "1.2.3"
    assert metadata.dependencies == ("dependency>=2",)


def test_legacy_setup_projects_use_pkg_resources_compatible_setuptools(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy-pkg"
    project.mkdir()
    project.joinpath("setup.py").write_text(
        "from pkg_resources import parse_version\n"
        "from setuptools import setup\n"
        "setup(name='legacy-pkg', version=str(parse_version('1.0')))\n",
        encoding="utf-8",
    )

    spec = BackendSpec.from_project(project)

    assert spec is not None
    assert spec.requirements == ("setuptools>=40.8.0,<82",)


def test_build_backend_rejects_invalid_package_version(tmp_path: Path) -> None:
    project = tmp_path / "bad-version-pkg"
    package_dir = project / "src" / "bad_version_pkg"
    package_dir.mkdir(parents=True)
    package_dir.joinpath("__init__.py").write_text(
        '__version__ = "not-a-version"\n',
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text("", encoding="utf-8")

    with pytest.raises(BuildError, match="use the project's build backend"):
        ProjectMetadataReader(project).read()


def test_default_wheel_directories_are_isolated() -> None:
    first = build.default_wheel_dir()
    second = build.default_wheel_dir()

    assert first != second
    assert os.path.isdir(first)
    assert os.path.isdir(second)


def write_project(tmp_path: Path, name: str, package: str, version: str) -> Path:
    project = tmp_path / name
    package_dir = project / "src" / package
    package_dir.mkdir(parents=True)
    package_dir.joinpath("__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    project.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                f'version = "{version}"',
                "",
            ],
        ),
        encoding="utf-8",
    )
    return project


def _unbuildable_project(tmp_path: Path, name: str) -> Path:
    """A project whose backend raises if anything tries to run it."""
    project = tmp_path / name
    project.mkdir()
    project.joinpath("setup.py").write_text(
        "raise RuntimeError('the backend should not have run')\n",
        encoding="utf-8",
    )
    return project


def test_pre_pep_643_metadata_does_not_stand_in_for_a_build(tmp_path: Path) -> None:
    """A source distribution's PKG-INFO records what the author's machine
    produced. Only from metadata 2.2 does PEP 643 bind it to what a wheel
    built from that sdist will say, so anything older has to be built."""
    project = _unbuildable_project(tmp_path, "old-metadata-pkg")
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'missing_backend'\n",
        encoding="utf-8",
    )
    project.joinpath("PKG-INFO").write_text(
        "Metadata-Version: 2.1\nName: old-metadata-pkg\nVersion: 1.0\n",
        encoding="utf-8",
    )

    with pytest.raises(BuildError):
        prepare_project_metadata(project)


def test_metadata_declaring_a_field_dynamic_does_not_stand_in_for_a_build(
    tmp_path: Path,
) -> None:
    project = _unbuildable_project(tmp_path, "dynamic-metadata-pkg")
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'missing_backend'\n",
        encoding="utf-8",
    )
    project.joinpath("PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: dynamic-metadata-pkg\nVersion: 1.0\n"
        "Dynamic: Requires-Dist\n",
        encoding="utf-8",
    )

    with pytest.raises(BuildError):
        prepare_project_metadata(project)


def test_pkg_info_provides_extra_is_reported(tmp_path: Path) -> None:
    project = _unbuildable_project(tmp_path, "extras-pkg")
    project.joinpath("PKG-INFO").write_text(
        "Metadata-Version: 2.3\nName: extras-pkg\nVersion: 1.0\n"
        "Provides-Extra: cli\nProvides-Extra: docs\n"
        'Requires-Dist: rich; extra == "cli"\n',
        encoding="utf-8",
    )

    metadata = prepare_project_metadata(project)

    assert metadata.provided_extras == frozenset({"cli", "docs"})


def test_a_static_project_table_precedes_backend_execution(tmp_path: Path) -> None:
    """A [project] table with nothing dynamic already says what the backend
    would say, so standing up a build environment to be told the same thing
    is pure cost -- paid once per candidate, per backtrack."""
    project = _unbuildable_project(tmp_path, "static-table-pkg")
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'missing_backend'\n"
        "[project]\nname = 'static-table-pkg'\nversion = '2.0'\n"
        "requires-python = '>=3.10'\n"
        "dependencies = ['downstream>=1']\n"
        "[project.optional-dependencies]\ncli = ['rich']\n",
        encoding="utf-8",
    )

    metadata = prepare_project_metadata(project)

    assert metadata.name == "static-table-pkg"
    assert metadata.version == "2.0"
    assert metadata.dependencies == ("downstream>=1",)
    assert metadata.requires_python == ">=3.10"
    assert metadata.provided_extras == frozenset({"cli"})


@pytest.mark.parametrize(
    "dynamic",
    ["dependencies", "optional-dependencies", "requires-python", "version"],
)
def test_a_dynamic_project_field_falls_through_to_the_backend(
    tmp_path: Path,
    dynamic: str,
) -> None:
    """Declaring a field dynamic hands it to the backend, so the table's
    answer for it -- including saying nothing at all -- is not the wheel's."""
    project = _unbuildable_project(tmp_path, f"dyn-{dynamic}-pkg")

    # PEP 621 forbids specifying a field statically *and* listing it in
    # dynamic, so the version case has to omit it -- writing both would model
    # a project no backend would accept and prove nothing.
    static_version = "" if dynamic == "version" else "version = '1.0'\n"

    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'missing_backend'\n"
        f"[project]\nname = 'dyn-pkg'\n{static_version}"
        f"dynamic = ['{dynamic}']\n",
        encoding="utf-8",
    )

    with pytest.raises(BuildError):
        prepare_project_metadata(project)


def test_an_unparsable_static_version_falls_through_to_the_backend(
    tmp_path: Path,
) -> None:
    """A version [project] declares but PEP 440 rejects is one the backend may
    still normalize, so it means "cannot answer" -- not an exception escaping
    prepare_metadata, where callers are waiting on BuildError to fall back."""
    project = _unbuildable_project(tmp_path, "badversion-pkg")
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n"
        "build-backend = 'missing_backend'\n"
        "[project]\nname = 'badversion-pkg'\nversion = 'not a version'\n",
        encoding="utf-8",
    )

    with pytest.raises(BuildError):
        prepare_project_metadata(project)


def test_pyproject_with_only_tool_tables_still_has_the_legacy_backend(
    tmp_path: Path,
) -> None:
    """PEP 518: an absent [build-system] means setuptools, not "no backend".

    A pyproject.toml carrying nothing but tool configuration is the common
    shape for a project that still builds through setup.py -- python-ldap
    ships exactly this -- and treating it as backendless left the project
    with no way to report its own metadata.
    """
    project = tmp_path / "tool-only-pkg"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(
        "[tool.black]\nline-length = 88\n",
        encoding="utf-8",
    )
    project.joinpath("setup.py").write_text(
        "from setuptools import setup\nsetup(name='tool-only-pkg', version='1.0')\n",
        encoding="utf-8",
    )

    spec = BackendSpec.from_project(project)

    assert spec is not None
    assert spec.name == "setuptools.build_meta:__legacy__"
    assert spec.setup_py_present is True


def test_pyproject_with_only_tool_tables_and_no_setup_py_has_no_backend(
    tmp_path: Path,
) -> None:
    project = tmp_path / "nothing-pkg"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(
        "[tool.black]\nline-length = 88\n",
        encoding="utf-8",
    )

    assert BackendSpec.from_project(project) is None
