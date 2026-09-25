from __future__ import annotations

import random
import re
import pytest
from kpip.core.packaging import (
    InvalidSpecifier,
    SpecifierSet,
    canonicalize_name,
    canonicalize_requirement,
    marker_applies,
    parse_requirement,
)
from kpip.core.versions import InvalidVersion, Version, _versions, release_key


def test_canonicalize_name() -> None:
    assert canonicalize_name("Demo_Pkg.Name") == "demo-pkg-name"


def test_parse_requirement_with_extras_specifier_and_marker() -> None:
    requirement = parse_requirement('Demo-Pkg[PDF,SSL]>=1.0; python_version >= "3.11"')

    assert requirement.name == "Demo-Pkg"
    assert requirement.canonical_name == "demo-pkg"
    assert requirement.extras == {"pdf", "ssl"}
    assert requirement.marker == 'python_version >= "3.11"'
    assert requirement.is_satisfied_by("1.2")


def test_unconstrained_requirement_preserves_prerelease_filtering() -> None:
    requirement = parse_requirement("demo-pkg")

    assert requirement.is_satisfied_by("1.0rc1")
    assert not requirement.is_satisfied_by("1.0rc1", allow_prereleases=False)


def test_standard_requirement_skips_url_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_url_parse(value: str) -> None:
        raise AssertionError(f"parsed colon-free requirement as a URL: {value}")

    # Patched on ``urllib.parse`` itself: the module imports it inside the
    # helpers that need a URL rather than at module scope, so there is no
    # name to reach through here -- and a requirement with no colon never
    # gets that far.
    monkeypatch.setattr("urllib.parse.urlparse", fail_url_parse)

    requirement = parse_requirement("demo-pkg>=1")

    assert requirement.name == "demo-pkg"


def test_parse_requirement_reuses_immutable_result() -> None:
    first = parse_requirement("demo-pkg>=1")
    second = parse_requirement("demo-pkg>=1")

    assert second is first
    with pytest.raises(AttributeError):
        first.name = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        first.specifier.specifiers = ()  # type: ignore[misc]


def test_canonicalize_requirement() -> None:
    assert (
        canonicalize_requirement('Demo_Pkg[SSL,PDF] >= 1.0; python_version >= "3.11"')
        == 'demo-pkg[pdf,ssl]>=1.0; python_version >= "3.11"'
    )


def test_version_orders_prerelease_before_final() -> None:
    assert Version("1.0rc1") < Version("1.0")
    assert Version("1.0") < Version("1.0.post1")


def test_version_comparison_ignores_trailing_release_zeros() -> None:
    assert Version("1.3") == Version("1.3.0")
    assert SpecifierSet("==1.3").contains("1.3.0")


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("3.2.3-2", "3.2.3.post2"),
        ("1.0.post", "1.0.post0"),
        ("5.0.0.b1", "5.0.0b1"),
        ("6.0.0.rc1", "6.0.0rc1"),
        ("1!2.0.dev1+linux-x86_64", "1!2.0.dev1+linux.x86.64"),
    ],
)
def test_version_accepts_pep440_separator_forms(raw: str, normalized: str) -> None:
    assert str(Version(raw)) == normalized


def test_version_orders_epoch_and_dev_releases() -> None:
    assert Version("1!1.0") > Version("2.0")
    assert Version("1.0.dev1") < Version("1.0a1.dev1") < Version("1.0a1")


@pytest.mark.parametrize(
    "raw",
    ["1.2.3", "1!2.0rc1.post2.dev3+linux-x86_64", "0.0.0", "1.0.dev"],
)
def test_version_wire_roundtrip_is_interned(raw: str) -> None:
    original = Version(raw)
    state = original.to_wire()
    # Text and key, and nothing else: the release used to sit between them
    # and was read by nothing.
    assert state == (original.public, bytes(original))
    assert type(state[1]) is bytes

    restored = Version.from_wire(state)
    assert restored == original
    assert restored is Version(original.public)
    assert restored is Version.from_wire(state)
    assert hash(restored) == hash(original)
    assert str(restored) == str(original)


