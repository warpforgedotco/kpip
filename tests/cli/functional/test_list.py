import json
import os
import re
from pathlib import Path

import pytest
from kpip_test_support import (
    KpipTestEnvironment,
    ScriptFactory,
    TestData,
    create_test_package,
    create_test_package_with_setup,
    make_wheel,
    wheel,
)


@pytest.fixture(scope="session")
def simple_script(
    tmpdir_factory: pytest.TempPathFactory,
    script_factory: ScriptFactory,
    shared_data: TestData,
) -> KpipTestEnvironment:
    tmpdir = tmpdir_factory.mktemp("kpip_test_package")
    script = script_factory(tmpdir.joinpath("workspace"))
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        shared_data.find_links,
        "--no-index",
        "simple==1.0",
        "simple2==3.0",
    )
    return script


def test_basic_list(simple_script: KpipTestEnvironment) -> None:
    """Test default behavior of list command without format specifier."""
    result = simple_script.kpip("list")
    assert "simple     1.0" in result.stdout, str(result)
    assert "simple2    3.0" in result.stdout, str(result)


def test_verbose_flag(simple_script: KpipTestEnvironment) -> None:
    """Test the list command with the '-v' option"""
    result = simple_script.kpip("list", "-v", "--format=columns")
    assert "Package" in result.stdout, str(result)
    assert "Version" in result.stdout, str(result)
    assert "Location" in result.stdout, str(result)
    assert "Installer" in result.stdout, str(result)
    assert "simple     1.0" in result.stdout, str(result)
    assert "simple2    3.0" in result.stdout, str(result)


def test_columns_flag(simple_script: KpipTestEnvironment) -> None:
    """Test the list command with the '--format=columns' option"""
    result = simple_script.kpip("list", "--format=columns")
    assert "Package" in result.stdout, str(result)
    assert "Version" in result.stdout, str(result)
    assert "simple (1.0)" not in result.stdout, str(result)
    assert "simple     1.0" in result.stdout, str(result)
    assert "simple2    3.0" in result.stdout, str(result)


def test_format_priority(simple_script: KpipTestEnvironment) -> None:
    """Test that latest format has priority over previous ones."""
    result = simple_script.kpip(
        "list",
        "--format=columns",
        "--format=freeze",
        expect_stderr=True,
    )
    assert "simple==1.0" in result.stdout, str(result)
    assert "simple2==3.0" in result.stdout, str(result)
    assert "simple     1.0" not in result.stdout, str(result)
    assert "simple2    3.0" not in result.stdout, str(result)

    result = simple_script.kpip("list", "--format=freeze", "--format=columns")
    assert "Package" in result.stdout, str(result)
    assert "Version" in result.stdout, str(result)
    assert "simple==1.0" not in result.stdout, str(result)
    assert "simple2==3.0" not in result.stdout, str(result)
    assert "simple     1.0" in result.stdout, str(result)
    assert "simple2    3.0" in result.stdout, str(result)


def test_local_flag(simple_script: KpipTestEnvironment) -> None:
    """Test the behavior of --local flag in the list command"""
    result = simple_script.kpip("list", "--local", "--format=json")
    assert {"name": "simple", "version": "1.0"} in json.loads(result.stdout)


def test_local_columns_flag(simple_script: KpipTestEnvironment) -> None:
    """Test the behavior of --local --format=columns flags in the list command"""
    result = simple_script.kpip("list", "--local", "--format=columns")
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "simple (1.0)" not in result.stdout
    assert re.search(r"^simple\s+1\.0$", result.stdout, re.MULTILINE), str(result)


def test_multiple_exclude_and_normalization(
    script: KpipTestEnvironment,
    tmpdir: Path,
) -> None:
    req_path = wheel.make_wheel(name="Normalizable_Name", version="1.0").save_to_dir(
        tmpdir,
    )
    script.kpip("install", "--no-index", req_path)
    result = script.kpip("list")
    print(result.stdout)
    assert "Normalizable_Name" in result.stdout
    assert "kpip" in result.stdout
    result = script.kpip("list", "--exclude", "normalizablE-namE", "--exclude", "kPiP")
    assert "Normalizable_Name" not in result.stdout
    assert "kpip" not in result.stdout


