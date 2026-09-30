import json
import os
import textwrap
from pathlib import Path
from typing import Any, Protocol

import pytest
from kpip_test_support import (
    KpipTestEnvironment,
    TestData,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
    create_test_package,
    create_test_package_with_subdirectory,
    need_svn,
    requirements_file,
)
from kpip_test_support.local_repos import local_checkout


class ArgRecordingSdist:
    def __init__(self, sdist_path: Path, args_path: Path) -> None:
        self.sdist_path = sdist_path
        self.args_path_internal = args_path

    def args(self) -> Any:
        return json.loads(self.args_path_internal.read_text())


class ArgRecordingSdistMaker(Protocol):
    def __call__(self, name: str, **kwargs: Any) -> ArgRecordingSdist: ...


@pytest.fixture
def arg_recording_sdist_maker(
    script: KpipTestEnvironment,
) -> ArgRecordingSdistMaker:
    arg_writing_setup_py_prelude = textwrap.dedent("""
        import io
        import json
        import os
        import sys

        args_path = os.path.join(os.environ["OUTPUT_DIR"], "{name}.json")
        with open(args_path, 'w') as f:
            json.dump(sys.argv, f)
        """)
    output_dir = script.scratch_path.joinpath("args_recording_sdist_maker_output")
    output_dir.mkdir(parents=True)
    script.environ["OUTPUT_DIR"] = str(output_dir)

    def arg_recording_sdist_maker_internal(
        name: str,
        **kwargs: Any,
    ) -> ArgRecordingSdist:
        sdist_path = create_basic_sdist_for_package(
            script,
            name,
            "0.1.0",
            setup_py_prelude=arg_writing_setup_py_prelude.format(name=name),
            **kwargs,
        )
        args_path = output_dir / f"{name}.json"
        return ArgRecordingSdist(sdist_path, args_path)

    return arg_recording_sdist_maker_internal


def test_requirements_file(script: KpipTestEnvironment, data: TestData) -> None:
    """Test installing from a requirements file."""
    file = script.temporary_multiline_file(
        "initools-req.txt",
        """\
        INITools==0.2
        # and something else to test out:
        six>=1.0.0
        """,
    )
    script.kpip_install_local(
        "-r",
        file,
        find_links=[data.pypi_packages, data.common_wheels],
    )
    script.assert_installed(initools="0.2", six="1.17.0")


@pytest.mark.parametrize(
    "path, groupname",
    [
        (None, "reqs"),
        ("pyproject.toml", "reqs"),
        ("./pyproject.toml", "reqs"),
        (lambda path: path.absolute(), "reqs"),
    ],
)
def test_dependency_group(
    script: KpipTestEnvironment,
    path: Any,
    groupname: str,
) -> None:
    """Test installing from a dependency group."""
    pyproject = script.temporary_multiline_file(
        "pyproject.toml",
        """\
            [dependency-groups]
            reqs = [
                "simple.dist==0.1",
                "simplewheel<2",
            ]
            """,
    )
    if path is None:
        arg = groupname
    else:
        if callable(path):
            path = path(pyproject)
        arg = f"{path}:{groupname}"
    script.kpip_install_local("--group", arg)
    script.assert_installed(**{"simple.dist": "0.1", "simplewheel": "1.0"})


def test_multiple_dependency_groups(script: KpipTestEnvironment) -> None:
    """Test installing from two dependency groups simultaneously."""
    script.temporary_multiline_file(
        "pyproject.toml",
        """\
            [dependency-groups]
            simple = ["simple.dist==0.1"]
            othersimple = ["simplewheel<2"]
            """,
    )
    script.kpip_install_local("--group", "simple", "--group", "othersimple")
    script.assert_installed(**{"simple.dist": "0.1", "simplewheel": "1.0"})


def test_dependency_group_with_non_normalized_name(script: KpipTestEnvironment) -> None:
    """Test installing from a dependency group with a non-normalized name, verifying that
    the pyproject.toml content and CLI arg are normalized to match.
    """
    script.temporary_multiline_file(
        "pyproject.toml",
        """\
            [dependency-groups]
            INITOOLS = ["simplewheel==1.0"]
            """,
    )
    script.kpip_install_local("--group", "IniTools")
    script.assert_installed(simplewheel="1.0")


