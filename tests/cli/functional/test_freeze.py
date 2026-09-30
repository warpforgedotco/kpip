import json
import os
import re
import sys
import textwrap
from doctest import ELLIPSIS, OutputChecker
from pathlib import Path

import pytest
from kpip_test_support import (
    KpipTestEnvironment,
    TestData,
    create_test_package,
    create_test_package_with_setup,
    create_test_package_with_srcdir,
    git_commit,
    need_bzr,
    need_mercurial,
    need_svn,
    vcs_add,
    wheel,
)
from kpip_test_support.venv import VirtualEnvironment
from packaging.utils import canonicalize_name

distribute_re = re.compile("^distribute==[0-9.]+\n", re.MULTILINE)


def check_output_internal(result: str, expected: str) -> None:
    checker = OutputChecker()
    actual = str(result)

    if sys.platform == "win32":
        actual = actual.replace("initools", "INITools")

    actual = distribute_re.sub("", actual)

    def banner(msg: str) -> str:
        return f"\n========== {msg} ==========\n"

    assert checker.check_output(expected, actual, ELLIPSIS), (
        banner("EXPECTED") + expected + banner("ACTUAL") + actual + banner(6 * "=")
    )


def test_basic_freeze(script: KpipTestEnvironment) -> None:
    """Some tests of freeze, first we have to install some stuff.  Note that
    the test is a little crude at the end because Python 2.5+ adds egg
    info to the standard library, so stuff like wsgiref will show up in
    the freezing.  (Probably that should be accounted for in kpip, but
    currently it is not).

    """
    script.scratch_path.joinpath("initools-req.txt").write_text(
        textwrap.dedent("""\
        simple==2.0
        # and something else to test out:
        simple2<=3.0
        """),
    )
    script.kpip_install_local(
        "-r",
        script.scratch_path / "initools-req.txt",
    )
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""\
        ...simple==2.0
        simple2==3.0...
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)


def test_freeze_with_pip(script: KpipTestEnvironment) -> None:
    """Test that kpip shows itself only when --all is used"""
    result = script.kpip("freeze")
    assert "kpip==" not in result.stdout
    result = script.kpip("freeze", "--all")
    assert "kpip==" in result.stdout


def test_freeze_with_setuptools(script: KpipTestEnvironment) -> None:
    """Test that kpip shows setuptools only when --all is used on Python < 3.12,
    otherwise it should be shown in default freeze output.
    """
    result = script.kpip("freeze", "--all")
    assert "setuptools==" in result.stdout

    result = script.kpip("freeze")

    # Shown: only before 3.12, where venvs preinstalled it, was it hidden.
    assert "setuptools==" in result.stdout, result.stdout

    result_all = script.kpip("freeze", "--all")
    assert "setuptools==" in result_all.stdout


def test_exclude_and_normalization(script: KpipTestEnvironment, tmpdir: Path) -> None:
    req_path = wheel.make_wheel(name="Normalizable_Name", version="1.0").save_to_dir(
        tmpdir,
    )
    script.kpip("install", "--no-index", req_path)
    result = script.kpip("freeze")
    assert "Normalizable_Name" in result.stdout
    result = script.kpip("freeze", "--exclude", "normalizablE-namE")
    assert "Normalizable_Name" not in result.stdout


def test_freeze_multiple_exclude_with_all(script: KpipTestEnvironment) -> None:
    result = script.kpip("freeze", "--all")
    assert "kpip==" in result.stdout
    assert "setuptools==" in result.stdout
    result = script.kpip(
        "freeze",
        "--all",
        "--exclude",
        "kpip",
        "--exclude",
        "setuptools",
    )
    assert "kpip==" not in result.stdout
    assert "setuptools==" not in result.stdout