@pytest.mark.parametrize(
    "specifier, expected_lower, expected_upper",
    [
        (">=1,<2", (Version("1"), True), (Version("2"), False)),
        (">1,<=2", (Version("1"), False), (Version("2"), True)),
        ("==1.2", (Version("1.2"), True), (Version("1.2"), True)),
        ("~=1.2", (Version("1.2"), True), (Version("2"), False)),
        ("!=1.5", None, None),
        ("==1.*", None, None),
    ],
)
def test_specifier_set_bounds(
    specifier: str,
    expected_lower: tuple[Version, bool] | None,
    expected_upper: tuple[Version, bool] | None,
) -> None:
    assert SpecifierSet(specifier).bounds == (expected_lower, expected_upper)


def test_specifier_set_bounds_is_computed_once() -> None:
    specifier = SpecifierSet(">=1.0,<2")
    assert specifier.bounds == ((Version("1.0"), True), (Version("2"), False))
    assert specifier.bounds is specifier.bounds


def test_empty_specifier_set_preserves_prerelease_filtering() -> None:
    specifier = SpecifierSet()

    assert specifier.contains("1.0")
    assert not specifier.contains("1.0rc1")
    assert specifier.contains("1.0rc1", allow_prereleases=True)


@pytest.mark.parametrize(
    "specifier, version, expected",
    [
        ("==5.0.*", "5.0.1", True),
        ("==5.0.*", "5.1", False),
        ("!=5.0.*", "5.0.1", False),
        ("!=5.0.*", "5.1", True),
    ],
)
def test_wildcard_specifier_contains(
    specifier: str,
    version: str,
    expected: bool,
) -> None:
    assert SpecifierSet(specifier).contains(version) is expected


@pytest.mark.parametrize(
    "version, requires_python, expected",
    [
        ("3.6.5", "== 3.6.4", False),
        ("3.6.5", "== 3.6.5", True),
        ("3.6.5", "", True),
    ],
)
def test_requires_python_specifier_oracle(
    version: str,
    requires_python: str,
    expected: bool,
) -> None:
    assert SpecifierSet(requires_python).contains(version) is expected


def test_invalid_requires_python_specifier_oracle() -> None:
    with pytest.raises(ValueError, match="invalid version specifier"):
        SpecifierSet("invalid")


def test_requirement_attribute_oracle() -> None:
    requirement = parse_requirement("affinegap==1.10")

    assert requirement.name == "affinegap"
    assert requirement.url is None
    assert requirement.extras == frozenset()
    assert str(requirement.specifier) == "==1.10"
    assert requirement.marker is None


@pytest.mark.parametrize(
    "url, name, specifier",
    [
        (
            "https://example.com/packages/INITools-0.3.tar.gz",
            "INITools",
            "==0.3",
        ),
        (
            "https://example.com/packages/demo_pkg-1.2-py3-none-any.whl",
            "demo-pkg",
            "==1.2",
        ),
    ],
)
def test_parse_bare_direct_archive_reference_infers_name_and_version(
    url: str,
    name: str,
    specifier: str,
) -> None:
    requirement = parse_requirement(url)

    assert requirement.name == name
    assert str(requirement.specifier) == specifier
    assert requirement.url == url


def test_marker_applies_respects_parenthesized_extra_marker() -> None:
    requirement = parse_requirement("backports-zstd>=1.0.0; (extra == 'zstd')")

    assert marker_applies(requirement.marker, extras=()) is False
    assert marker_applies(requirement.marker, extras=("zstd",)) is True