@pytest.mark.usefixtures("enable_user_site")
def test_user_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --user flag in the list command"""
    script.kpip_install_local("simplewheel==1.0")
    script.kpip_install_local("--user", "simple.dist==0.1")
    result = script.kpip("list", "--user", "--format=json")
    assert {"name": "simplewheel", "version": "1.0"} not in json.loads(result.stdout)
    assert {"name": "simple.dist", "version": "0.1"} in json.loads(result.stdout)


@pytest.mark.usefixtures("enable_user_site")
def test_user_columns_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --user --format=columns flags in the list command"""
    script.kpip_install_local("simplewheel==1.0")
    script.kpip_install_local("--user", "simple.dist==0.1")
    result = script.kpip("list", "--user", "--format=columns")
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "simple.dist (2.0)" not in result.stdout
    assert "simple.dist 0.1" in result.stdout, str(result)


@pytest.mark.network
def test_uptodate_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --uptodate flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
        "simple2==3.0",
    )
    script.kpip(
        "install",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git#egg=pip-test-package",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--uptodate",
        "--format=json",
    )
    json_output = json.loads(result.stdout)
    for item in json_output:
        if "editable_project_location" in item:
            item["editable_project_location"] = "<location>"
    assert {"name": "simple", "version": "1.0"} not in json_output
    assert {
        "name": "pip-test-package",
        "version": "0.1.1",
        "editable_project_location": "<location>",
    } in json_output
    assert {"name": "simple2", "version": "3.0"} in json_output


@pytest.mark.network
def test_uptodate_columns_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --uptodate --format=columns flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
        "simple2==3.0",
    )
    script.kpip(
        "install",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git#egg=pip-test-package",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--uptodate",
        "--format=columns",
    )
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "Editable project location" in result.stdout
    assert "pip-test-package (0.1.1," not in result.stdout
    assert "pip-test-package 0.1.1" in result.stdout, str(result)
    assert "simple2          3.0" in result.stdout, str(result)


@pytest.mark.network
def test_outdated_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --outdated flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
        "simple2==3.0",
        "simplewheel==1.0",
    )
    script.kpip(
        "install",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git@0.1#egg=pip-test-package",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--outdated",
        "--format=json",
    )
    json_output = json.loads(result.stdout)
    for item in json_output:
        if "editable_project_location" in item:
            item["editable_project_location"] = "<location>"
    assert {
        "name": "simple",
        "version": "1.0",
        "latest_version": "3.0",
        "latest_filetype": "sdist",
    } in json_output
    assert {
        "name": "simplewheel",
        "version": "1.0",
        "latest_version": "2.0",
        "latest_filetype": "wheel",
    } in json_output
    assert {
        "name": "pip-test-package",
        "version": "0.1",
        "latest_version": "0.1.1",
        "latest_filetype": "sdist",
        "editable_project_location": "<location>",
    } in json_output
    assert "simple2" not in {p["name"] for p in json_output}


@pytest.mark.network
def test_outdated_columns_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --outdated --format=columns flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
        "simple2==3.0",
        "simplewheel==1.0",
    )
    script.kpip(
        "install",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git@0.1#egg=pip-test-package",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--outdated",
        "--format=columns",
    )
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "Latest" in result.stdout
    assert "Type" in result.stdout
    assert "simple (1.0) - Latest: 3.0 [sdist]" not in result.stdout
    assert "simplewheel (1.0) - Latest: 2.0 [wheel]" not in result.stdout
    assert "simple           1.0     3.0    sdist" in result.stdout, str(result)
    assert "simplewheel      1.0     2.0    wheel" in result.stdout, str(result)
    assert "simple2" not in result.stdout, str(result)


@pytest.fixture(scope="session")
def kpip_test_package_script(
    tmpdir_factory: pytest.TempPathFactory,
    script_factory: ScriptFactory,
    shared_data: TestData,
) -> KpipTestEnvironment:
    tmpdir = tmpdir_factory.mktemp("kpip_test_package")
    script = script_factory(tmpdir.joinpath("workspace"))
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        shared_data.find_links,
        "--no-index",
        "simple==1.0",
    )
    script.kpip(
        "install",
        "--no-build-isolation",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git#egg=pip-test-package",
    )
    return script