def test_freeze_with_invalid_names(script: KpipTestEnvironment) -> None:
    """Test that invalid names produce warnings and are passed over gracefully."""

    def fake_install(pkgname: str, dest: str) -> None:
        egg_info_path = os.path.join(
            dest,
            "{}-1.0-py{}.{}.egg-info".format(
                pkgname.replace("-", "_"),
                sys.version_info[0],
                sys.version_info[1],
            ),
        )
        with open(egg_info_path, "w") as egg_info_file:
            egg_info_file.write(
                textwrap.dedent(f"""\
                Metadata-Version: 1.0
                Name: {pkgname}
                Version: 1.0
                """),
            )

    valid_pkgnames = ("middle-dash", "middle_underscore", "middle.dot")
    invalid_pkgnames = (
        "-leadingdash",
        "_leadingunderscore",
        ".leadingdot",
        "trailingdash-",
        "trailingunderscore_",
        "trailingdot.",
    )
    for pkgname in valid_pkgnames + invalid_pkgnames:
        fake_install(pkgname, os.fspath(script.site_packages_path))

    result = script.kpip("freeze", expect_stderr=True)

    output_lines = {line.strip() for line in result.stdout.splitlines()}
    for name in valid_pkgnames:
        assert f"{name}==1.0" in output_lines

    canonical_invalid_names = {canonicalize_name(n) for n in invalid_pkgnames}
    for line in output_lines:
        output_name, _, _ = line.partition("=")
        assert canonicalize_name(output_name) not in canonical_invalid_names

    for name in canonical_invalid_names:
        assert f"Ignoring invalid distribution {name} (" in result.stderr


@pytest.mark.git
def test_freeze_editable_not_vcs(script: KpipTestEnvironment) -> None:
    """Test an editable install that is not version controlled."""
    pkg_path = create_test_package(script.scratch_path)
    os.rename(os.path.join(pkg_path, ".git"), os.path.join(pkg_path, ".bak"))
    script.kpip("install", "--no-build-isolation", "-e", pkg_path)
    result = script.kpip("freeze")

    expected = textwrap.dedent(f"""\
    ...# Editable install with no version control (version...pkg==0.1)
    -e {os.path.normcase(pkg_path)}
    ...""")
    check_output_internal(result.stdout, expected)


@pytest.mark.git
def test_freeze_editable_git_with_no_remote(
    script: KpipTestEnvironment,
    deprecated_python: bool,
) -> None:
    """Test an editable Git install with no remote url."""
    pkg_path = create_test_package(script.scratch_path)
    script.kpip("install", "--no-build-isolation", "-e", pkg_path)
    result = script.kpip("freeze")

    if not deprecated_python:
        assert result.stderr == ""

    expected = textwrap.dedent(f"""\
    ...# Editable Git install with no remote (version...pkg==0.1)
    -e {os.path.normcase(pkg_path)}
    ...""")
    check_output_internal(result.stdout, expected)


@need_svn
def test_freeze_svn(script: KpipTestEnvironment) -> None:
    """Test freezing a svn checkout"""
    checkout_path = create_test_package(script.scratch_path, vcs="svn")

    script.run("python", "setup.py", "develop", cwd=checkout_path, expect_stderr=True)
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""\
        ...-e svn+...#egg=version_pkg
        ...""")
    check_output_internal(result.stdout, expected)


@pytest.mark.git
@pytest.mark.xfail(
    condition=True,
    reason="xfail means editable is not in output",
    run=True,
    strict=True,
)
def test_freeze_exclude_editable(script: KpipTestEnvironment) -> None:
    """Test excluding editable from freezing list."""
    pkg_version = create_test_package(script.scratch_path)

    result = script.run(
        "git",
        "clone",
        os.fspath(pkg_version),
        "kpip-test-package",
        expect_stderr=True,
    )
    repo_dir = script.scratch_path / "kpip-test-package"
    result = script.run(
        "python",
        "setup.py",
        "develop",
        cwd=repo_dir,
        expect_stderr=True,
    )
    result = script.kpip("freeze", "--exclude-editable", expect_stderr=True)
    expected = textwrap.dedent("""
            ...-e git+...#egg=version_pkg
            ...
        """).strip()
    check_output_internal(result.stdout, expected)


@pytest.mark.git
def test_freeze_git_clone(script: KpipTestEnvironment) -> None:
    """Test freezing a Git clone."""
    pkg_version = create_test_package(script.scratch_path)

    result = script.run(
        "git",
        "clone",
        os.fspath(pkg_version),
        "kpip-test-package",
        expect_stderr=True,
    )
    repo_dir = script.scratch_path / "kpip-test-package"
    result = script.run(
        "python",
        "setup.py",
        "develop",
        cwd=repo_dir,
        expect_stderr=True,
    )
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""
            ...-e git+...#egg=version_pkg
            ...
        """).strip()
    check_output_internal(result.stdout, expected)

    script.run(
        "git",
        "checkout",
        "-b",
        "branch/name/with/slash",
        cwd=repo_dir,
        expect_stderr=True,
    )
    (repo_dir / "newfile").touch()
    script.run("git", "add", "newfile", cwd=repo_dir)
    git_commit(repo_dir, message="...")
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""
            ...-e ...@...#egg=version_pkg
            ...
        """).strip()
    check_output_internal(result.stdout, expected)


@pytest.mark.git
def test_freeze_git_clone_srcdir(script: KpipTestEnvironment) -> None:
    """Test freezing a Git clone where setup.py is in a subdirectory
    relative the repo root and the source code is in a subdirectory
    relative to setup.py.
    """
    pkg_version = create_test_package_with_srcdir(script.scratch_path)

    result = script.run(
        "git",
        "clone",
        os.fspath(pkg_version),
        "kpip-test-package",
        expect_stderr=True,
    )
    repo_dir = script.scratch_path / "kpip-test-package"
    result = script.run(
        "python",
        "setup.py",
        "develop",
        cwd=repo_dir / "subdir",
        expect_stderr=True,
    )
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""
            ...-e git+...#egg=version_pkg&subdirectory=subdir
            ...
        """).strip()
    check_output_internal(result.stdout, expected)