class TestVersionIsItsOwnKey:
    """Version is bytes whose order is PEP 440 order: it compares only with
    other Versions, in C, and is immutable and interned.
    """

    def test_sentinel_comparisons_defer_to_the_sentinel(self) -> None:
        from kpip._vendor.nab_resolver.ranges import (
            NEGATIVE_INFINITY,
            POSITIVE_INFINITY,
        )

        version = Version("1.2.3")

        assert (version == NEGATIVE_INFINITY) is False
        assert (version != POSITIVE_INFINITY) is True
        assert (version < POSITIVE_INFINITY) is True
        assert (version > NEGATIVE_INFINITY) is True
        assert (version <= POSITIVE_INFINITY) is True
        assert (version >= NEGATIVE_INFINITY) is True
        assert (NEGATIVE_INFINITY < version) is True
        assert (POSITIVE_INFINITY > version) is True

    def test_never_compares_with_text(self) -> None:
        version = Version("1.2.3")

        assert (version == "1.2.3") is False
        assert (version != "1.2.3") is True
        with pytest.raises(TypeError):
            version < "2.0"  # noqa: B015
        with pytest.raises(TypeError):
            version >= "1.0"  # noqa: B015

    def test_unrelated_types_are_unequal(self) -> None:
        assert (Version("1.2.3") == object()) is False
        assert (Version("1.2.3") == None) is False  # noqa: E711

    def test_is_frozen(self) -> None:
        version = Version("1.2.3")

        with pytest.raises(AttributeError):
            version.public = "9"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            del version.release
        with pytest.raises(AttributeError):
            version.anything = 1  # type: ignore[attr-defined]

    def test_is_interned_by_text(self) -> None:
        assert Version("1.2.3") is Version("1.2.3")
        assert Version("1.2.3") == Version("1.2.3.0")
        assert Version("1.2.3") is not Version("1.2.3.0")

    def test_equal_versions_share_dict_slots(self) -> None:
        table = {Version("1.0"): "a"}
        assert table[Version("1.0.0")] == "a"
        assert table[Version("1")] == "a"
        assert len({Version("1.0"), Version("1.0.0"), Version("1.0+x")}) == 2

    def test_copies_and_pickles_are_the_same_value(self) -> None:
        import copy
        import pickle

        version = Version("1!2.0rc1.post2.dev3+linux-x86_64")

        assert copy.copy(version) is version
        assert copy.deepcopy(version) is version
        restored = pickle.loads(pickle.dumps(version))
        assert restored == version
        assert str(restored) == str(version)

    def test_zero_version_is_the_shared_sentinel(self) -> None:
        from kpip.core.versions import ZERO_VERSION

        assert ZERO_VERSION == Version("0")
        assert ZERO_VERSION < Version("0.0.1")
        assert not ZERO_VERSION.is_prerelease

    def test_derived_fields(self) -> None:
        version = Version("1!2.0rc1.post2.dev3+Linux_x86-64")

        assert version.epoch == 1
        assert version.release == (2, 0)
        assert version.local == "linux.x86.64"
        assert version.public == "1!2.0rc1.post2.dev3+linux.x86.64"
        assert version.base_version == "1!2.0"
        assert version.is_prerelease
        assert Version("1.0.post1").is_prerelease is False
        assert Version("1.0.post1.dev0").is_prerelease is True
        assert Version("1.0").local is None
        assert Version("1.0").base_version == "1.0"

    def test_bare_dev_and_pre_segments_mean_zero(self) -> None:
        assert str(Version("1.0.dev")) == "1.0.dev0"
        assert Version("1.0.dev") == Version("1.0.dev0")
        assert Version("1.0.dev").is_prerelease
        assert Version("1.0-dev") < Version("1.0a0")
        assert str(Version("1.0a")) == "1.0a0"
        assert str(Version("1.0.post")) == "1.0.post0"

    def test_parts_are_the_fields_the_key_is_written_from(self) -> None:
        assert Version("1.2.0").parts == (0, (1, 2), (3, 0, 0, 0, 1, 0), ())
        assert Version("1.2+a").parts == (0, (1, 2), (3, 0, 0, 0, 1, 0), ((0, "a"),))
        assert Version("1.2.0").release == (1, 2, 0)

    def test_a_version_from_its_wire_record_reads_its_parts_from_its_text(
        self,
    ) -> None:
        state = ("7!3.4.0rc2+x.1", Version("7!3.4.0rc2+x.1").to_wire()[1])
        _versions.clear()
        restored = Version.from_wire(state)
        assert "parts" not in restored.__dict__
        assert restored.parts == (7, (3, 4), (2, 2, 0, 0, 1, 0), ((0, "x"), (1, 1)))
        assert restored.release == (3, 4, 0)

    def test_key_order_is_pep440_order(self) -> None:
        from packaging.version import Version as Reference

        chooser = random.Random(440)
        segments = [0, 1, 2, 9, 10, 255, 256, 1000, 70000]

        def text() -> str:
            value = f"{chooser.choice([1, 300])}!" if chooser.random() < 0.1 else ""
            value += ".".join(
                str(chooser.choice(segments)) for _ in range(chooser.randint(1, 4))
            )
            if chooser.random() < 0.2:
                value += chooser.choice(["a", "b", "rc"]) + str(
                    chooser.choice([0, 300])
                )
            if chooser.random() < 0.15:
                value += f".post{chooser.choice([0, 256])}"
            if chooser.random() < 0.15:
                value += f".dev{chooser.choice([0, 256])}"
            if chooser.random() < 0.15:
                value += "+" + ".".join(
                    chooser.choice(["abc", "ab", "5", "05x", "0", "300"])
                    for _ in range(chooser.randint(1, 3))
                )
            return value

        versions = sorted({Version(text()) for _ in range(3000)})
        assert versions == sorted(versions, key=lambda version: version.parts)
        for lower, upper in zip(versions, versions[1:]):
            assert Reference(lower.public) < Reference(upper.public)
            assert lower.parts < upper.parts
        states = [version.to_wire() for version in versions]
        _versions.clear()
        for version, state in zip(versions, states):
            assert Version.from_wire(state).is_prerelease is version.is_prerelease

    @pytest.mark.parametrize(
        "text",
        [
            "1.0",
            "1!2.0",
            "1.0a1",
            "1.0b2",
            "1.0rc1",
            "1.0.dev3",
            "1.0.post1",
            "1.0.post1.dev0",
            "1.0rc1.post2",
            "1.0+abc.dev.rc",
            "1.0a1+local",
            "2!1.0.post4+ubuntu",
        ],
    )
    def test_a_rebuilt_version_knows_it_is_a_prerelease_without_parsing(
        self, text: str
    ) -> None:
        parsed = Version(text)
        _versions.clear()
        restored = Version.from_wire(parsed.to_wire())
        assert restored.is_prerelease is parsed.is_prerelease
        assert "parts" not in restored.__dict__

    def test_public_key_starts_every_local_version_of_it(self) -> None:
        public = Version("1.2rc1")
        for local in ("1.2rc1+a", "1.2rc1+0", "1.2rc1+a.b.9"):
            assert Version(local).startswith(public.public_key)
            assert Version(local).public_key == public.public_key
        assert not Version("1.2rc1.post1").startswith(public.public_key)

    def test_release_key_sorts_before_every_version_of_its_release(self) -> None:
        probe = release_key(0, (1, 2, 0))
        assert Version("1.1.99+z") < probe < Version("1.2.dev0")
        assert probe < Version("1.2") < Version("1.2.0.1")
        assert Version("1!0.1") > release_key(0, (9,))


