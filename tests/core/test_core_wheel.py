from __future__ import annotations

import platform
import sys
import zipfile
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.core.utils import CURRENT_PYTHON_VERSION_DIGITS
from kpip.core.wheel import (
    TargetContext,
    WheelTag,
    parse_wheel_file,
    parse_wheel_filename,
    read_metadata_message,
    supported_wheel_tags,
    wheel_candidate,
    wheel_tag_rank,
)


def test_read_metadata_message_preserves_headers_folding_and_payload(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Name: demo\r\n"
            "Version: 1.0\r\n"
            "Requires-Dist: first>=1\r\n"
            "Requires-Dist: second>=2;\r\n python_version >= '3.11'\r\n"
            "Summary: metadata parser oracle\r\n"
            "\r\n"
            "Description body\r\n",
        )

    metadata = read_metadata_message(wheel)

    assert metadata.get("name") == "demo"
    assert metadata.get("Summary") == "metadata parser oracle"
    assert metadata.get_all("requires-dist") == [
        "first>=1",
        "second>=2;\npython_version >= '3.11'",
    ]
    assert metadata.get_payload() == "Description body\r\n"


def test_read_metadata_message_uses_fast_archive_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Name: demo\nVersion: 1.0\n",
        )

    def unexpected_zipfile(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("ordinary wheels should use WheelArchive")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_zipfile)

    assert read_metadata_message(wheel).get("Name") == "demo"


@pytest.mark.parametrize("version", ["1.0", "1.0rc1"])
def test_read_metadata_message_prefers_matching_dist_info(
    tmp_path: Path,
    version: str,
) -> None:
    wheel = tmp_path / f"demo-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "aaa-1.0.dist-info/METADATA",
            "Name: aaa\nVersion: 1.0\n",
        )
        archive.writestr(
            f"demo-{version}.dist-info/METADATA",
            f"Name: demo\nVersion: {version}\n",
        )

    assert read_metadata_message(wheel).get("Name") == "demo"


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("simple-1.1.1-py2-none-any.whl", ("simple", "1.1.1")),
        ("simple-1.1-py2.py3-abi1.abi2-any.whl", ("simple", "1.1")),
        ("simple-1.1-4-py2-none-any.whl", ("simple", "1.1")),
        ("simple-1-py2-none-any.whl", ("simple", "1")),
        ("complex_dist-0.1-py2.py3-none-any.whl", ("complex-dist", "0.1")),
    ],
)
def test_parse_wheel_filename_oracle(filename: str, expected: tuple[str, str]) -> None:
    assert parse_wheel_filename(filename) == expected


def test_parse_wheel_file_multi_tag_oracle() -> None:
    wheel = parse_wheel_file("simple-1.1-py2.py3-abi1.abi2-any.whl")

    assert wheel is not None
    assert wheel.name == "simple"
    assert str(wheel.version) == "1.1"
    assert wheel.build_tag is None
    assert set(wheel.tags) == {
        WheelTag("py2", "abi1", "any"),
        WheelTag("py2", "abi2", "any"),
        WheelTag("py3", "abi1", "any"),
        WheelTag("py3", "abi2", "any"),
    }


def test_parse_wheel_file_interns_versions_and_tags() -> None:
    first = parse_wheel_file("first-1.0-py3-none-any.whl")
    second = parse_wheel_file("second-1.0-py3-none-any.whl")

    assert first is not None
    assert second is not None
    assert first.version is second.version
    assert first.tags is second.tags


def test_parse_wheel_file_build_tag_oracle() -> None:
    wheel = parse_wheel_file("simple-1.1-4-py2-none-any.whl")

    assert wheel is not None
    assert wheel.build_tag == "4"
    assert wheel.tags == (WheelTag("py2", "none", "any"),)


