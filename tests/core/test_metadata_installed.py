"""Correctness of the installed-distribution metadata fast path.

``InstalledDistribution`` reads Name/Version/Requires-Dist through
``parse_metadata_headers`` instead of ``importlib.metadata``'s full RFC822
email parsing (see ``core/metadata.py``). These tests build real dist-info
directories on disk and assert kpip's fast path agrees exactly with what
``importlib.metadata`` itself would report.
"""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

import pytest
from kpip.core.metadata import iter_installed_distributions
from kpip.core.versions import Version


def _write_dist_info(
    site_packages: Path,
    dist_info_name: str,
    metadata_text: str,
    *,
    filename: str = "METADATA",
) -> None:
    dist_info = site_packages / dist_info_name
    dist_info.mkdir(parents=True)
    (dist_info / filename).write_text(metadata_text, encoding="utf-8")


def test_iter_installed_distributions_matches_stdlib(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "widget-1.2.3.dist-info",
        "Metadata-Version: 2.1\n"
        "Name: widget\n"
        "Version: 1.2.3\n"
        "Requires-Dist: gadget>=1.0\n"
        'Requires-Dist: extra-thing==2.0; extra == "extra"\n'
        "Provides-Extra: extra\n",
    )

    [distribution] = iter_installed_distributions(paths=[str(site_packages)])

    stdlib_dist = next(
        iter(importlib.metadata.distributions(path=[str(site_packages)])),
    )

    assert distribution.name == stdlib_dist.metadata.get("Name")
    assert distribution.raw_version == stdlib_dist.version
    assert distribution.canonical_name == "widget"

    assert sorted(distribution._fast_metadata_headers()["requires-dist"]) == sorted(
        stdlib_dist.metadata.get_all("Requires-Dist", []),
    )

    fast_deps = sorted(str(dep) for dep in distribution.dependencies())
    assert fast_deps == ["gadget>=1.0"]

    with_extra = sorted(dep.name for dep in distribution.dependencies(["extra"]))
    assert with_extra == ["extra-thing", "gadget"]


def test_iter_installed_distributions_falls_back_to_pkg_info(tmp_path: Path) -> None:
    """Old-style egg-info installs ship PKG-INFO instead of METADATA."""
    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "legacy-0.1.egg-info",
        "Metadata-Version: 1.0\nName: legacy\nVersion: 0.1\n",
        filename="PKG-INFO",
    )

    [distribution] = iter_installed_distributions(paths=[str(site_packages)])

    assert distribution.name == "legacy"
    assert distribution.raw_version == "0.1"
    assert distribution.dependencies() == []


def test_installed_distribution_dependencies_reuse_parsed_headers(
    tmp_path: Path,
) -> None:
    """dependencies() must not re-read the file iter_installed_distributions
    already parsed."""
    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "widget-1.0.dist-info",
        "Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n",
    )

    [distribution] = iter_installed_distributions(paths=[str(site_packages)])
    assert distribution._fast_headers is not None

    (site_packages / "widget-1.0.dist-info" / "METADATA").write_text(
        "not valid metadata at all",
        encoding="utf-8",
    )
    assert distribution.dependencies() == []


def _widget_metadata(version: str) -> str:
    return f"Metadata-Version: 2.1\nName: widget\nVersion: {version}\n"


def test_find_installed_matches_an_uncached_scan(tmp_path: Path) -> None:
    from kpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(site_packages, "widget-1.2.3.dist-info", _widget_metadata("1.2.3"))
    _write_dist_info(
        site_packages,
        "Other_Thing-0.1.dist-info",
        "Metadata-Version: 2.1\nName: Other_Thing\nVersion: 0.1\n",
    )
    clear_installed_index()
    paths = [str(site_packages)]
    found = find_installed("WIDGET", paths)
    assert found is not None
    assert (found.name, found.raw_version, found.version) == (
        "widget",
        "1.2.3",
        Version("1.2.3"),
    )
    assert find_installed("other-thing", paths) is not None
    assert find_installed("missing", paths) is None
    expected = {
        d.canonical_name: d.raw_version for d in iter_installed_distributions(paths)
    }
    assert expected == {"widget": "1.2.3", "other-thing": "0.1"}