@need_mercurial
def test_freeze_mercurial_clone_srcdir(script: KpipTestEnvironment) -> None:
    """Test freezing a Mercurial clone where setup.py is in a subdirectory
    relative to the repo root and the source code is in a subdirectory
    relative to setup.py.
    """
    pkg_version = create_test_package_with_srcdir(script.scratch_path, vcs="hg")

    result = script.run("hg", "clone", os.fspath(pkg_version), "kpip-test-package")
    repo_dir = script.scratch_path / "kpip-test-package"
    result = script.run("python", "setup.py", "develop", cwd=repo_dir / "subdir")
    result = script.kpip("freeze")
    expected = textwrap.dedent("""
            ...-e hg+...#egg=version_pkg&subdirectory=subdir
            ...
        """).strip()
    check_output_internal(result.stdout, expected)


@pytest.mark.git
def test_freeze_git_remote(script: KpipTestEnvironment) -> None:
    """Test freezing a Git clone."""
    pkg_version = create_test_package(script.scratch_path)

    result = script.run(
        "git",
        "clone",
        os.fspath(pkg_version),
        "kpip-test-package",
        expect_stderr=True,
    )
    repo_dir = script.scratch_path / "kpip-test-package"
    result = script.run(
        "python",
        "setup.py",
        "develop",
        cwd=repo_dir,
        expect_stderr=True,
    )
    origin_remote = pkg_version
    result = script.kpip("freeze", expect_stderr=True)
    expected = (
        textwrap.dedent("""
            ...-e git+{remote}@...#egg=version_pkg
            ...
        """)
        .format(remote=origin_remote.as_uri())
        .strip()
    )
    check_output_internal(result.stdout, expected)
    script.run("git", "remote", "rename", "origin", "other", cwd=repo_dir)
    result = script.kpip("freeze", expect_stderr=True)
    expected = (
        textwrap.dedent("""
            ...-e git+{remote}@...#egg=version_pkg
            ...
        """)
        .format(remote=origin_remote.as_uri())
        .strip()
    )
    check_output_internal(result.stdout, expected)
    other_remote = f"{pkg_version}-other"
    script.run("git", "remote", "set-url", "other", other_remote, cwd=repo_dir)
    result = script.kpip("freeze", expect_stderr=True)
    expected = os.path.normcase(
        textwrap.dedent(f"""
            ...# Editable Git...(version...pkg...)...
            # '{other_remote}'
            -e {repo_dir}...
        """).strip(),
    )
    check_output_internal(os.path.normcase(result.stdout), expected)
    script.run("git", "remote", "add", "origin", os.fspath(origin_remote), cwd=repo_dir)
    result = script.kpip("freeze", expect_stderr=True)
    expected = (
        textwrap.dedent("""
            ...-e git+{remote}@...#egg=version_pkg
            ...
        """)
        .format(remote=origin_remote.as_uri())
        .strip()
    )
    check_output_internal(result.stdout, expected)