def test_wheel_tag_rank_oracle() -> None:
    supported = (
        WheelTag("py2", "none", "TEST"),
        WheelTag("py2", "TEST", "any"),
        WheelTag("py2", "none", "any"),
    )

    def rank(filename: str) -> int:
        wheel = parse_wheel_file(filename)
        assert wheel is not None
        result = wheel_tag_rank(wheel.tags, supported)
        assert result is not None
        return result

    # Lower ranks first, in the order of the supported tags matched.
    test_rank = rank("simple-0.1-py2-none-TEST.whl")
    abi_rank = rank("simple-0.1-py2-TEST-any.whl")
    any_rank = rank("simple-0.1-py2-none-any.whl")
    assert test_rank < abi_rank < any_rank

    any_wheel = parse_wheel_file("simple-0.1-py2-none-any.whl")
    assert any_wheel is not None
    assert wheel_tag_rank(any_wheel.tags, ()) is None


def test_wheel_tag_rank_reuses_compatibility_result() -> None:
    candidate = (WheelTag("py3", "none", "any"),)
    supported = (WheelTag("py3", "none", "any"),)
    wheel_tag_rank.cache_clear()

    assert wheel_tag_rank(candidate, supported) == 0
    assert wheel_tag_rank(candidate, supported) == 0

    cache = wheel_tag_rank.cache_info()
    assert cache.misses == 1
    assert cache.hits == 1


def test_supported_wheel_tags_target_context_oracle() -> None:
    """pip's tags for a target named by options, in pip's order."""
    from pip._internal.utils.compatibility_tags import get_supported

    tags = supported_wheel_tags(
        TargetContext(
            platforms=("linux_x86_64",),
            implementation="cp",
            python_version="3.11",
            abis=("cp311",),
        ),
    )
    expected = get_supported(
        version="311", platforms=["linux_x86_64"], impl="cp", abis=["cp311"]
    )

    assert [(tag.interpreter, tag.abi, tag.platform) for tag in tags] == [
        (tag.interpreter, tag.abi, tag.platform)
        for tag in expected
        # Older abi3 and py3x versions match through interpreter_matches.
        if not (
            (tag.abi == "abi3" and tag.interpreter != "cp311")
            or (
                tag.interpreter.startswith("py3")
                and tag.interpreter not in ("py311", "py3")
            )
        )
    ]


@pytest.mark.parametrize(
    "runtime,wheel,expected",
    [
        ("macosx_13_0_arm64", "macosx_11_0_arm64", True),
        ("macosx_13_0_arm64", "macosx_13_0_universal2", True),
        ("macosx_13_0_arm64", "macosx_14_0_arm64", False),
        # A universal2 wheel carries an x86_64 slice as well as an arm64 one,
        # so an Intel Mac loads it: packaging.tags.mac_platforms((13, 0),
        # "x86_64") lists macosx_13_0_universal2.
        ("macosx_13_0_x86_64", "macosx_13_0_universal2", True),
        ("macosx_13_0_x86_64", "macosx_13_0_intel", True),
        ("macosx_13_0_x86_64", "macosx_13_0_fat64", True),
        ("macosx_13_0_x86_64", "macosx_13_0_arm64", False),
        ("macosx_13_0_arm64", "macosx_13_0_x86_64", False),
    ],
)
def test_wheel_tag_rank_macos_platform_oracle(
    runtime: str,
    wheel: str,
    expected: bool,
) -> None:
    supported = (WheelTag("cp311", "cp311", runtime),)
    candidate = (WheelTag("cp311", "cp311", wheel),)

    assert (wheel_tag_rank(candidate, supported) is not None) is expected


@pytest.mark.parametrize(
    "filename",
    [
        "simple-_invalid_-py2-none-any.whl",
        "Cython-cp27-none-linux_x86_64.whl",
        "invalid.whl",
        "simple-0.1_1-py2-none-any.whl",
        "six-1.16.0_build1-py3-none-any.whl",
    ],
)
def test_parse_wheel_filename_rejects_invalid_oracle(filename: str) -> None:
    assert parse_wheel_filename(filename) is None