class TestSplitMarker:
    """split_marker's fast paths (no semicolon; no quote before the first
    semicolon) must agree exactly with the quote-aware character walk.
    """

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("botocore==1.28.50", ("botocore==1.28.50", None)),
            ("pkg[extra]>=1.0", ("pkg[extra]>=1.0", None)),
            ("", ("", None)),
            ("pkg ; python_version >= '3.8'", ("pkg", "python_version >= '3.8'")),
            ('pkg ; python_version >= "3.8"', ("pkg", 'python_version >= "3.8"')),
            ("pkg;extra == 'feature'", ("pkg", "extra == 'feature'")),
            (
                "pkg @ https://x/y?q=';' ; python_version >= '3.8'",
                ("pkg @ https://x/y?q=';'", "python_version >= '3.8'"),
            ),
            ("pkg=='a;b'", ("pkg=='a;b'", None)),
        ],
    )
    def test_split(self, value: str, expected: tuple[str, str | None]) -> None:
        from kpip.core.packaging import split_marker

        assert split_marker(value) == expected


def _table_sizes() -> dict[str, int]:
    from kpip.core import packaging
    from kpip.core import versions

    return {
        "specifier_sets": len(packaging._specifier_sets),
        "versions": len(versions._versions),
    }


def _table_limits() -> dict[str, int]:
    from kpip.core import packaging
    from kpip.core import versions

    return {
        "specifier_sets": packaging._SPECIFIER_SET_CACHE_SIZE,
        "versions": versions._VERSIONS_LIMIT,
        "contains": packaging._CONTAINS_CACHE_SIZE,
    }


def _contains_cache_size(specifier: SpecifierSet) -> int:
    return max(
        len(getattr(specifier, "_contains", ())),
        len(getattr(specifier, "_contains_with_prereleases", ())),
    )


