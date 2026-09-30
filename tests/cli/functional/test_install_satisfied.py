"""``kpip install <name>`` for names already installed prints each
requirement once and installs nothing -- on the pre-startup recognizer and on
the normal path alike."""

from __future__ import annotations

from kpip_test_support import KpipTestEnvironment, TestData


def test_already_installed_names_are_reported_once(
    script: KpipTestEnvironment, data: TestData
) -> None:
    script.kpip_install_local("simplewheel==2.0")

    result = script.kpip("install", "--no-index", "simplewheel", "simplewheel>=1.0")
    assert result.stdout == (
        "Requirement already satisfied: simplewheel\n"
        "Requirement already satisfied: simplewheel>=1.0\n"
    ), result.stdout
    assert not result.files_created

    result = script.kpip("install", "--no-index", "-f", data.find_links, "simplewheel")
    assert result.stdout == (
        f"Looking in links: {data.find_links}\n"
        "Requirement already satisfied: simplewheel\n"
    ), result.stdout
    assert not result.files_created

    result = script.kpip(
        "install",
        "--no-index",
        "--no-binary",
        ":all:",
        "simplewheel",
        "simplewheel>=1.0",
    )
    assert result.stdout.count("Requirement already satisfied: simplewheel\n") == 1
    assert result.stdout.count("Requirement already satisfied: simplewheel>=1.0\n") == 1
    assert not result.files_created


def test_unmet_specifier_still_resolves(
    script: KpipTestEnvironment, data: TestData
) -> None:
    script.kpip_install_local("simplewheel==1.0")

    result = script.kpip(
        "install", "--no-index", "-f", data.find_links, "simplewheel>=2.0"
    )
    assert "Requirement already satisfied" not in result.stdout
    result.did_create(script.site_packages / "simplewheel-2.0.dist-info")


def test_an_installed_name_skipped_for_its_markers_is_not_satisfied(
    script: KpipTestEnvironment,
) -> None:
    """A requirement whose markers do not match is ignored, installed or not:
    it is logged as ignored and never reported as already satisfied."""
    script.kpip_install_local("simplewheel==2.0")
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text('simplewheel; sys_platform == "xyz"\n')

    result = script.kpip("install", "--no-index", "-r", requirements)

    assert "Requirement already satisfied" not in result.stdout, result.stdout
    assert (
        "Ignoring simplewheel: markers 'sys_platform == \"xyz\"' don't match "
        "your environment"
    ) in result.stderr
    assert not result.files_created

    result = script.kpip("install", "--quiet", "--no-index", "-r", requirements)

    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr


def test_a_target_directory_gets_what_the_environment_already_has(
    script: KpipTestEnvironment, data: TestData
) -> None:
    """As with pip, ``--target`` is a library of its own: a requirement the
    running environment has installed is installed into the target too."""
    script.kpip_install_local("simplewheel==2.0")
    target = script.scratch_path / "target"

    result = script.kpip(
        "install",
        "--no-index",
        "-f",
        data.find_links,
        "--target",
        target,
        "simplewheel",
    )

    assert "Requirement already satisfied" not in result.stdout, result.stdout
    assert (target / "simplewheel").is_dir()
