import collections
import hashlib
from collections.abc import Callable
from itertools import repeat

import pytest
from kpip_test_support import (
    KpipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from kpip_test_support.server import MockServer, file_response, package_page

FindLinks = collections.namedtuple(
    "FindLinks",
    "index_html sdist_hash wheel_hash",
)


def create_find_links(script: KpipTestEnvironment) -> FindLinks:
    sdist_path = create_basic_sdist_for_package(script, "base", "0.1.0")
    wheel_path = create_basic_wheel_for_package(script, "base", "0.1.0")

    sdist_hash = hashlib.sha256(sdist_path.read_bytes()).hexdigest()
    wheel_hash = hashlib.sha256(wheel_path.read_bytes()).hexdigest()

    index_html = script.scratch_path / "index.html"
    index_html.write_text(
        f"""
        <!DOCTYPE html>
        <a href="{sdist_path.as_uri()}#sha256={sdist_hash}">{sdist_path.stem}</a>
        <a href="{wheel_path.as_uri()}#sha256={wheel_hash}">{wheel_path.stem}</a>
        """.strip(),
    )

    return FindLinks(index_html, sdist_hash, wheel_hash)


@pytest.mark.parametrize(
    "requirements_template, message",
    [
        (
            """
            base==0.1.0 --hash=sha256:{sdist_hash} --hash=sha256:{wheel_hash}
            base==0.1.0 --hash=sha256:{sdist_hash} --hash=sha256:{wheel_hash}
            """,
            "Using 2 sha256 hashes for requirement {name!r}",
        ),
        (
            """
            base==0.1.0 --hash=sha256:{sdist_hash} --hash=sha256:{wheel_hash}
            base==0.1.0 --hash=sha256:{sdist_hash}
            """,
            "Using 1 sha256 hashes for requirement {name!r}",
        ),
    ],
    ids=["identical", "intersect"],
)
def test_new_resolver_hash_intersect(
    script: KpipTestEnvironment,
    requirements_template: str,
    message: str,
) -> None:
    find_links = create_find_links(script)

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(
        requirements_template.format(
            sdist_hash=find_links.sdist_hash,
            wheel_hash=find_links.wheel_hash,
        ),
    )

    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-cache-dir",
        "--no-deps",
        "--no-index",
        "--find-links",
        find_links.index_html,
        "-vv",
        "--requirement",
        requirements_txt,
    )

    assert message.format(name="base") in result.stdout, str(result)


def test_new_resolver_hash_intersect_from_constraint(
    script: KpipTestEnvironment,
) -> None:
    find_links = create_find_links(script)
    sdist_hash = find_links.sdist_hash

    constraints_txt = script.scratch_path / "constraints.txt"
    constraints_txt.write_text(f"base==0.1.0 --hash=sha256:{sdist_hash}")
    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(
        f"""
        base==0.1.0 --hash=sha256:{sdist_hash} --hash=sha256:{find_links.wheel_hash}
        """,
    )

    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-cache-dir",
        "--no-deps",
        "--no-index",
        "--find-links",
        find_links.index_html,
        "-vv",
        "--constraint",
        constraints_txt,
        "--requirement",
        requirements_txt,
    )

    message = "Using 1 sha256 hashes for requirement {name!r}".format(name="base")
    assert message in result.stdout, str(result)


@pytest.mark.parametrize(
    "requirements_template, constraints_template",
    [
        (
            """
            base==0.1.0 --hash=sha256:{sdist_hash}
            base==0.1.0 --hash=sha256:{wheel_hash}
            """,
            "",
        ),
        (
            "base==0.1.0 --hash=sha256:{sdist_hash}",
            "base==0.1.0 --hash=sha256:{wheel_hash}",
        ),
    ],
    ids=["both-requirements", "one-each"],
)
def test_new_resolver_hash_intersect_empty(
    script: KpipTestEnvironment,
    requirements_template: str,
    constraints_template: str,
) -> None:
    find_links = create_find_links(script)

    constraints_txt = script.scratch_path / "constraints.txt"
    constraints_txt.write_text(
        constraints_template.format(
            sdist_hash=find_links.sdist_hash,
            wheel_hash=find_links.wheel_hash,
        ),
    )

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(
        requirements_template.format(
            sdist_hash=find_links.sdist_hash,
            wheel_hash=find_links.wheel_hash,
        ),
    )

    result = script.kpip(
        "install",
        "--no-build-isolation",
        "--no-cache-dir",
        "--no-deps",
        "--no-index",
        "--find-links",
        find_links.index_html,
        "--constraint",
        constraints_txt,
        "--requirement",
        requirements_txt,
        expect_error=True,
    )

    assert (
        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."
    ) in result.stderr, str(result)