def test_parse_requirement_shares_specifier_sets_by_text() -> None:
    first = parse_requirement("leaf-0>=1.1.0,<2")
    second = parse_requirement("leaf-1>=1.1.0,<2")
    third = parse_requirement("leaf-2>=1.1.0")
    assert first.specifier is second.specifier
    assert first.specifier is not third.specifier
    assert first.specifier == SpecifierSet(">=1.1.0,<2")
    assert (
        parse_requirement("bare-a").specifier is parse_requirement("bare-b").specifier
    )
    assert not parse_requirement("bare-a").specifier.specifiers
    assert first.specifier.contains(Version("1.5"))
    assert not second.specifier.contains(Version("2.0"))
    assert str(first.specifier) == str(second.specifier)


def test_specifier_set_intern_table_is_bounded() -> None:
    limit = _table_limits()["specifier_sets"]
    for index in range(limit + 5):
        parse_requirement(f"pkg-{index}>={index}")
    assert _table_sizes()["specifier_sets"] <= limit
    assert parse_requirement("pkg-0>=0").specifier == SpecifierSet(">=0")


def test_shared_specifier_set_contains_cache_is_bounded() -> None:
    shared = parse_requirement("pkg-a>=1").specifier
    assert shared is parse_requirement("pkg-b>=1").specifier
    limit = _table_limits()["contains"]
    for index in range(limit + 50):
        assert shared.contains(Version(f"1.{index}"))
        assert not shared.contains(Version(f"0.{index}"))
    assert _contains_cache_size(shared) <= limit
    assert shared.contains(Version("1.0"))
    assert not shared.contains(Version("0.1"))


def test_specifier_clauses_share_one_version_per_text() -> None:
    pinned = parse_requirement("a==1.1.0").specifier.specifiers[0]
    floor = parse_requirement("b>=1.1.0").specifier.specifiers[0]
    assert pinned.parsed_version is floor.parsed_version
    assert pinned.parsed_version == Version("1.1.0")
    wildcard = parse_requirement("c==1.1.*").specifier
    assert wildcard.contains(Version("1.1.5"))
    assert not wildcard.contains(Version("1.2"))
    # A malformed operand is a malformed specifier: one exception type covers
    # every way a clause can fail to parse.
    with pytest.raises(InvalidSpecifier, match="not-a-version"):
        parse_requirement("d==not-a-version")
    limit = _table_limits()["versions"]
    for index in range(limit + 5):
        parse_requirement(f"pkg-{index}>={index}.0")
    assert _table_sizes()["versions"] <= limit


class TestFrozenInternedSpecifiers:
    """Specifier, SpecifierSet and Requirement are frozen value types;
    SpecifierSet and parse_requirement intern by text."""

    def test_specifier_set_is_interned_and_hashable(self) -> None:
        assert SpecifierSet(">=1.0,<2") is SpecifierSet(" >=1.0,<2 ")
        assert SpecifierSet() is SpecifierSet("")
        assert not SpecifierSet()
        assert SpecifierSet(">=1.0, <2") == SpecifierSet(">=1.0,<2")
        assert hash(SpecifierSet(">=1.0, <2")) == hash(SpecifierSet(">=1.0,<2"))
        assert (
            len({SpecifierSet(">=1"), SpecifierSet(">=1 "), SpecifierSet(">=2")}) == 2
        )
        assert str(SpecifierSet(">=1.0, <2")) == ">=1.0,<2"

    def test_specifier_set_is_frozen(self) -> None:
        specifier = SpecifierSet(">=1")
        with pytest.raises(AttributeError):
            specifier.specifiers = ()  # type: ignore[misc]
        with pytest.raises(AttributeError):
            specifier.specifiers[0].operator = "<"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            del specifier.text

    def test_requirement_equality_ignores_raw_and_name_spelling(self) -> None:
        assert parse_requirement("Demo_Pkg>=1") == parse_requirement("demo-pkg >= 1")
        assert hash(parse_requirement("Demo_Pkg>=1")) == hash(
            parse_requirement("demo-pkg >= 1")
        )
        assert parse_requirement("pkg>=1") != parse_requirement(
            "pkg>=1; python_version>'3'"
        )
        assert parse_requirement("pkg[a]>=1") != parse_requirement("pkg>=1")
        assert parse_requirement("pkg>=1").raw != parse_requirement("pkg >= 1").raw

    def test_requirement_is_frozen_and_eager(self) -> None:
        requirement = parse_requirement("Demo_Pkg[x]>=1")
        assert requirement.canonical_name == "demo-pkg"
        assert requirement.is_unnamed_direct is False
        assert parse_requirement("./local").is_unnamed_direct
        with pytest.raises(AttributeError):
            requirement.name = "other"  # type: ignore[misc]

    def test_wildcard_keeps_its_parsed_prefix(self) -> None:
        clause = SpecifierSet("==1.1.*").specifiers[0]
        assert clause.is_wildcard
        assert clause.parsed_version == Version("1.1")
        assert SpecifierSet("==1.1.*").exact_version is None
        with pytest.raises(InvalidSpecifier):
            SpecifierSet(">=1.1.*")
        with pytest.raises(InvalidSpecifier):
            SpecifierSet("==1.0.")

    def test_contains_caches_are_bounded_per_mode(self) -> None:
        from kpip.core import packaging

        shared = SpecifierSet(">=1")
        limit = packaging._CONTAINS_CACHE_SIZE
        for index in range(limit + 10):
            shared.contains(Version(f"1.{index}"))
            shared.contains(Version(f"1.{index}a1"), allow_prereleases=True)
        assert _contains_cache_size(shared) <= limit
        assert shared.contains(Version("1.0"))