@pytest.mark.network
def test_editables_flag(kpip_test_package_script: KpipTestEnvironment) -> None:
    """Test the behavior of --editables flag in the list command"""
    result = kpip_test_package_script.kpip("list", "--editable", "--format=json")
    result2 = kpip_test_package_script.kpip("list", "--editable")
    assert {"name": "simple", "version": "1.0"} not in json.loads(result.stdout)
    assert os.path.join("src", "pip-test-package") in result2.stdout


@pytest.mark.network
def test_exclude_editable_flag(kpip_test_package_script: KpipTestEnvironment) -> None:
    """Test the behavior of --editables flag in the list command"""
    result = kpip_test_package_script.kpip(
        "list",
        "--exclude-editable",
        "--format=json",
    )
    assert {"name": "simple", "version": "1.0"} in json.loads(result.stdout)
    assert "pip-test-package" not in {p["name"] for p in json.loads(result.stdout)}


@pytest.mark.network
def test_editables_columns_flag(kpip_test_package_script: KpipTestEnvironment) -> None:
    """Test the behavior of --editables flag in the list command"""
    result = kpip_test_package_script.kpip("list", "--editable", "--format=columns")
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "Editable project location" in result.stdout
    assert os.path.join("src", "pip-test-package") in result.stdout, str(result)


@pytest.mark.network
def test_uptodate_editables_flag(
    kpip_test_package_script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test the behavior of --editable --uptodate flag in the list command"""
    result = kpip_test_package_script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--editable",
        "--uptodate",
    )
    assert "simple" not in result.stdout
    assert os.path.join("src", "pip-test-package") in result.stdout, str(result)


@pytest.mark.network
def test_uptodate_editables_columns_flag(
    kpip_test_package_script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test the behavior of --editable --uptodate --format=columns flag in the
    list command
    """
    result = kpip_test_package_script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--editable",
        "--uptodate",
        "--format=columns",
    )
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "Editable project location" in result.stdout
    assert os.path.join("src", "pip-test-package") in result.stdout, str(result)