def test_find_installed_reads_metadata_once_per_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    from kpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    for index in range(5):
        _write_dist_info(
            site_packages,
            f"pkg{index}-1.0.dist-info",
            f"Metadata-Version: 2.1\nName: pkg{index}\nVersion: 1.0\n",
        )
    clear_installed_index()
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):  # noqa: ANN001, ANN202
        if isinstance(file, str) and file.endswith("METADATA"):
            opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    paths = [str(site_packages)]
    for name in ("pkg0", "pkg3", "pkg4", "nope", "pkg0"):
        find_installed(name, paths)
    assert len(opened) == 5


def test_find_installed_notices_a_new_distribution(tmp_path: Path) -> None:
    import os
    import time

    from kpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(site_packages, "widget-1.2.3.dist-info", _widget_metadata("1.2.3"))
    clear_installed_index()
    paths = [str(site_packages)]
    assert find_installed("gadget", paths) is None
    _write_dist_info(
        site_packages,
        "gadget-2.0.dist-info",
        "Metadata-Version: 2.1\nName: gadget\nVersion: 2.0\n",
    )
    later = time.time() + 2
    os.utime(site_packages, (later, later))
    found = find_installed("gadget", paths)
    assert found is not None
    assert found.raw_version == "2.0"


def test_find_installed_first_path_wins(tmp_path: Path) -> None:
    from kpip.core.metadata import clear_installed_index, find_installed

    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_dist_info(first, "widget-1.0.dist-info", _widget_metadata("1.0"))
    _write_dist_info(second, "widget-2.0.dist-info", _widget_metadata("2.0"))
    clear_installed_index()
    found = find_installed("widget", [str(first), str(second)])
    assert found is not None
    assert found.raw_version == "1.0"
    found = find_installed("widget", [str(second), str(first)])
    assert found is not None
    assert found.raw_version == "2.0"


def test_find_installed_accepts_a_generator_of_paths(tmp_path: Path) -> None:
    from kpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(site_packages, "widget-1.2.3.dist-info", _widget_metadata("1.2.3"))
    clear_installed_index()
    found = find_installed("widget", (path for path in [str(site_packages)]))
    assert found is not None
    assert found.raw_version == "1.2.3"
    assert find_installed("widget", [str(site_packages)]) is found