@need_mercurial
def test_freeze_mercurial_clone(script: KpipTestEnvironment) -> None:
    """Test freezing a Mercurial clone."""
    pkg_version = create_test_package(script.scratch_path, vcs="hg")

    result = script.run(
        "hg",
        "clone",
        os.fspath(pkg_version),
        "kpip-test-package",
        expect_stderr=True,
    )
    repo_dir = script.scratch_path / "kpip-test-package"
    result = script.run(
        "python",
        "setup.py",
        "develop",
        cwd=repo_dir,
        expect_stderr=True,
    )
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""
            ...-e hg+...#egg=version_pkg
            ...
        """).strip()
    check_output_internal(result.stdout, expected)


@need_bzr
def test_freeze_bazaar_clone(script: KpipTestEnvironment) -> None:
    """Test freezing a Bazaar clone."""
    try:
        checkout_path = create_test_package(script.scratch_path, vcs="bazaar")
    except OSError as e:
        pytest.fail(f"Invoking `bzr` failed: {e}")

    result = script.run("bzr", "checkout", os.fspath(checkout_path), "bzr-package")
    result = script.run(
        "python",
        "setup.py",
        "develop",
        cwd=script.scratch_path / "bzr-package",
        expect_stderr=True,
    )
    result = script.kpip("freeze", expect_stderr=True)
    expected = textwrap.dedent("""\
        ...-e bzr+file://...@1#egg=version_pkg
        ...""")
    check_output_internal(result.stdout, expected)


@need_mercurial
@pytest.mark.git
@pytest.mark.parametrize(
    "outer_vcs, inner_vcs",
    [("hg", "git"), ("git", "hg")],
)
def test_freeze_nested_vcs(
    script: KpipTestEnvironment,
    outer_vcs: str,
    inner_vcs: str,
) -> None:
    """Test VCS can be correctly freezed when resides inside another VCS repo."""
    pkg_path = create_test_package(script.scratch_path, vcs=inner_vcs)

    root_path = script.scratch_path.joinpath("test_freeze_nested_vcs")
    root_path.mkdir()
    root_path.joinpath(".hgignore").write_text("src")
    root_path.joinpath(".gitignore").write_text("src")
    vcs_add(script, root_path, outer_vcs)

    src_path = root_path.joinpath("src")
    src_path.mkdir()
    script.run(
        inner_vcs,
        "clone",
        os.fspath(pkg_path),
        os.fspath(src_path),
        expect_stderr=True,
    )
    script.kpip("install", "--no-build-isolation", "-e", src_path, expect_stderr=True)

    result = script.kpip("freeze", expect_stderr=True)
    check_output_internal(
        result.stdout,
        f"...-e {inner_vcs}+...#egg=version_pkg\n...",
    )


freeze_req_opts = textwrap.dedent("""\
    # Unchanged requirements below this line
    -r ignore.txt
    --requirement ignore.txt
    -f http://ignore
    -i http://ignore
    --pre
    --trusted-host url
    --process-dependency-links
    --extra-index-url http://ignore
    --find-links http://ignore
    --index-url http://ignore
    --use-feature resolvelib