def test_current_macos_accepts_newer_arm64_wheel() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("macOS arm64 wheel compatibility test")
    wheel = parse_wheel_file(
        "demo-1.0-cp"
        f"{CURRENT_PYTHON_VERSION_DIGITS}-cp{CURRENT_PYTHON_VERSION_DIGITS}"
        "-macosx_12_0_arm64.whl",
    )
    assert wheel is not None
    assert wheel_tag_rank(wheel.tags) is not None


def test_wheel_candidate_rejects_invalid_filename_oracle(tmp_path: Path) -> None:
    wheel = tmp_path / "invalid.whl"
    wheel.write_bytes(b"not a wheel")

    with pytest.raises(InstallationError, match="Invalid wheel filename"):
        wheel_candidate(wheel)


def test_wheel_candidate_reuses_metadata_across_extras(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "\n".join(
                (
                    "Name: demo",
                    "Version: 1.0",
                    "Requires-Dist: base",
                    "Requires-Dist: optional; extra == 'feature'",
                    "Provides-Extra: feature",
                    "",
                ),
            ),
        )
    base = wheel_candidate(wheel)
    monkeypatch.setattr(
        "kpip.core.wheel.read_metadata_message_internal",
        lambda *args_internal, **kwargs_internal: pytest.fail("metadata was reparsed"),
    )

    feature = wheel_candidate(wheel, {"feature"})

    assert [item.name for item in base.dependencies] == ["base"]
    assert [item.name for item in feature.dependencies] == ["base", "optional"]


def test_wheel_tag_refuses_mutation() -> None:
    """The cached hash and lowercase forms only hold if a tag cannot change.

    A tag lives in sets and dictionary keys -- ``supported_wheel_tags`` and
    ``wheel_tag_rank``'s cache both depend on it -- so a rewritten field would
    leave the hash pointing at the old value and silently corrupt lookups.
    """
    tag = WheelTag("py3", "none", "any")

    for attribute in ("interpreter", "abi", "platform", "_hash"):
        with pytest.raises(AttributeError, match="immutable"):
            setattr(tag, attribute, "changed")

    with pytest.raises(AttributeError, match="immutable"):
        del tag.interpreter

    assert tag == WheelTag("py3", "none", "any")
    assert hash(tag) == hash(WheelTag("py3", "none", "any"))


def test_wheel_tag_hash_matches_equality() -> None:
    """Equal tags must be interchangeable as dictionary keys."""
    first = WheelTag("cp312", "cp312", "macosx_11_0_arm64")
    second = WheelTag("cp312", "cp312", "macosx_11_0_arm64")
    other = WheelTag("cp312", "cp312", "manylinux_2_17_x86_64")

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second, other}) == 2
    assert {first: "value"}[second] == "value"


def test_target_context_refuses_mutation() -> None:
    """The cached hash only holds if a target cannot change.

    A target is looked up through ``supported_wheel_tags``'s unbounded
    cache, so a rewritten field would leave the hash pointing at the old
    value while ``__eq__`` reflected the new one, corrupting the cache.
    """
    target = TargetContext(
        platforms=("linux_x86_64",),
        implementation="cp",
        python_version="3.11",
        abis=("cp311",),
    )

    for attribute in ("platforms", "implementation", "python_version", "abis", "_hash"):
        with pytest.raises(AttributeError, match="immutable"):
            setattr(target, attribute, "changed")

    with pytest.raises(AttributeError, match="immutable"):
        del target.platforms

    assert target == TargetContext(
        platforms=("linux_x86_64",),
        implementation="cp",
        python_version="3.11",
        abis=("cp311",),
    )


def test_target_context_hash_matches_equality() -> None:
    """Equal targets must be interchangeable as dictionary/cache keys."""
    first = TargetContext(platforms=("linux_x86_64",), implementation="cp")
    second = TargetContext(platforms=("linux_x86_64",), implementation="cp")
    other = TargetContext(platforms=("macosx_11_0_arm64",), implementation="cp")

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second, other}) == 2
    assert {first: "value"}[second] == "value"