def test_schema_check_in_requirements_file(script: KpipTestEnvironment) -> None:
    """Test installing from a requirements file with an invalid vcs schema.."""
    script.scratch_path.joinpath("file-egg-req.txt").write_text(
        "\n{}\n".format(
            "git://github.com/alex/django-fixture-generator.git#egg=fixture_generator",
        ),
    )

    with pytest.raises(AssertionError):
        script.kpip("install", "-vvv", "-r", script.scratch_path / "file-egg-req.txt")


@pytest.mark.parametrize(
    "test_type,editable",
    [
        ("rel_path", False),
        ("rel_path", True),
        ("rel_url", False),
        ("rel_url", True),
        ("embedded_rel_path", False),
        ("embedded_rel_path", True),
    ],
)
def test_relative_requirements_file(
    script: KpipTestEnvironment,
    data: TestData,
    test_type: str,
    editable: bool,
) -> None:
    """Test installing from a requirements file with a relative path. For path
    URLs, use an egg= definition.

    """
    dist_info_folder = script.site_packages / "fspkg-0.1.dev0.dist-info"
    package_folder = script.site_packages / "fspkg"

    full_rel_path = os.path.relpath(
        data.packages.joinpath("FSPkg"),
        script.scratch_path,
    )
    full_rel_url = "file:" + full_rel_path + "#egg=FSPkg"
    embedded_rel_path = script.scratch_path.joinpath(full_rel_path)

    req_path = {
        "rel_path": full_rel_path,
        "rel_url": full_rel_url,
        "embedded_rel_path": os.fspath(embedded_rel_path),
    }[test_type]

    req_path = req_path.replace(os.path.sep, "/")
    if not editable:
        with requirements_file(req_path + "\n", script.scratch_path) as reqs_file:
            result = script.kpip(
                "install",
                "--no-build-isolation",
                "-vvv",
                "-r",
                reqs_file.name,
                cwd=script.scratch_path,
            )
            result.did_create(dist_info_folder)
            result.did_create(package_folder)
    else:
        with requirements_file(
            "-e " + req_path + "\n",
            script.scratch_path,
        ) as reqs_file:
            result = script.kpip(
                "install",
                "--no-build-isolation",
                "-vvv",
                "-r",
                reqs_file.name,
                cwd=script.scratch_path,
            )
            direct_url = result.get_created_direct_url("fspkg")
            assert direct_url
            assert direct_url.is_local_editable()


@pytest.mark.xfail
@pytest.mark.network
@need_svn
def test_multiple_requirements_files(script: KpipTestEnvironment, tmpdir: Path) -> None:
    """Test installing from multiple nested requirements files."""
    other_lib_name, other_lib_version = "six", "1.16.0"
    script.scratch_path.joinpath("initools-req.txt").write_text(
        textwrap.dedent("""
            -e {}@10#egg=INITools
            -r {}-req.txt
        """).format(
            local_checkout("svn+http://svn.colorstudy.com/INITools", tmpdir),
            other_lib_name,
        ),
    )
    script.scratch_path.joinpath(f"{other_lib_name}-req.txt").write_text(
        f"{other_lib_name}<={other_lib_version}",
    )
    result = script.kpip("install", "-r", script.scratch_path / "initools-req.txt")
    assert result.files_created[script.site_packages / other_lib_name].dir
    fn = f"{other_lib_name}-{other_lib_version}.dist-info"
    assert result.files_created[script.site_packages / fn].dir
    result.did_create(script.venv / "src" / "initools")


def test_package_in_constraints_and_dependencies(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("constraints.txt").write_text(
        "TopoRequires2==0.0.1\nTopoRequires==0.0.1",
    )
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        "TopoRequires2",
    )
    assert "installed TopoRequires-0.0.1" in result.stdout


def test_constraints_apply_to_dependency_groups(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("constraints.txt").write_text("TopoRequires==0.0.1")
    pyproject = script.scratch_path / "pyproject.toml"
    pyproject.write_text(
        textwrap.dedent("""\
            [dependency-groups]
            mylibs = ["TopoRequires2"]
            """),
    )
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        "--group",
        "mylibs",
    )
    assert "installed TopoRequires-0.0.1" in result.stdout