class TestPep440ContainmentRules:
    """The per-operator rules plain ordering gets wrong, as the reference
    implementation applies them."""

    @pytest.mark.parametrize(
        "specifier, version, expected",
        [
            ("==1.0", "1.0+local", True),
            ("!=1.0", "1.0+local", False),
            ("==1.0+local", "1.0", False),
            ("==1.0+local", "1.0+local", True),
            ("==005.3.1.0.*", "5.3.1.0", True),
            ("==0.*", "0b2", True),
            ("==1.0.*", "1", True),
            ("==1.1.*", "1.1a1", True),
            ("==1.1.*", "1.10", False),
            ("!=1.*", "1.5.post1", False),
            ("==1!1.*", "1.0", False),
            ("<1.0", "1.0rc1", False),
            ("<1.0", "1.0.dev0", False),
            ("<1.0", "0.9rc1", True),
            ("<1.0rc2", "1.0rc1", True),
            ("<1.0.post1", "1.0rc1", True),
            ("<1.0.post1", "1.0.post1.dev0", False),
            (">0.3", "0.3.post1", False),
            (">0.3", "0.3.post1.dev0", False),
            (">0.3.post1", "0.3.post2", True),
            (">0.3a1", "0.3.post1", True),
            (">0.3", "0.3+local", False),
            (">0.3", "0.4", True),
            ("~=2.1", "3a1", False),
            ("~=2.1", "2.9.dev0", True),
            ("~=1!1.3", "1!1.3", True),
            ("~=1!1.3", "1.3", False),
            ("~=2.0001", "2.3.0.5", True),
            ("~=1.2.0.0", "1.2.1.0a0", False),
            ("~=1.4.5a4", "1.4.6", True),
            ("~=2.2.post3", "2.3", True),
        ],
    )
    def test_contains(self, specifier: str, version: str, expected: bool) -> None:
        assert (
            SpecifierSet(specifier).contains(version, allow_prereleases=True)
            is expected
        )

    def test_not_equal_prerelease_clause_does_not_opt_in(self) -> None:
        assert not SpecifierSet("!=1.0a1").explicitly_allows_prereleases
        assert not SpecifierSet("!=1.0a1,>=0.5").contains("2.0b1")
        assert SpecifierSet(">=1.0a1").explicitly_allows_prereleases
        # `===` keeps its operand as text but still opts the set in when that
        # text names a prerelease, as packaging's Specifier.prereleases does.
        assert SpecifierSet("===1.0a1").explicitly_allows_prereleases
        # `==1.0a1.*` is not a legal specifier: a prefix match cannot follow a
        # pre-release segment.
        with pytest.raises(InvalidSpecifier):
            SpecifierSet("==1.0a1.*")

    def test_derived_attributes(self) -> None:
        assert SpecifierSet("==1.0,<2").is_pinned
        assert SpecifierSet("==1.0,<2").exact_version is None
        assert SpecifierSet("===1.0").is_pinned
        assert SpecifierSet("===1.0").exact_version is None
        assert not SpecifierSet("==1.*").is_pinned
        assert SpecifierSet("==1.0").exact_version == Version("1.0")
        assert SpecifierSet("~=1!1.3").bounds == (
            (Version("1!1.3"), True),
            (Version("1!2"), False),
        )


