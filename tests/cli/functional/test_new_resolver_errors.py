import pathlib
import sys

from kpip_test_support import (
    KpipTestEnvironment,
    create_basic_wheel_for_package,
    create_test_package_with_setup,
)
from kpip_test_support.wheel import make_wheel


def test_new_resolver_conflict_requirements_file(
    tmpdir: pathlib.Path,
    script: KpipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "base", "1.0")
    create_basic_wheel_for_package(script, "base", "2.0")
    create_basic_wheel_for_package(
        script,
        "pkga",
        "1.0",
        depends=["base==1.0"],
    )
    create_basic_wheel_for_package(
        script,
        "pkgb",
        "1.0",
        depends=["base==2.0"],
    )

    req_file = tmpdir.joinpath("requirements.txt")
    req_file.write_text("pkga\npkgb")

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "-r",
        req_file,
        expect_error=True,
    )

    # As pip: the causes, which name base, are explained on stdout.
    assert "base" in result.stdout, str(result)


def test_new_resolver_conflict_constraints_file(
    tmpdir: pathlib.Path,
    script: KpipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "pkg", "1.0")

    constraints_file = tmpdir.joinpath("constraints.txt")
    constraints_file.write_text("pkg!=1.0")

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "-c",
        constraints_file,
        "pkg==1.0",
        expect_error=True,
    )

    assert "pkg" in result.stderr, str(result)

    assert "pkg!=1.0" in result.stdout, str(result)


def test_new_resolver_requires_python_error(script: KpipTestEnvironment) -> None:
    compatible_python = f">={sys.version_info.major}.{sys.version_info.minor}"
    incompatible_python = f"<{sys.version_info.major}.{sys.version_info.minor}"

    pkga = create_test_package_with_setup(
        script,
        name="pkga",
        version="1.0",
        python_requires=compatible_python,
    )
    pkgb = create_test_package_with_setup(
        script,
        name="pkgb",
        version="1.0",
        python_requires=incompatible_python,
    )

    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-index",
        pkga,
        pkgb,
        expect_error=True,
    )

    assert "pkgb" in result.stderr, str(result)
    assert "pkga" not in result.stderr, str(result)


def test_new_resolver_checks_requires_python_before_dependencies(
    script: KpipTestEnvironment,
) -> None:
    incompatible_python = f"<{sys.version_info.major}.{sys.version_info.minor}"

    pkg_dep = create_basic_wheel_for_package(
        script,
        name="pkg-dep",
        version="1",
    )
    create_basic_wheel_for_package(
        script,
        name="pkg-root",
        version="1",
        depends=[f"pkg-dep@{pathlib.Path(pkg_dep).as_uri()}"],
        requires_python=incompatible_python,
    )

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "pkg-root",
        expect_error=True,
    )

    assert "pkg-root" in result.stderr, str(result)
    assert "pkg_dep" not in result.stderr, str(result)
    assert "pkg_dep" not in result.stdout, str(result)


def test_new_resolver_no_versions_available_hint(script: KpipTestEnvironment) -> None:
    """Test hint that no package candidate is available at all,
    when ResolutionImpossible occurs.
    """
    wheel_house = script.scratch_path.joinpath("wheelhouse")
    wheel_house.mkdir()

    incompatible_dep_wheel = make_wheel(
        name="incompatible-dep",
        version="1.0.0",
        wheel_metadata_updates={"Tag": ["py3-none-fakeplat"]},
    )
    incompatible_dep_wheel.save_to(
        wheel_house.joinpath("incompatible_dep-1.0.0-py3-none-fakeplat.whl"),
    )

    requesting_pkg_v1 = make_wheel(
        name="requesting-pkg",
        version="1.0.0",
        metadata_updates={"Requires-Dist": ["incompatible-dep==1.0.0"]},
    )
    requesting_pkg_v1.save_to(
        wheel_house.joinpath("requesting_pkg-1.0.0-py2.py3-none-any.whl"),
    )

    requesting_pkg_v2 = make_wheel(
        name="requesting-pkg",
        version="2.0.0",
        metadata_updates={"Requires-Dist": ["incompatible-dep==1.0.0"]},
    )
    requesting_pkg_v2.save_to(
        wheel_house.joinpath("requesting_pkg-2.0.0-py2.py3-none-any.whl"),
    )

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        str(wheel_house),
        "requesting-pkg",
        expect_error=True,
    )

    assert "incompatible-dep" in result.stderr, str(result)