""")


def test_freeze_with_requirement_option_file_url_egg_not_installed(
    script: KpipTestEnvironment,
    deprecated_python: bool,
) -> None:
    """Test "freeze -r requirements.txt" with a local file URL whose egg name
    is not installed.
    """
    url = "file:///my-package.tar.gz#egg=Does.Not-Exist"
    requirements_path = script.scratch_path.joinpath("requirements.txt")
    requirements_path.write_text(url + "\n")

    result = script.kpip(
        "freeze",
        "--requirement",
        "requirements.txt",
        expect_stderr=True,
    )
    expected_err = (
        f"WARNING: Requirement file [requirements.txt] contains {url}, "
        "but package 'Does.Not-Exist' is not installed\n"
    )
    if deprecated_python:
        assert expected_err in result.stderr
    else:
        assert expected_err == result.stderr


def test_freeze_with_requirement_option(script: KpipTestEnvironment) -> None:
    """Test that new requirements are created correctly with --requirement hints"""
    script.scratch_path.joinpath("hint1.txt").write_text(
        textwrap.dedent("""\
        INITools==0.1
        NoExist==4.2  # A comment that ensures end of line comments work.
        simple==3.0; python_version > '1.0'
        """)
        + freeze_req_opts,
    )
    script.scratch_path.joinpath("hint2.txt").write_text(
        textwrap.dedent("""\
        iniTools==0.1
        Noexist==4.2  # A comment that ensures end of line comments work.
        Simple==3.0; python_version > '1.0'
        """)
        + freeze_req_opts,
    )
    result = script.kpip_install_local("initools==0.2")
    result = script.kpip_install_local("simple")
    result = script.kpip(
        "freeze",
        "--requirement",
        "hint1.txt",
        expect_stderr=True,
    )
    expected = textwrap.dedent("""\
        INITools==0.2
        simple==3.0
    """)
    expected += freeze_req_opts
    expected += "## The following requirements were added by kpip freeze:..."
    check_output_internal(result.stdout, expected)
    assert (
        "Requirement file [hint1.txt] contains NoExist==4.2, but package "
        "'NoExist' is not installed"
    ) in result.stderr
    result = script.kpip(
        "freeze",
        "--requirement",
        "hint2.txt",
        expect_stderr=True,
    )
    check_output_internal(result.stdout, expected)
    assert (
        "Requirement file [hint2.txt] contains Noexist==4.2, but package "
        "'Noexist' is not installed"
    ) in result.stderr


def test_freeze_with_requirement_option_multiple(script: KpipTestEnvironment) -> None:
    """Test that new requirements are created correctly with multiple
    --requirement hints

    """
    script.scratch_path.joinpath("hint1.txt").write_text(
        textwrap.dedent("""\
        INITools==0.1
        NoExist==4.2
        simple==3.0; python_version > '1.0'
    """)
        + freeze_req_opts,
    )
    script.scratch_path.joinpath("hint2.txt").write_text(
        textwrap.dedent("""\
        NoExist2==2.0
        simple2==1.0
    """)
        + freeze_req_opts,
    )
    result = script.kpip_install_local("initools==0.2")
    result = script.kpip_install_local("simple")
    result = script.kpip_install_local("simple2==1.0")
    result = script.kpip_install_local("meta")
    result = script.kpip(
        "freeze",
        "--requirement",
        "hint1.txt",
        "--requirement",
        "hint2.txt",
        expect_stderr=True,
    )
    expected = textwrap.dedent("""\
        INITools==0.2
        simple==1.0
    """)
    expected += freeze_req_opts
    expected += textwrap.dedent("""\
        simple2==1.0
    """)
    expected += "## The following requirements were added by kpip freeze:"
    expected += "\n" + textwrap.dedent("""\
        ...meta==1.0...
    """)
    check_output_internal(result.stdout, expected)
    assert (
        "Requirement file [hint1.txt] contains NoExist==4.2, but package "
        "'NoExist' is not installed"
    ) in result.stderr
    assert (
        "Requirement file [hint2.txt] contains NoExist2==2.0, but package "
        "'NoExist2' is not installed"
    ) in result.stderr
    assert result.stdout.count("--index-url http://ignore") == 1


def test_freeze_with_requirement_option_package_repeated_one_file(
    script: KpipTestEnvironment,
) -> None:
    """Test freezing with single requirements file that contains a package
    multiple times
    """
    script.scratch_path.joinpath("hint1.txt").write_text(
        textwrap.dedent("""\
        simple2
        simple2
        NoExist
    """)
        + freeze_req_opts,
    )
    result = script.kpip_install_local("simple2==1.0")
    result = script.kpip_install_local("meta")
    result = script.kpip(
        "freeze",
        "--requirement",
        "hint1.txt",
        expect_stderr=True,
    )
    expected_out = textwrap.dedent("""\
        simple2==1.0
    """)
    expected_out += freeze_req_opts
    expected_out += "## The following requirements were added by kpip freeze:"
    expected_out += "\n" + textwrap.dedent("""\
        ...meta==1.0...
    """)
    check_output_internal(result.stdout, expected_out)
    err1 = (
        "Requirement file [hint1.txt] contains NoExist, "
        "but package 'NoExist' is not installed\n"
    )
    err2 = "Requirement simple2 included multiple times [hint1.txt]\n"
    assert err1 in result.stderr
    assert err2 in result.stderr
    assert result.stderr.count("is not installed") == 1


def test_freeze_with_requirement_option_package_repeated_multi_file(
    script: KpipTestEnvironment,
) -> None:
    """Test freezing with multiple requirements file that contain a package"""
    script.scratch_path.joinpath("hint1.txt").write_text(
        textwrap.dedent("""\
        simple
    """)
        + freeze_req_opts,
    )
    script.scratch_path.joinpath("hint2.txt").write_text(
        textwrap.dedent("""\
        simple
        NoExist
    """)
        + freeze_req_opts,
    )
    result = script.kpip_install_local("simple==1.0")
    result = script.kpip_install_local("meta")
    result = script.kpip(
        "freeze",
        "--requirement",
        "hint1.txt",
        "--requirement",
        "hint2.txt",
        expect_stderr=True,
    )
    expected_out = textwrap.dedent("""\
        simple==1.0
    """)
    expected_out += freeze_req_opts
    expected_out += "## The following requirements were added by kpip freeze:"
    expected_out += "\n" + textwrap.dedent("""\
        ...meta==1.0...
    """)
    check_output_internal(result.stdout, expected_out)

    err1 = (
        "Requirement file [hint2.txt] contains NoExist, but package "
        "'NoExist' is not installed\n"
    )
    err2 = "Requirement simple included multiple times [hint1.txt, hint2.txt]\n"
    assert err1 in result.stderr
    assert err2 in result.stderr
    assert result.stderr.count("is not installed") == 1


@pytest.mark.usefixtures("enable_user_site")
def test_freeze_user(
    script: KpipTestEnvironment,
    virtualenv: VirtualEnvironment,
    data: TestData,
) -> None:
    """Testing freeze with --user, first we have to install some stuff."""
    script.kpip_install_local("--find-links", data.find_links, "--user", "simple==2.0")
    script.kpip_install_local("--find-links", data.find_links, "simple2==3.0")
    result = script.kpip("freeze", "--user", expect_stderr=True)
    expected = textwrap.dedent("""\
        simple==2.0
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)
    assert "simple2" not in result.stdout