def test_new_resolver_hash_intersect_empty_from_constraint(
    script: KpipTestEnvironment,
) -> None:
    find_links = create_find_links(script)

    constraints_txt = script.scratch_path / "constraints.txt"
    constraints_txt.write_text(
        f"""
        base==0.1.0 --hash=sha256:{find_links.sdist_hash}
        base==0.1.0 --hash=sha256:{find_links.wheel_hash}
        """,
    )

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-deps",
        "--no-index",
        "--find-links",
        find_links.index_html,
        "--constraint",
        constraints_txt,
        "base==0.1.0",
        expect_error=True,
    )

    message = (
        "Hashes are required in --require-hashes mode, but they are missing "
        "from some requirements."
    )
    assert message in result.stderr, str(result)


@pytest.mark.parametrize("constrain_by_hash", [False, True])
def test_new_resolver_hash_requirement_and_url_constraint_can_succeed(
    script: KpipTestEnvironment,
    constrain_by_hash: bool,
) -> None:
    wheel_path = create_basic_wheel_for_package(script, "base", "0.1.0")

    wheel_hash = hashlib.sha256(wheel_path.read_bytes()).hexdigest()

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(
        f"""
        base==0.1.0 --hash=sha256:{wheel_hash}
        """,
    )

    constraints_txt = script.scratch_path / "constraints.txt"
    constraint_text = f"base @ {wheel_path.as_uri()}\n"
    if constrain_by_hash:
        constraint_text += f"base==0.1.0 --hash=sha256:{wheel_hash}\n"
    constraints_txt.write_text(constraint_text)

    script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--constraint",
        constraints_txt,
        "--requirement",
        requirements_txt,
    )

    script.assert_installed(base="0.1.0")


@pytest.mark.parametrize("constrain_by_hash", [False, True])
def test_new_resolver_hash_requirement_and_url_constraint_can_fail(
    script: KpipTestEnvironment,
    constrain_by_hash: bool,
) -> None:
    wheel_path = create_basic_wheel_for_package(script, "base", "0.1.0")
    other_path = create_basic_wheel_for_package(script, "other", "0.1.0")

    other_hash = hashlib.sha256(other_path.read_bytes()).hexdigest()

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(
        f"""
        base==0.1.0 --hash=sha256:{other_hash}
        """,
    )

    constraints_txt = script.scratch_path / "constraints.txt"
    constraint_text = f"base @ {wheel_path.as_uri()}\n"
    if constrain_by_hash:
        constraint_text += f"base==0.1.0 --hash=sha256:{other_hash}\n"
    constraints_txt.write_text(constraint_text)

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--constraint",
        constraints_txt,
        "--requirement",
        requirements_txt,
        expect_error=True,
    )

    assert (
        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."
    ) in result.stderr, str(result)

    script.assert_not_installed("base", "other")


def test_new_resolver_unpinned_requirement_with_pinned_hash_constraint(
    script: KpipTestEnvironment,
) -> None:
    """Regression test for https://github.com/pypa/pip/issues/9243.

    An unpinned requirement combined with a constraints file that supplies both an
    ``==`` pin and ``--hash`` for that distribution used to fail with ``HashUnpinned``:

    > In --require-hashes mode, all requirements must have their versions pinned with ==

    This was because "is_pinned" could not be true for the unpinned requirement, even
    though the constraint did have a pin that was being enforced.
    """
    find_links = create_find_links(script)

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text("base\n")

    constraints_txt = script.scratch_path / "constraints.txt"
    constraints_txt.write_text(f"base==0.1.0 --hash=sha256:{find_links.wheel_hash}\n")

    script.kpip(
        "install",
        "--no-cache-dir",
        "--no-deps",
        "--no-index",
        "--find-links",
        find_links.index_html,
        "--constraint",
        constraints_txt,
        "--requirement",
        requirements_txt,
    )

    script.assert_installed(base="0.1.0")