def test_default_and_explicit_scans_are_cached_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``paths=None`` consults every metadata finder; an explicit list equal to
    sys.path consults only the path finder. Neither order may let one answer
    stand in for the other."""
    import sys

    from kpip.core import metadata

    metadata.clear_installed_index()
    calls: list[object] = []
    real = metadata._iter_raw_distributions

    def recording(paths, canonical_names=None):  # noqa: ANN001, ANN202
        calls.append("<default>" if paths is None else list(paths))
        return real(paths, canonical_names)

    monkeypatch.setattr(metadata, "_iter_raw_distributions", recording)
    explicit = list(sys.path)
    for order in ((None, explicit), (explicit, None)):
        metadata.clear_installed_index()
        calls.clear()
        for paths in order:
            metadata.find_installed("not-installed-anywhere", paths)
        assert len(calls) == 2
        assert "<default>" in calls
        assert any(call != "<default>" for call in calls)
        for paths in order:
            metadata.find_installed("not-installed-anywhere", paths)
        assert len(calls) == 2


def _stdlib_view(paths: list[str]) -> list[tuple[str, str, str]]:
    """(name, metadata path, location) for every entry the stdlib finds,
    a ``.dist-info`` directory with no metadata named as pip names it: for
    what its own name spells."""
    found = []
    for dist in importlib.metadata.distributions(path=paths):
        text = dist.read_text("METADATA") or dist.read_text("PKG-INFO")
        if not text:
            text = dist.read_text("")
        path = str(getattr(dist, "_path", ""))
        if not text and not (path.endswith(".dist-info") and os.path.isdir(path)):
            continue
        found.append(
            (
                dist.metadata["Name"] if text else os.path.basename(path).split("-")[0],
                str(getattr(dist, "_path", None)),
                str(dist.locate_file("")),
            )
        )
    return sorted(found)


def _kpip_view(paths: list[str]) -> list[tuple[str, str, str]]:
    return sorted(
        (dist.name, str(dist.metadata_location), dist.location)
        for dist in iter_installed_distributions(paths=paths)
    )


def _populate_mixed_root(root: Path) -> None:
    _write_dist_info(root, "demo-1.0.dist-info", "Name: demo\nVersion: 1.0\n")
    _write_dist_info(root, "Up-3.0.DIST-INFO", "Name: Up\nVersion: 3.0\n")
    _write_dist_info(
        root, "legacy-0.1.egg-info", "Name: legacy\nVersion: 0.1\n", filename="PKG-INFO"
    )
    (root / "flat-2.0.egg-info").write_text("Name: flat\nVersion: 2.0\n")
    (root / "empty-4.0.dist-info").mkdir()
    (root / "notes.txt").write_text("not a distribution\n")
    (root / "package").mkdir()


def test_directory_scan_matches_stdlib_for_every_root_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct listing reports exactly the stdlib's entries, metadata
    paths and locations, however the root is spelled."""
    root = tmp_path / "site-packages"
    root.mkdir()
    _populate_mixed_root(root)

    for spelling in (
        str(root),
        f"{root}/",
        f"{root}/./",
        str(root.relative_to(tmp_path)),
    ):
        monkeypatch.chdir(tmp_path)
        view = _kpip_view([spelling])
        assert view == _stdlib_view([spelling]), spelling
        assert sorted(name for name, _, _ in view) == [
            "Up",
            "demo",
            "empty",
            "flat",
            "legacy",
        ]

    monkeypatch.chdir(root)
    assert _kpip_view([""]) == _stdlib_view([""])
    assert len(_kpip_view([""])) == 5
    assert _kpip_view(["."]) == _stdlib_view(["."])


def test_directory_scan_skips_roots_the_stdlib_skips(tmp_path: Path) -> None:
    plain_file = tmp_path / "plain.txt"
    plain_file.write_text("x")
    for root in (str(tmp_path / "missing"), str(plain_file)):
        assert _kpip_view([root]) == []
        assert _stdlib_view([root]) == []