def test_freeze_path(tmpdir: Path, script: KpipTestEnvironment, data: TestData) -> None:
    """Test freeze with --path."""
    script.kpip_install_local("--target", tmpdir, "simple==2.0")
    result = script.kpip("freeze", "--path", tmpdir)
    expected = textwrap.dedent("""\
        simple==2.0
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)


@pytest.mark.usefixtures("enable_user_site")
def test_freeze_path_exclude_user(
    tmpdir: Path,
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test freeze with --path and make sure packages from --user are not picked
    up.
    """
    script.kpip_install_local("--find-links", data.find_links, "--user", "simple2")
    script.kpip_install_local("--target", tmpdir, "simple==1.0")
    result = script.kpip("freeze", "--user")
    expected = textwrap.dedent("""\
        simple2==3.0
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)
    result = script.kpip("freeze", "--path", tmpdir)
    expected = textwrap.dedent("""\
        simple==1.0
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)


def test_freeze_path_multiple(
    tmpdir: Path,
    script: KpipTestEnvironment,
    data: TestData,
) -> None:
    """Test freeze with multiple --path arguments."""
    path1 = tmpdir / "path1"
    os.mkdir(path1)
    path2 = tmpdir / "path2"
    os.mkdir(path2)
    script.kpip_install_local("--target", path1, "simple==2.0")
    script.kpip_install_local("--target", path2, "simple2==3.0")
    result = script.kpip("freeze", "--path", path1)
    expected = textwrap.dedent("""\
        simple==2.0
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)
    result = script.kpip("freeze", "--path", path1, "--path", path2)
    expected = textwrap.dedent("""\
        simple==2.0
        simple2==3.0
        <BLANKLINE>""")
    check_output_internal(result.stdout, expected)


def test_freeze_direct_url_archive(
    script: KpipTestEnvironment,
    shared_data: TestData,
) -> None:
    req = "simple @ " + shared_data.packages.joinpath("simple-2.0.tar.gz").as_uri()
    script.kpip("install", "--no-build-isolation", req)
    result = script.kpip("freeze")
    assert req in result.stdout


def test_freeze_skip_work_dir_pkg(script: KpipTestEnvironment) -> None:
    """Test that freeze should not include package
    present in working directory
    """
    pkg_path = create_test_package_with_setup(script, name="simple", version="1.0")
    script.run("python", "setup.py", "egg_info", expect_stderr=True, cwd=pkg_path)

    result = script.kpip("freeze", cwd=pkg_path)
    assert "simple" not in result.stdout


def test_freeze_include_work_dir_pkg(script: KpipTestEnvironment) -> None:
    """Test that freeze should include package in working directory
    if working directory is added in PYTHONPATH
    """
    pkg_path = create_test_package_with_setup(script, name="simple", version="1.0")
    script.run("python", "setup.py", "egg_info", expect_stderr=True, cwd=pkg_path)

    script.environ.update({"PYTHONPATH": pkg_path})

    result = script.kpip("freeze", cwd=pkg_path)
    assert "simple==1.0" in result.stdout


def test_freeze_pep610_editable(script: KpipTestEnvironment) -> None:
    """Test that a package installed with a direct_url.json with editable=true
    is correctly frozen as editable.
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
    result = script.kpip("freeze")
    assert "# Editable Git install with no remote (testpkg==0.1)" in result.stdout