@pytest.mark.network
def test_outdated_editables_flag(script: KpipTestEnvironment, data: TestData) -> None:
    """Test the behavior of --editable --outdated flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
    )
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git@0.1#egg=pip-test-package",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--editable",
        "--outdated",
    )
    assert "simple" not in result.stdout
    assert os.path.join("src", "pip-test-package") in result.stdout


@pytest.mark.network
def test_outdated_editables_columns_flag(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test the behavior of --editable --outdated flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
    )
    result = script.kpip(
        "install",
        "--no-build-isolation",
        "-e",
        "git+https://github.com/pypa/pip-test-package.git@0.1#egg=pip-test-package",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--editable",
        "--outdated",
        "--format=columns",
    )
    assert "Package" in result.stdout
    assert "Version" in result.stdout
    assert "Editable project location" in result.stdout
    assert os.path.join("src", "pip-test-package") in result.stdout, str(result)


def test_outdated_not_required_flag(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test the behavior of --outdated --not-required flag in the list command"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==2.0",
        "require_simple==1.0",
    )
    result = script.kpip(
        "list",
        "-f",
        data.find_links,
        "--no-index",
        "--outdated",
        "--not-required",
        "--format=json",
    )
    assert json.loads(result.stdout) == []


def test_outdated_pre(script: KpipTestEnvironment, data: TestData) -> None:
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
    )

    script.scratch_path.joinpath("wheelhouse").mkdir()
    wheelhouse_path = script.scratch_path / "wheelhouse"
    wheelhouse_path.joinpath("simple-1.1-py2.py3-none-any.whl").write_text("")
    wheelhouse_path.joinpath("simple-2.0.dev0-py2.py3-none-any.whl").write_text("")
    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--format=json",
    )
    assert {"name": "simple", "version": "1.0"} in json.loads(result.stdout)
    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--format=json",
    )
    assert {
        "name": "simple",
        "version": "1.0",
        "latest_version": "1.1",
        "latest_filetype": "wheel",
    } in json.loads(result.stdout)
    result_pre = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--pre",
        "--format=json",
    )
    assert {
        "name": "simple",
        "version": "1.0",
        "latest_version": "2.0.dev0",
        "latest_filetype": "wheel",
    } in json.loads(result_pre.stdout)


def test_outdated_formats(script: KpipTestEnvironment, data: TestData) -> None:
    """Test of different outdated formats"""
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "simple==1.0",
    )

    script.scratch_path.joinpath("wheelhouse").mkdir()
    wheelhouse_path = script.scratch_path / "wheelhouse"
    wheelhouse_path.joinpath("simple-1.1-py2.py3-none-any.whl").write_text("")
    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--format=freeze",
    )
    assert "simple==1.0" in result.stdout

    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--format=columns",
    )
    assert "Package Version Latest Type" in result.stdout
    assert "simple  1.0     1.1    wheel" in result.stdout

    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--format=freeze",
        expect_error=True,
    )
    assert (
        "List format 'freeze' cannot be used with the --outdated option."
        in result.stderr
    )

    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--format=json",
    )
    assert json.loads(result.stdout) == [
        {
            "name": "simple",
            "version": "1.0",
            "latest_version": "1.1",
            "latest_filetype": "wheel",
        },
    ]


def test_not_required_flag(script: KpipTestEnvironment, data: TestData) -> None:
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "TopoRequires4",
    )
    result = script.kpip("list", "--not-required", expect_stderr=True)
    assert "TopoRequires4 " in result.stdout, str(result)
    assert "TopoRequires " not in result.stdout
    assert "TopoRequires2 " not in result.stdout
    assert "TopoRequires3 " not in result.stdout


def test_not_required_with_exclude_does_not_list_dependencies(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    script.kpip(
        "install",
        "--no-build-isolation",
        "-f",
        data.find_links,
        "--no-index",
        "TopoRequires4",
    )

    result = script.kpip(
        "list",
        "--not-required",
        "--exclude",
        "TopoRequires4",
        "--format=json",
    )
    names = {item["name"] for item in json.loads(result.stdout)}
    listed_dependencies = names & {"TopoRequires", "TopoRequires2", "TopoRequires3"}

    assert "TopoRequires4" not in names
    assert not listed_dependencies


def test_list_freeze(simple_script: KpipTestEnvironment) -> None:
    """Test freeze formatting of list command"""
    result = simple_script.kpip("list", "--format=freeze")
    assert "simple==1.0" in result.stdout, str(result)
    assert "simple2==3.0" in result.stdout, str(result)


def test_list_json(simple_script: KpipTestEnvironment) -> None:
    """Test json formatting of list command"""
    result = simple_script.kpip("list", "--format=json")
    data = json.loads(result.stdout)
    assert {"name": "simple", "version": "1.0"} in data
    assert {"name": "simple2", "version": "3.0"} in data


def test_list_path(tmpdir: Path, script: KpipTestEnvironment, data: TestData) -> None:
    """Test list with --path."""
    result = script.kpip("list", "--path", tmpdir, "--format=json")
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "2.0"} not in json_result

    script.kpip_install_local("--target", tmpdir, "simple==2.0")
    result = script.kpip("list", "--path", tmpdir, "--format=json")
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "2.0"} in json_result


@pytest.mark.usefixtures("enable_user_site")
def test_list_path_exclude_user(
    tmpdir: Path,
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test list with --path and make sure packages from --user are not picked
    up.
    """
    script.kpip_install_local("--user", "simple2")
    script.kpip_install_local("--target", tmpdir, "simple==1.0")

    result = script.kpip("list", "--user", "--format=json")
    json_result = json.loads(result.stdout)
    assert {"name": "simple2", "version": "3.0"} in json_result

    result = script.kpip("list", "--path", tmpdir, "--format=json")
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "1.0"} in json_result