def test_multiple_constraints_files(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("outer.txt").write_text("-c inner.txt")
    script.scratch_path.joinpath("inner.txt").write_text("Upper==1.0")
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "outer.txt",
        "Upper",
    )
    assert "installed Upper-1.0" in result.stdout


def test_respect_order_in_requirements_file(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("frameworks-req.txt").write_text(
        textwrap.dedent("""\
        parent
        child
        simple
        """),
    )

    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-r",
        script.scratch_path / "frameworks-req.txt",
    )

    downloaded = [line for line in result.stdout.split("\n") if "Processing" in line]

    assert "parent" in downloaded[0], (
        f'First download should be "parent" but was "{downloaded[0]}"'
    )
    assert "child" in downloaded[1], (
        f'Second download should be "child" but was "{downloaded[1]}"'
    )
    assert "simple" in downloaded[2], (
        f'Third download should be "simple" but was "{downloaded[2]}"'
    )


def test_install_local_editable_with_extras(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.packages.joinpath("LocalExtras")
    res = script.kpip_install_local(
        "-e",
        f"{to_install}[bar]",
        allow_stderr_warning=True,
    )
    res.assert_installed("LocalExtras", editable=True, editable_vcs=False)
    res.assert_installed("simple", editable=False)


def test_install_collected_dependencies_first(script: KpipTestEnvironment) -> None:
    result = script.kpip_install_local(
        "toporequires2",
    )
    text = [line for line in result.stdout.split("\n") if "Installing" in line][0]
    assert text.endswith("toporequires2")


@pytest.mark.network
def test_install_local_editable_with_subdirectory(script: KpipTestEnvironment) -> None:
    version_pkg_path = create_test_package_with_subdirectory(script, "version_subdir")
    result = script.kpip(
        "install",
        "-e",
        "{uri}#egg=version_subpkg&subdirectory=version_subdir".format(
            uri=f"git+{version_pkg_path.as_uri()}",
        ),
    )

    result.assert_installed("version_subpkg", sub_dir="version_subdir")


@pytest.mark.network
def test_install_local_with_subdirectory(script: KpipTestEnvironment) -> None:
    version_pkg_path = create_test_package_with_subdirectory(script, "version_subdir")
    result = script.kpip(
        "install",
        "{uri}#egg=version_subpkg&subdirectory=version_subdir".format(
            uri=f"git+{version_pkg_path.as_uri()}",
        ),
    )

    result.assert_installed("version_subpkg.py", editable=False)


@pytest.mark.usefixtures("enable_user_site")
def test_wheel_user_with_prefix_in_pydistutils_cfg(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    if os.name == "posix":
        user_filename = ".pydistutils.cfg"
    else:
        user_filename = "pydistutils.cfg"
    user_cfg = os.path.join(os.path.expanduser("~"), user_filename)
    script.scratch_path.joinpath("bin").mkdir()
    with open(user_cfg, "w") as cfg:
        cfg.write(
            textwrap.dedent(f"""
            [install]
            prefix={script.scratch_path}"""),
        )

    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--user",
        "--no-index",
        "-f",
        data.find_links,
        "requiresupper",
    )
    assert "installed requiresupper" in result.stdout


def test_constraints_not_installed_by_default(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("c.txt").write_text("requiresupper")
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "c.txt",
        "Upper",
    )
    assert "requiresupper" not in result.stdout


def test_constraints_only_causes_error(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("c.txt").write_text("requiresupper")
    result = script.kpip(
        "install",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "c.txt",
        expect_error=True,
    )
    assert "installed requiresupper" not in result.stdout


def test_constraints_local_editable_install_causes_error(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("constraints.txt").write_text("singlemodule==0.0.0")
    to_install = data.src.joinpath("singlemodule")
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        "-e",
        to_install,
        expect_error=True,
    )
    assert "Cannot install singlemodule 0.0.1" in result.stderr, str(result)


@pytest.mark.network
def test_constraints_local_editable_install_pep518(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.src.joinpath("pep518-3.0")

    script.kpip("download", "setuptools", "-d", data.packages)
    script.kpip("install", "--no-index", "-f", data.find_links, "-e", to_install)


def test_constraints_local_install_causes_error(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.scratch_path.joinpath("constraints.txt").write_text("singlemodule==0.0.0")
    to_install = data.src.joinpath("singlemodule")
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        to_install,
        expect_error=True,
    )
    assert "No matching distribution found for singlemodule" in result.stderr, str(
        result
    )


def test_constraints_constrain_to_local_editable(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.src.joinpath("singlemodule")
    script.scratch_path.joinpath("constraints.txt").write_text(
        f"-e {to_install.as_uri()}#egg=singlemodule",
    )
    result = script.kpip(
        "install",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        "singlemodule",
        allow_stderr_warning=True,
        expect_error=True,
    )
    assert "Editable requirements are not allowed as constraints" in result.stderr


def test_constraints_constrain_to_local(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.src.joinpath("singlemodule")
    script.scratch_path.joinpath("constraints.txt").write_text(
        f"{to_install.as_uri()}#egg=singlemodule",
    )
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        "singlemodule",
        allow_stderr_warning=True,
    )
    assert "Building wheel for singlemodule" in result.stdout


def test_constrained_to_url_install_same_url(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.src.joinpath("singlemodule")
    constraints = f"{to_install.as_uri()}#egg=singlemodule"
    script.scratch_path.joinpath("constraints.txt").write_text(constraints)
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        "-f",
        data.find_links,
        "-c",
        script.scratch_path / "constraints.txt",
        to_install,
        allow_stderr_warning=True,
    )
    assert "Building wheel for singlemodule" in result.stdout, str(result)


def test_double_install_spurious_hash_mismatch(
    script: KpipTestEnvironment,
    tmpdir: Path,
    data: TestData,
) -> None:
    """Make sure installing the same hashed sdist twice doesn't throw hash
    mismatch errors.

    Really, this is a test that we disable reads from the wheel cache in
    hash-checking mode. Locally, implicitly built wheels of sdists obviously
    have different hashes from the original archives. Comparing against those
    causes spurious mismatch errors.

    """
    with requirements_file(
        "simple==1.0 --hash=sha256:393043e672415891885c9a2a"
        "0929b1af95fb866d6ca016b42d2e6ce53619b653",
        tmpdir,
    ) as reqs_file:
        result = script.kpip_install_local(
            "--find-links",
            data.find_links,
            "-r",
            reqs_file.resolve(),
        )
        assert "Successfully installed simple-1.0" in str(result)

        script.kpip("uninstall", "-y", "simple")

        result = script.kpip_install_local(
            "--find-links",
            data.find_links,
            "-r",
            reqs_file.resolve(),
        )
        assert "Successfully installed simple-1.0" in str(result)


def test_install_with_extras_from_constraints(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.environ["_KPIP_TEST_ENV"] = ""
    to_install = data.packages.joinpath("LocalExtras")
    file = script.temporary_file(
        "constraints.txt",
        f"LocalExtras[bar] @ {to_install.as_uri()}",
    )
    result = script.kpip_install_local(
        "-c",
        file,
        "LocalExtras",
        allow_stderr_warning=True,
        expect_error=True,
    )
    assert "Constraints cannot have extras" in result.stderr


def test_install_with_extras_from_install(script: KpipTestEnvironment) -> None:
    create_basic_wheel_for_package(
        script,
        name="LocalExtras",
        version="0.0.1",
        extras={"bar": ["simple"], "baz": ["singlemodule"]},
    )
    script.scratch_path.joinpath("constraints.txt").write_text("LocalExtras")
    result = script.kpip_install_local(
        "--find-links",
        script.scratch_path,
        "-c",
        script.scratch_path / "constraints.txt",
        "LocalExtras[baz]",
    )
    result.did_create(script.site_packages / "singlemodule.py")


def test_install_with_extras_and_url_constraint(
    script: KpipTestEnvironment,
) -> None:
    """Regression test for https://github.com/pypa/pip/issues/12018.

    A URL constraint for the base package plus a requirement that asks for
    the same package with extras used to trigger an AssertionError in
    LinkCandidate (``'name[extra]' != 'name' for wheel``).
    """
    create_basic_wheel_for_package(
        script,
        name="LocalExtras",
        version="0.0.1",
        extras={"baz": ["singlemodule"]},
    )
    wheel_path = next(script.scratch_path.glob("LocalExtras-0.0.1-*.whl"))
    script.scratch_path.joinpath("constraints.txt").write_text(
        f"LocalExtras @ {wheel_path.as_uri()}",
    )
    result = script.kpip_install_local(
        "--find-links",
        script.scratch_path,
        "-c",
        script.scratch_path / "constraints.txt",
        "LocalExtras[baz]",
    )
    result.did_create(script.site_packages / "singlemodule.py")


def test_install_with_extras_joined(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.environ["_KPIP_TEST_ENV"] = ""
    to_install = data.packages.joinpath("LocalExtras")
    file = script.temporary_file(
        "constraints.txt",
        f"LocalExtras[bar] @ {to_install.as_uri()}",
    )
    result = script.kpip_install_local(
        "-c",
        file,
        "LocalExtras[baz]",
        allow_stderr_warning=True,
        expect_error=True,
    )
    assert "Constraints cannot have extras" in result.stderr


def test_install_with_extras_editable_joined(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.packages.joinpath("LocalExtras")
    file = script.temporary_file(
        "constraints.txt",
        f"-e LocalExtras[bar] @ {to_install.as_uri()}",
    )
    result = script.kpip_install_local(
        "-c",
        file,
        "LocalExtras[baz]",
        allow_stderr_warning=True,
        expect_error=True,
    )
    assert "Editable requirements are not allowed as constraints" in result.stderr


def test_install_distribution_full_union(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.packages.joinpath("LocalExtras")
    result = script.kpip_install_local(
        to_install,
        f"{to_install}[bar]",
        f"{to_install}[baz]",
    )
    assert "Building wheel for LocalExtras" in result.stdout
    result.did_create(script.site_packages / "simple")
    result.did_create(script.site_packages / "singlemodule.py")


def test_install_distribution_duplicate_extras(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.packages.joinpath("LocalExtras")
    package_name = f"{to_install}[bar]"
    result = script.kpip_install_local(package_name, package_name)
    unexpected = f"Double requirement given: {package_name}"
    assert unexpected not in result.stderr


def test_install_distribution_union_with_constraints(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.environ["_KPIP_TEST_ENV"] = ""
    to_install = data.packages.joinpath("LocalExtras")
    script.scratch_path.joinpath("constraints.txt").write_text(f"{to_install}[bar]")
    result = script.kpip_install_local(
        "-c",
        script.scratch_path / "constraints.txt",
        f"{to_install}[baz]",
        allow_stderr_warning=True,
        expect_error=True,
    )
    assert "Unnamed requirements are not allowed as constraints" in result.stderr


def test_install_distribution_union_with_versions(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install_001 = data.packages.joinpath("LocalExtras")
    to_install_002 = data.packages.joinpath("LocalExtras-0.0.2")
    result = script.kpip_install_local(
        f"{to_install_001}[bar]",
        f"{to_install_002}[baz]",
        expect_error=True,
    )
    assert "Cannot install localextras" in result.stderr
    assert "The user requested localextras 0.0.1" in result.stdout
    assert "The user requested localextras 0.0.2" in result.stdout


@pytest.mark.xfail
def test_install_distribution_union_conflicting_extras(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    to_install = data.packages.joinpath("LocalExtras-0.0.2")
    result = script.kpip_install_local(
        to_install,
        f"{to_install}[bar]",
        expect_error=True,
    )
    assert "installed" not in result.stdout
    assert "Conflict" in result.stderr


def test_install_unsupported_wheel_link_with_marker(
    script: KpipTestEnvironment,
) -> None:
    script.scratch_path.joinpath("with-marker.txt").write_text(
        textwrap.dedent("""\
            {url}; {req}
        """).format(
            url="https://github.com/a/b/c/asdf-1.5.2-cp27-none-xyz.whl",
            req='sys_platform == "xyz"',
        ),
    )
    result = script.kpip("install", "-r", script.scratch_path / "with-marker.txt")

    assert (
        "Ignoring asdf: markers 'sys_platform == \"xyz\"' don't match your environment"
    ) in result.stdout
    assert len(result.files_created) == 0


def test_install_unsupported_wheel_file(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    path = data.packages.joinpath("simple.dist-0.1-py1-none-invalid.whl")
    script.scratch_path.joinpath("wheel-file.txt").write_text(f"{path}\n")
    result = script.kpip(
        "install",
        "-r",
        script.scratch_path / "wheel-file.txt",
        expect_error=True,
        expect_stderr=True,
    )
    assert (
        "simple.dist-0.1-py1-none-invalid.whl is not a supported wheel on this platform"
        in result.stderr
    )
    assert len(result.files_created) == 0


def test_config_settings_local_to_package(
    script: KpipTestEnvironment,
    common_wheels: Path,
    arg_recording_sdist_maker: ArgRecordingSdistMaker,
) -> None:
    pyproject_toml = textwrap.dedent("""
        [build-system]
        requires = ["setuptools"]
        build-backend = "setuptools.build_meta"
        """)
    simple0_sdist = arg_recording_sdist_maker(
        "simple0",
        extra_files={"pyproject.toml": pyproject_toml},
        depends=["foo"],
    )
    foo_sdist = arg_recording_sdist_maker(
        "foo",
        extra_files={"pyproject.toml": pyproject_toml},
    )
    simple1_sdist = arg_recording_sdist_maker(
        "simple1",
        extra_files={"pyproject.toml": pyproject_toml},
        depends=["bar"],
    )
    bar_sdist = arg_recording_sdist_maker(
        "bar",
        extra_files={"pyproject.toml": pyproject_toml},
        depends=["simple3"],
    )
    simple3_sdist = arg_recording_sdist_maker(
        "simple3",
        extra_files={"pyproject.toml": pyproject_toml},
    )
    simple2_sdist = arg_recording_sdist_maker(
        "simple2",
        extra_files={"pyproject.toml": pyproject_toml},
    )

    reqs_file = script.scratch_path.joinpath("reqs.txt")
    reqs_file.write_text(
        textwrap.dedent("""
            simple0 --config-settings "--build-option=--verbose"
            foo --config-settings "--build-option=--quiet"
            simple1 --config-settings "--build-option=--verbose"
            simple2
            """),
    )

    script.kpip_install_local(
        "--no-build-isolation",
        "-r",
        reqs_file,
        find_links=[script.scratch_path, common_wheels],
    )

    simple0_args = simple0_sdist.args()
    assert "--verbose" in simple0_args
    foo_args = foo_sdist.args()
    assert "--quiet" in foo_args
    simple1_args = simple1_sdist.args()
    assert "--verbose" in simple1_args
    bar_args = bar_sdist.args()
    assert "--verbose" not in bar_args
    simple3_args = simple3_sdist.args()
    assert "--verbose" not in simple3_args
    simple2_args = simple2_sdist.args()
    assert "--verbose" not in simple2_args


class TestEditableDirectURL:
    def test_install_local_project(
        self,
        script: KpipTestEnvironment,
        data: TestData,
        common_wheels: Path,
    ) -> None:
        uri = (data.src / "simplewheel-2.0").as_uri()
        script.kpip(
            "install",
            "--no-index",
            "-e",
            f"simplewheel @ {uri}",
            "-f",
            common_wheels,
        )
        script.assert_installed(simplewheel="2.0")

    def test_install_local_project_with_extra(
        self,
        script: KpipTestEnvironment,
        data: TestData,
        common_wheels: Path,
    ) -> None:
        uri = (data.src / "requires_simple_extra").as_uri()
        script.kpip(
            "install",
            "--no-index",
            "-e",
            f"requires-simple-extra[extra] @ {uri}",
            "-f",
            common_wheels,
            "-f",
            data.packages,
        )
        script.assert_installed(requires_simple_extra="0.1")
        script.assert_installed(simple="1.0")

    def test_install_local_git_repo(
        self,
        script: KpipTestEnvironment,
        common_wheels: Path,
    ) -> None:
        repo_path = create_test_package(script.scratch_path, "simple")
        url = "git+" + repo_path.as_uri()
        script.kpip(
            "install",
            "--no-index",
            "-e",
            f"simple @ {url}",
            "-f",
            common_wheels,
        )
        script.assert_installed(simple="0.1")

    @pytest.mark.network
    def test_install_remote_git_repo_with_extra(
        self,
        script: KpipTestEnvironment,
        data: TestData,
        common_wheels: Path,
    ) -> None:
        req = "pip-test-package[extra] @ git+https://github.com/pypa/pip-test-package"
        script.kpip(
            "install",
            "--no-index",
            "-e",
            req,
            "-f",
            common_wheels,
            "-f",
            data.packages,
        )
        script.assert_installed(pip_test_package="0.1.1")
        script.assert_installed(simple="3.0")