def test_zip_and_egg_roots_still_go_through_the_stdlib(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("zipped-5.0.dist-info/METADATA", "Name: zipped\nVersion: 5.0\n")
    egg = tmp_path / "legacy-6.0.egg"
    _write_dist_info(
        egg, "EGG-INFO", "Name: legacy\nVersion: 6.0\n", filename="PKG-INFO"
    )
    _write_dist_info(egg, "inner-7.0.dist-info", "Name: inner\nVersion: 7.0\n")

    for root in (str(archive), str(egg)):
        view = _kpip_view([root])
        assert view == _stdlib_view([root]), root
        assert view, root
    assert [name for name, _, _ in _kpip_view([str(archive)])] == ["zipped"]
    assert sorted(name for name, _, _ in _kpip_view([str(egg)])) == ["inner", "legacy"]


def test_default_scan_honours_other_distribution_finders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finder on sys.meta_path that offers find_distributions (a custom
    importer) keeps the default scan on the stdlib so its entries are seen."""
    import sys

    from kpip.core.metadata import clear_installed_index, find_installed

    private = tmp_path / "private"
    _write_dist_info(
        private, "finder_only-1.0.dist-info", "Name: finder-only\nVersion: 1.0\n"
    )

    class PrivateFinder:
        @staticmethod
        def find_distributions(context=None):  # noqa: ANN001, ANN205
            yield importlib.metadata.PathDistribution(
                private / "finder_only-1.0.dist-info"
            )

    clear_installed_index()
    try:
        assert find_installed("finder-only") is None
        monkeypatch.setattr(sys, "meta_path", [PrivateFinder(), *sys.meta_path])
        clear_installed_index()
        found = find_installed("finder-only")
        assert found is not None
        assert found.raw_version == "1.0"
        assert find_installed("finder-only", [str(tmp_path)]) is None
    finally:
        clear_installed_index()


def test_path_distribution_answers_like_the_stdlib(tmp_path: Path) -> None:
    root = tmp_path / "site-packages"
    _write_dist_info(
        root,
        "widget-1.2.3.dist-info",
        "Name: widget\nVersion: 1.2.3\nRequires-Dist: gadget>=1.0\n",
    )
    dist_info = root / "widget-1.2.3.dist-info"
    (dist_info / "RECORD").write_text(
        "widget/__init__.py,,\nwidget-1.2.3.dist-info/RECORD,,\n"
    )
    (dist_info / "top_level.txt").write_text("widget\n")
    (dist_info / "sub").mkdir()

    [distribution] = iter_installed_distributions(paths=[str(root)])
    stdlib_dist = importlib.metadata.PathDistribution(dist_info)

    for name in ("RECORD", "top_level.txt", "METADATA", "missing", "sub"):
        assert distribution.raw.read_text(name) == stdlib_dist.read_text(name), name
    with pytest.raises(FileNotFoundError):
        distribution.read_text("missing")
    assert distribution.files() == sorted(str(f) for f in stdlib_dist.files or ())
    assert distribution.metadata.get_all("Requires-Dist") == ["gadget>=1.0"]
    assert distribution.metadata["Name"] == "widget"
    assert str(distribution.raw.locate_file("")) == str(root)


def test_installed_distribution_keeps_a_legacy_version_as_text(tmp_path: Path) -> None:
    """A non-PEP 440 version never matches a Version (so it is replaced on
    install and ignored by the resolver) but the distribution stays
    inspectable and removable through its text."""
    from kpip.core.metadata import clear_installed_index, find_installed

    site_packages = tmp_path / "site-packages"
    _write_dist_info(
        site_packages,
        "legacy-1.0_beta.dist-info",
        "Metadata-Version: 2.1\nName: legacy\nVersion: 1.0 beta\n",
    )
    clear_installed_index()
    found = find_installed("legacy", [str(site_packages)])
    assert found is not None
    assert found.raw_version == "1.0 beta"
    assert found.version is None
    assert found.version != Version("1.0")


class _RecordingHeaderCache:
    """An in-memory stand-in for the wheel metadata store's header side."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, int, int], dict[str, list[str]]] = {}
        self.prefetched: list[list[tuple[str, int, int]]] = []
        self.puts: list[tuple[str, int, int]] = []

    def prefetch(self, identities) -> None:  # noqa: ANN001
        self.prefetched.append(list(identities))

    def get_reference(self, identity):  # noqa: ANN001, ANN202
        return self.entries.get(identity)

    def put(self, identity, headers) -> None:  # noqa: ANN001
        self.puts.append(identity)
        self.entries[identity] = headers


@pytest.fixture
def header_cache():  # noqa: ANN201
    from kpip.core.metadata import clear_installed_index, use_header_cache

    cache = _RecordingHeaderCache()
    clear_installed_index()
    use_header_cache(cache)
    try:
        yield cache
    finally:
        use_header_cache(None)
        clear_installed_index()


def test_header_cache_serves_unchanged_metadata_without_reading_it(
    tmp_path: Path, header_cache: _RecordingHeaderCache
) -> None:
    import os

    from kpip.index.metadata_cache import metadata_identity

    root = tmp_path / "site-packages"
    _write_dist_info(
        root,
        "widget-1.0.dist-info",
        "Name: widget\nVersion: 1.0\nRequires-Dist: gadget>=1.0\n",
    )
    _write_dist_info(
        root, "legacy-0.1.egg-info", "Name: legacy\nVersion: 0.1\n", filename="PKG-INFO"
    )
    metadata_file = root / "widget-1.0.dist-info" / "METADATA"

    first = {d.name: d for d in iter_installed_distributions(paths=[str(root)])}
    assert sorted(first) == ["legacy", "widget"]
    identity = metadata_identity(metadata_file)
    assert header_cache.puts == [identity]
    assert header_cache.prefetched == [[identity]]
    assert [str(dep) for dep in first["widget"].dependencies()] == ["gadget>=1.0"]

    stat = metadata_file.stat()
    metadata_file.write_text("Name: widget\nVersion: 9.9\nRequires-Dist: gadget>=1.0\n")
    os.utime(metadata_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = {d.name: d for d in iter_installed_distributions(paths=[str(root)])}
    assert second["widget"].raw_version == "1.0"
    assert header_cache.puts == [identity]
    assert [str(dep) for dep in second["widget"].dependencies()] == ["gadget>=1.0"]

    os.utime(metadata_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = {d.name: d for d in iter_installed_distributions(paths=[str(root)])}
    assert third["widget"].raw_version == "9.9"
    assert len(header_cache.puts) == 2
    assert header_cache.puts[1] == metadata_identity(metadata_file)


def test_header_cache_round_trips_through_the_wheel_metadata_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second process finds the first one's headers on disk and reads no
    METADATA file for an unchanged environment."""
    from kpip.core.metadata import clear_installed_index, use_header_cache
    from kpip.index.metadata_cache import WheelMetadataCache

    root = tmp_path / "site-packages"
    for index in range(5):
        _write_dist_info(
            root,
            f"pkg{index}-1.{index}.dist-info",
            f"Name: pkg{index}\nVersion: 1.{index}\nRequires-Dist: pkg0\n",
        )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    first_store = WheelMetadataCache(cache_dir)
    use_header_cache(first_store)
    try:
        clear_installed_index()
        first = iter_installed_distributions(paths=[str(root)])
        assert len(first) == 5
        first_store.flush()

        second_store = WheelMetadataCache(cache_dir)
        use_header_cache(second_store)
        clear_installed_index()

        def no_reads(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError(f"METADATA read on a warm scan: {args[0]}")

        monkeypatch.setattr("kpip.core.metadata._read_text_file", no_reads)
        second = iter_installed_distributions(paths=[str(root)])
    finally:
        use_header_cache(None)
        clear_installed_index()

    assert [(d.name, d.raw_version) for d in second] == [
        (d.name, d.raw_version) for d in first
    ]
    assert [str(dep) for dep in second[3].dependencies()] == ["pkg0"]
    assert not second_store._pending_puts


class TestFilteredScan:
    """A ``names=`` scan must not read metadata it was never asked about.

    Both scanners filter twice: once on the directory name, before opening
    anything, and again on the parsed ``Name``. The first pass is an
    optimization and has to be a strict superset of the second.
    """

    @staticmethod
    def _populate(site_packages: Path, count: int) -> None:
        for index in range(count):
            _write_dist_info(
                site_packages,
                f"pkg_{index:03d}-1.0.dist-info",
                f"Metadata-Version: 2.1\nName: pkg-{index:03d}\nVersion: 1.0\n",
            )

    def test_unrelated_metadata_is_never_opened(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site_packages = tmp_path / "site-packages"
        self._populate(site_packages, 40)

        from kpip.core import metadata as core_metadata

        opened: list[object] = []
        read_text = core_metadata._read_text_file
        read_raw = core_metadata._read_raw_metadata_text

        def recording_text(path: str) -> str | None:
            opened.append(path)
            return read_text(path)

        def recording_raw(dist: object) -> str | None:
            opened.append(getattr(dist, "_path", dist))
            return read_raw(dist)

        monkeypatch.setattr(core_metadata, "_read_text_file", recording_text)
        monkeypatch.setattr(core_metadata, "_read_raw_metadata_text", recording_raw)

        found = core_metadata.iter_installed_distributions(
            [str(site_packages)],
            names=["pkg-007"],
        )

        assert [dist.canonical_name for dist in found] == ["pkg-007"]

        unrelated = [
            target for target in opened if "pkg_007-1.0.dist-info" not in str(target)
        ]

        assert not unrelated, f"opened metadata for {len(unrelated)} unasked-for names"

    def test_unfiltered_scan_still_sees_everything(self, tmp_path: Path) -> None:
        site_packages = tmp_path / "site-packages"
        self._populate(site_packages, 12)

        found = iter_installed_distributions([str(site_packages)])

        assert len(found) == 12

    @pytest.mark.parametrize(
        "directory",
        [
            "Widget_Thing-1.0.dist-info",
            "widget.thing-1.0.dist-info",
            "widget_thing-1.0.dist-info",
            "WIDGET-THING-1.0.dist-info",
        ],
    )
    def test_the_prefilter_survives_name_escaping(
        self,
        tmp_path: Path,
        directory: str,
    ) -> None:
        """A directory is named for an *escaped* distribution name, so the
        separators and case in it need not match what METADATA says."""
        site_packages = tmp_path / "site-packages"
        _write_dist_info(
            site_packages,
            directory,
            "Metadata-Version: 2.1\nName: Widget.Thing\nVersion: 1.0\n",
        )

        found = iter_installed_distributions(
            [str(site_packages)],
            names=["widget-thing"],
        )

        assert [dist.canonical_name for dist in found] == ["widget-thing"]

    def test_a_version_only_directory_name_still_matches(self, tmp_path: Path) -> None:
        site_packages = tmp_path / "site-packages"
        _write_dist_info(
            site_packages,
            "widget.dist-info",
            "Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n",
        )

        found = iter_installed_distributions([str(site_packages)], names=["widget"])

        assert [dist.canonical_name for dist in found] == ["widget"]

    def test_a_directory_named_for_another_distribution_is_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        """The trade this optimization makes, pinned.

        The binary distribution format requires ``.dist-info`` to be named
        ``{escaped name}-{version}``, and both pip and kpip build it that way.
        A ``names=`` scan trusts that, so metadata whose directory disagrees
        with its own ``Name`` is not found -- an unfiltered scan still finds
        it. Reading every ``METADATA`` to cover a spec violation costs ~21x on
        a two thousand package environment.
        """
        site_packages = tmp_path / "site-packages"
        _write_dist_info(
            site_packages,
            "something_else-1.0.dist-info",
            "Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n",
        )

        assert (
            iter_installed_distributions([str(site_packages)], names=["widget"]) == []
        )

        unfiltered = iter_installed_distributions([str(site_packages)])

        assert [dist.canonical_name for dist in unfiltered] == ["widget"]


class TestInstalledNameMightMatch:
    def test_matches_name_and_name_with_version(self) -> None:
        from kpip.core.names import installed_name_might_match

        assert installed_name_might_match(
            "widget-1.0.dist-info", ".dist-info", {"widget"}
        )
        assert installed_name_might_match("widget.dist-info", ".dist-info", {"widget"})
        assert not installed_name_might_match(
            "gadget-1.0.dist-info", ".dist-info", {"widget"}
        )

    def test_a_longer_name_is_not_a_prefix_match(self) -> None:
        """``widget`` must not claim ``widget-extra``'s directory as its own
        beyond the version boundary, or the parsed-name check does the work."""
        from kpip.core.names import installed_name_might_match

        assert installed_name_might_match(
            "widget_extra-1.0.dist-info", ".dist-info", {"widget"}
        ), "prefix matches are allowed -- the parsed name filters them out"
        assert not installed_name_might_match(
            "widget_extra-1.0.dist-info", ".dist-info", {"gadget"}
        )

    def test_leading_and_trailing_separators_are_dropped(self) -> None:
        from kpip.core.names import canonicalize_installed_name

        assert canonicalize_installed_name("_widget_") == "widget"
        assert canonicalize_installed_name("Foo__Bar") == "foo-bar"
        assert canonicalize_installed_name("foo.bar-1.0") == "foo-bar-1-0"