def test_list_path_multiple(
    tmpdir: Path,
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test list with multiple --path arguments."""
    path1 = tmpdir / "path1"
    os.mkdir(path1)
    path2 = tmpdir / "path2"
    os.mkdir(path2)

    script.kpip_install_local("--target", path1, "simple==2.0")
    script.kpip_install_local("--target", path2, "simple2==3.0")

    result = script.kpip("list", "--path", path1, "--format=json")
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "2.0"} in json_result

    result = script.kpip("list", "--path", path1, "--path", path2, "--format=json")
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "2.0"} in json_result
    assert {"name": "simple2", "version": "3.0"} in json_result


def test_list_skip_work_dir_pkg(script: KpipTestEnvironment) -> None:
    """Test that list should not include package in working directory"""
    pkg_path = create_test_package_with_setup(script, name="simple", version="1.0")
    script.run("python", "setup.py", "egg_info", expect_stderr=True, cwd=pkg_path)

    result = script.kpip("list", "--format=json", cwd=pkg_path)
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "1.0"} not in json_result


def test_list_include_work_dir_pkg(script: KpipTestEnvironment) -> None:
    """Test that list should include package in working directory
    if working directory is added in PYTHONPATH
    """
    pkg_path = create_test_package_with_setup(script, name="simple", version="1.0")
    script.run("python", "setup.py", "egg_info", expect_stderr=True, cwd=pkg_path)

    script.environ.update({"PYTHONPATH": pkg_path})

    result = script.kpip("list", "--format=json", cwd=pkg_path)
    json_result = json.loads(result.stdout)
    assert {"name": "simple", "version": "1.0"} in json_result


def test_list_pep610_editable(script: KpipTestEnvironment) -> None:
    """Test that a package installed with a direct_url.json with editable=true
    is correctly listed as editable.
    """
    pkg_path = create_test_package(script.scratch_path, name="testpkg")
    result = script.kpip("install", "--no-build-isolation", pkg_path)
    direct_url_path = result.get_created_direct_url_path("testpkg")
    assert direct_url_path
    with open(direct_url_path) as f:
        direct_url_dict = json.load(f)
    assert "dir_info" in direct_url_dict
    direct_url_dict["dir_info"]["editable"] = True
    with open(direct_url_path, "w") as f:
        json.dump(direct_url_dict, f)
    result = script.kpip("list", "--format=json")
    for item in json.loads(result.stdout):
        if item["name"] == "testpkg":
            assert item["editable_project_location"]
            break
    else:
        pytest.fail("package 'testpkg' not found in kpip list result")


def test_list_wheel_build(script: KpipTestEnvironment) -> None:
    package = make_wheel(
        name="package",
        version="3.0",
        wheel_metadata_updates={"Build": "123"},
    ).save_to_dir(script.scratch_path)
    script.kpip("install", package, "--no-index")

    result = script.kpip("list")
    assert "Build" in result.stdout, str(result)
    assert "123" in result.stdout, str(result)


def test_outdated_only_final_for_specific_package(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test that --only-final filters prereleases for specific package."""
    script.kpip_install_local("simple==1.0")

    wheelhouse_path = script.scratch_path / "wheelhouse"
    wheelhouse_path.mkdir()
    make_wheel("simple", "1.1").save_to_dir(wheelhouse_path)
    make_wheel("simple", "2.0a1").save_to_dir(wheelhouse_path)

    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--format=json",
    )
    outdated = json.loads(result.stdout)
    assert len(outdated) == 1
    assert outdated[0]["name"] == "simple"
    assert outdated[0]["latest_version"] == "1.1"

    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--only-final=simple",
        "--format=json",
    )
    outdated = json.loads(result.stdout)
    assert len(outdated) == 1
    assert outdated[0]["name"] == "simple"
    assert outdated[0]["latest_version"] == "1.1"


def test_outdated_all_releases_for_specific_package(
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test that --all-releases allows prereleases for specific package."""
    script.kpip_install_local("simple==1.0")

    wheelhouse_path = script.scratch_path / "wheelhouse"
    wheelhouse_path.mkdir()
    make_wheel("simple", "1.1").save_to_dir(wheelhouse_path)
    make_wheel("simple", "2.0a1").save_to_dir(wheelhouse_path)

    result = script.kpip(
        "list",
        "--no-index",
        "--find-links",
        wheelhouse_path,
        "--outdated",
        "--all-releases=simple",
        "--format=json",
    )
    outdated = json.loads(result.stdout)
    assert len(outdated) == 1
    assert outdated[0]["name"] == "simple"
    assert outdated[0]["latest_version"] == "2.0a1"