def test_version_of_parses_text_and_passes_versions_through() -> None:
    from kpip.core.versions import version_of

    assert version_of("2.0.0") == Version("2.0")
    assert version_of(Version("2.0")) is Version("2.0")
    assert version_of("not a version") is None
    assert version_of("2.0.0") == Version("2.0.0")
    assert (Version("2.0.0") == "2.0.0") is False


class TestSpecifierClauseGrammar:
    """The string-based clause parser accepts and rejects exactly what the
    regex grammar it replaced did, clause for clause."""

    SPEC_RE = re.compile(r"(===|==|!=|~=|<=|>=|<|>)\s*([^,]+)")

    @classmethod
    def _reference(cls, text: str) -> list[tuple[str, str]] | None:
        spec = text.strip()
        if spec and (
            "[" in spec or "]" in spec or cls.SPEC_RE.sub("", spec).strip(" ,")
        ):
            return None
        clauses = [(op, ver.strip()) for op, ver in cls.SPEC_RE.findall(spec)]
        if spec and not clauses:
            return None
        for operator, version in clauses:
            if not version:
                return None
            if operator == "===":
                continue
            wildcard = version.endswith(".*")
            # PEP 440 restricts the operand by operator: `.*` prefix matching
            # and a local label are only legal on == and !=, the two cannot be
            # combined, and ~= needs a second release segment to have an upper
            # bound at all.
            if wildcard and operator not in ("==", "!="):
                return None
            try:
                parsed = Version(version[:-2] if wildcard else version)
            except InvalidVersion:
                return None
            has_local = bool(parsed.local)
            if operator not in ("==", "!=") and has_local:
                return None
            if wildcard and (parsed.public != parsed.base_version or has_local):
                return None
            if operator == "~=" and len(parsed.release) < 2:
                return None
        return clauses

    @staticmethod
    def _candidate(text: str) -> list[tuple[str, str]] | None:
        try:
            return [(s.operator, s.version) for s in SpecifierSet(text).specifiers]
        except ValueError:
            return None

    def test_matches_the_regex_grammar(self) -> None:
        rng = random.Random(20260821)
        operators = (
            "===",
            "==",
            "!=",
            "~=",
            "<=",
            ">=",
            "<",
            ">",
            "=",
            "!",
            "~",
            "",
            "x",
        )
        versions = ("1.0", "1.0.*", "2!1.0a1", "1.0 junk", "", " ", "1,0")
        seps = (",", " , ", ",,", " ", ";")
        texts = [
            "",
            " ",
            ",",
            ">=1.0",
            " >= 1.0 , <2 ",
            ">=1.0,,<2",
            "foo>=1",
            "= =1.0",
            "==>1.0",
            ">= ",
            "===",
            "==1.0[x]",
            ">=1.0<2",
            "~=1",
            "<>1",
            "=1.0",
        ]
        for _ in range(3000):
            clauses = [
                rng.choice(operators) + rng.choice(("", " ")) + rng.choice(versions)
                for _ in range(rng.randint(1, 3))
            ]
            texts.append(rng.choice(("", " ")) + rng.choice(seps).join(clauses))
        for text in texts:
            if "[" in text or "]" in text:
                continue
            assert self._candidate(text) == self._reference(text), repr(text)


def test_a_wheel_tag_triple_is_interned() -> None:
    """The same triple is the same object, however it is reached.

    A catalog holds the same handful of tags over and over, and a cold
    resolve of Airflow's graph rebuilt 159,112 of them before this.
    """
    from kpip.core.wheel import parsed_wheel_tags, wheel_tag

    first = wheel_tag("py3", "none", "any")

    assert wheel_tag("py3", "none", "any") is first
    # The filename path shares the same pool.
    assert parsed_wheel_tags("py3", "none", "any")[0] is first
    # A different triple is a different tag.
    assert wheel_tag("cp312", "cp312", "any") is not first