def test_new_resolver_hash_with_extras(script: KpipTestEnvironment) -> None:
    parent_with_extra_path = create_basic_wheel_for_package(
        script,
        "parent_with_extra",
        "0.1.0",
        depends=["child[extra]"],
    )
    parent_with_extra_hash = hashlib.sha256(
        parent_with_extra_path.read_bytes(),
    ).hexdigest()

    parent_without_extra_path = create_basic_wheel_for_package(
        script,
        "parent_without_extra",
        "0.1.0",
        depends=["child"],
    )
    parent_without_extra_hash = hashlib.sha256(
        parent_without_extra_path.read_bytes(),
    ).hexdigest()

    child_path = create_basic_wheel_for_package(
        script,
        "child",
        "0.1.0",
        extras={"extra": ["extra"]},
    )
    child_hash = hashlib.sha256(child_path.read_bytes()).hexdigest()

    create_basic_wheel_for_package(
        script,
        "child",
        "0.2.0",
        extras={"extra": ["extra"]},
    )

    extra_path = create_basic_wheel_for_package(script, "extra", "0.1.0")
    extra_hash = hashlib.sha256(extra_path.read_bytes()).hexdigest()

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(
        f"""
        child[extra]==0.1.0 --hash=sha256:{child_hash}
        parent_with_extra==0.1.0 --hash=sha256:{parent_with_extra_hash}
        parent_without_extra==0.1.0 --hash=sha256:{parent_without_extra_hash}
        extra==0.1.0 --hash=sha256:{extra_hash}
        """,
    )

    script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--requirement",
        requirements_txt,
    )

    script.assert_installed(
        parent_with_extra="0.1.0",
        parent_without_extra="0.1.0",
        child="0.1.0",
        extra="0.1.0",
    )


def test_new_resolver_hash_requires_dependency_hashes(
    script: KpipTestEnvironment,
) -> None:
    """A dependency nobody hashed stops the install before it is fetched."""
    parent_path = create_basic_wheel_for_package(
        script, "parent", "0.1.0", depends=["child"]
    )
    parent_hash = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    create_basic_wheel_for_package(script, "child", "0.1.0")

    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(f"parent==0.1.0 --hash=sha256:{parent_hash}\n")

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--requirement",
        requirements_txt,
        expect_error=True,
    )

    assert "Hashes are required in --require-hashes mode" in result.stderr
    assert "child==0.1.0" in result.stderr
    script.assert_not_installed("parent", "child")


def test_new_resolver_hash_checks_a_download_its_link_does_not_hash(
    script: KpipTestEnvironment,
    mock_server: MockServer,
) -> None:
    """A find-links page publishes no digest, so nothing filtered the link;
    the downloaded file must still carry a hash the user allowed."""
    wheel = create_basic_wheel_for_package(script, "demo", "1.0")
    page = package_page({wheel.name: f"/files/{wheel.name}"})
    archive = file_response(wheel)

    def response(environ: dict[str, object], start_response: Callable[..., object]):
        if environ["PATH_INFO"] == "/links/":
            return page(environ, start_response)
        return archive(environ, start_response)

    mock_server.set_responses(repeat(response))
    mock_server.start()
    requirements_txt = script.scratch_path / "requirements.txt"
    requirements_txt.write_text(f"demo==1.0 --hash=sha256:{'0' * 64}\n")
    try:
        result = script.kpip(
            "install",
            "--no-cache-dir",
            "--no-index",
            "--find-links",
            f"http://{mock_server.host}:{mock_server.port}/links/",
            "--requirement",
            requirements_txt,
            "--trusted-host",
            mock_server.host,
            expect_error=True,
        )
    finally:
        mock_server.stop()

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    script.assert_not_installed("demo")


def test_new_resolver_hash_from_a_constraint_is_checked(
    script: KpipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "demo", "1.0")
    constraints_txt = script.scratch_path / "constraints.txt"
    constraints_txt.write_text(f"demo==1.0 --hash=sha256:{'0' * 64}\n")

    result = script.kpip(
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        constraints_txt,
        "demo",
        expect_error=True,
    )

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    script.assert_not_installed("demo")