def test_target_context_cached_tag_lookup_is_stable() -> None:
    """Equal-but-distinct targets must not fragment the tag cache."""
    first = TargetContext(platforms=("linux_x86_64",), implementation="cp")
    second = TargetContext(platforms=("linux_x86_64",), implementation="cp")

    assert supported_wheel_tags(first) is supported_wheel_tags(second)


def test_parse_wheel_file_bare_name_matches_path(tmp_path: Path) -> None:
    name = "simple-1.1-4-py2.py3-abi1.abi2-any.whl"
    bare = parse_wheel_file(name)
    assert bare is not None
    assert parse_wheel_file(str(tmp_path / name)) == bare
    assert parse_wheel_file(f"nested/dir/{name}") == bare
    assert parse_wheel_file("nested/dir/") is None


def test_project_wheel_dependencies_marker_filtering() -> None:
    from kpip.core.packaging import parse_requirement
    from kpip.core.versions import Version
    from kpip.core.wheel import WheelResolutionMetadata, project_wheel_dependencies

    plain = WheelResolutionMetadata(
        name="pkg",
        version=Version("1.0"),
        dependencies=tuple(map(parse_requirement, ("a>=1", "b", "c[x]==2"))),
        provided_extras=frozenset(),
        requires_python=None,
    )
    assert project_wheel_dependencies(plain, None, frozenset()) is plain.dependencies
    assert (
        project_wheel_dependencies(plain, None, frozenset({"x"})) is plain.dependencies
    )

    marked = WheelResolutionMetadata(
        name="pkg",
        version=Version("1.0"),
        dependencies=tuple(
            map(
                parse_requirement,
                ("a", 'b; extra == "fast"', 'c; python_version < "2.0"', "d"),
            ),
        ),
        provided_extras=frozenset({"fast"}),
        requires_python=None,
    )
    assert [r.name for r in project_wheel_dependencies(marked, None, frozenset())] == [
        "a",
        "d",
    ]
    assert [
        r.name for r in project_wheel_dependencies(marked, None, frozenset({"fast"}))
    ] == ["a", "b", "d"]


def test_lazy_wheel_layout_is_read_once_and_shared_by_copies() -> None:
    from kpip.core.versions import Version
    from kpip.core.wheel import LazyWheelLayout, WheelCandidate

    reads: list[None] = []

    def compute() -> tuple[str, ...]:
        reads.append(None)
        return ("demo-1.0.dist-info", (), True)

    candidate = WheelCandidate(
        name="demo",
        version=Version("1.0"),
        path="/wheels/demo-1.0-py3-none-any.whl",
        dependencies=(),
        wheel_layout=LazyWheelLayout(compute),
    )
    copy = candidate.copy_with(source_kind="wheel")

    assert candidate.wheel_layout_if_loaded is None
    assert reads == []
    assert copy.wheel_layout == ("demo-1.0.dist-info", (), True)
    assert candidate.wheel_layout == ("demo-1.0.dist-info", (), True)
    assert reads == [None]
    assert candidate.wheel_layout_if_loaded == ("demo-1.0.dist-info", (), True)

    candidate.wheel_layout = None
    assert candidate.wheel_layout is None


@pytest.mark.parametrize(
    "filename, valid",
    [
        ("foo-1.0-py3-none-any.whl", True),
        ("foo-1.0-py2.py3-none-any.whl", True),
        ("foo_bar-1.0-py3-none-any.whl", True),
        ("foo.bar-1.0-py3-none-any.whl", True),
        ("foo-1.0-1-py3-none-any.whl", True),
        ("foo-1.0-1abc-py3-none-any.whl", True),
        ("foo-1.0.0-cp314-cp314-macosx_11_0_arm64.whl", True),
        # A build tag must start with a digit; treating a non-numeric one as
        # build 0 invented an ordering for a filename no builder produces.
        ("foo-1.0-abc-py3-none-any.whl", False),
        # PEP 427 escaping collapses runs of non-word characters to one
        # underscore, so a doubled underscore cannot come out of that rule.
        ("foo__bar-1.0-py3-none-any.whl", False),
        ("foo-bar-1.0-py3-none-any.whl", False),
        ("foo!x-1.0-py3-none-any.whl", False),
        ("foo-notaversion-py3-none-any.whl", False),
        ("foo-1.0-py3-none.whl", False),
        ("foo-1.0-py3-none-any.zip", False),
    ],
)
def test_wheel_filename_validation_matches_packaging(
    filename: str,
    valid: bool,
) -> None:
    from packaging.utils import InvalidWheelFilename, parse_wheel_filename

    from kpip.core.wheel import _parse_wheel_filename

    try:
        parse_wheel_filename(filename)
        reference = True
    except InvalidWheelFilename:
        reference = False

    assert reference is valid, "the expectation disagrees with packaging"
    assert (_parse_wheel_filename(filename) is not None) is valid


def test_build_tag_keeps_its_number_and_suffix() -> None:
    from kpip.core.wheel import legacy_build_tag

    assert legacy_build_tag(None) == ()
    assert legacy_build_tag("1") == (1, "")
    assert legacy_build_tag("12rc1") == (12, "rc1")


@pytest.mark.parametrize(
    "filename, compatible",
    [
        ("demo-1.0-py311-none-any.whl", True),
        ("demo-1.0-py38-none-any.whl", True),
        ("demo-1.0-py310-none-linux_x86_64.whl", True),
        ("demo-1.0-py313-none-any.whl", False),
        ("demo-1.0-py27-none-any.whl", False),
        ("demo-1.0-py311-abi3-any.whl", False),
    ],
)
def test_pure_wheel_for_an_older_minor_is_compatible(
    filename: str, compatible: bool
) -> None:
    """packaging's compatible_tags: py3X-none-* for every X up to the running one."""

    supported = supported_wheel_tags(
        TargetContext(platforms=("linux_x86_64",), python_version="3.12")
    )
    wheel = parse_wheel_file(filename)
    assert wheel is not None
    assert (wheel_tag_rank(wheel.tags, supported) is not None) is compatible


@pytest.mark.parametrize(
    "platform, python, worse, better",
    [
        (
            "manylinux_2_35_x86_64",
            "3.12",
            "demo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
            "demo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl",
        ),
        (
            "manylinux_2_35_x86_64",
            "3.12",
            "demo-1.0-cp312-cp312-manylinux2014_x86_64.whl",
            "demo-1.0-cp312-cp312-manylinux_2_34_x86_64.whl",
        ),
        (
            "macosx_14_0_arm64",
            "3.12",
            "demo-1.0-cp312-cp312-macosx_10_9_universal2.whl",
            "demo-1.0-cp312-cp312-macosx_11_0_arm64.whl",
        ),
        (
            "macosx_14_0_arm64",
            "3.12",
            "demo-1.0-cp312-cp312-macosx_11_0_arm64.whl",
            "demo-1.0-cp312-cp312-macosx_14_0_arm64.whl",
        ),
        (
            "manylinux_2_35_x86_64",
            "3.12",
            "demo-1.0-cp39-abi3-manylinux_2_28_x86_64.whl",
            "demo-1.0-cp311-abi3-manylinux_2_17_x86_64.whl",
        ),
        (
            "manylinux_2_35_x86_64",
            "3.12",
            "demo-1.0-py38-none-any.whl",
            "demo-1.0-py311-none-any.whl",
        ),
        (
            "manylinux_2_35_x86_64",
            "3.12",
            "demo-1.0-cp311-abi3-manylinux_2_28_x86_64.whl",
            "demo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        ),
    ],
)
def test_newest_and_most_specific_match_ranks_first(
    platform: str, python: str, worse: str, better: str
) -> None:
    """As packaging.tags orders them, and so pip prefers them."""
    supported = supported_wheel_tags(
        TargetContext(platforms=(platform,), python_version=python)
    )

    def rank(filename: str) -> int:
        wheel = parse_wheel_file(filename)
        assert wheel is not None
        result = wheel_tag_rank(wheel.tags, supported)
        assert result is not None
        return result

    assert rank(better) < rank(worse)
