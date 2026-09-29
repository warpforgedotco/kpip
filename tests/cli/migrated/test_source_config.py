from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from kpip.cli.config import (
    CONFIG_BASENAME,
    SourceConfig,
    load_source_config,
    resolve_sources,
)
from kpip.core.errors import ConfigurationError


@pytest.fixture(autouse=True)
def clean_source_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KPIP_FIND_LINKS",
        "KPIP_INDEX_URL",
        "KPIP_EXTRA_INDEX_URL",
        "KPIP_NO_INDEX",
        "KPIP_CONFIG_FILE",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "kpip.conf"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("KPIP_CONFIG_FILE", str(path))


def test_blank_find_links_is_no_find_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only value configures nothing.

    The install fast path used to test the raw config string for truthiness,
    so "   " read as "a wheelhouse is configured" and made it decline. Both
    readers now agree that it configures no find-links.
    """
    write_config(tmp_path, "[global]\nfind-links =    \n", monkeypatch)

    assert load_source_config("install").find_links == []


def test_command_section_overrides_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(
        tmp_path,
        "[global]\nindex-url = https://global.example/simple\n"
        "[install]\nindex-url = https://install.example/simple\n",
        monkeypatch,
    )

    assert load_source_config("install").index_url == "https://install.example/simple"
    assert load_source_config("list").index_url == "https://global.example/simple"


def test_environment_overrides_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, "[global]\nno-index = false\n", monkeypatch)
    monkeypatch.setenv("KPIP_NO_INDEX", "yes")
    monkeypatch.setenv("KPIP_FIND_LINKS", "/one /two")

    config = load_source_config("install")

    assert config.no_index is True
    assert config.find_links == ["/one", "/two"]


def test_resolve_sources_prefers_command_line() -> None:
    config = SourceConfig(
        ["/configured"],
        "https://configured.example/simple",
        ["https://configured.example/extra"],
        False,
    )
    options = argparse.Namespace(
        find_links=["/given"],
        index_url="https://given.example/simple",
        extra_index_url=[],
        no_index=True,
    )

    resolved = resolve_sources(options, config)

    assert resolved.find_links == ["/given"]
    assert resolved.index_url == "https://given.example/simple"
    assert resolved.extra_index_urls == ["https://configured.example/extra"]
    assert resolved.no_index is True


def test_resolve_sources_without_find_links_option() -> None:
    """``kpip index`` has no --find-links; the configured value survives."""
    config = SourceConfig(["/configured"], None, [], False)
    options = argparse.Namespace(index_url=None, extra_index_url=[], no_index=False)

    assert resolve_sources(options, config).find_links == ["/configured"]


def test_unparseable_configuration_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to defaults dropped the file's valid settings with it:
    ``no-index = true`` beside one bad line meant an install from PyPI."""
    write_config(
        tmp_path,
        "[global]\nno-index = true\nthis line is not an option\n",
        monkeypatch,
    )

    with pytest.raises(ConfigurationError, match="could not be loaded"):
        load_source_config("install")


def write_user_config(
    tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "xdg"
    (home / "kpip").mkdir(parents=True)
    (home / "kpip" / CONFIG_BASENAME).write_text(body, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))


def test_config_file_devnull_loads_no_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_config(tmp_path, "[global]\nno-index = true\n", monkeypatch)
    monkeypatch.setenv("KPIP_CONFIG_FILE", os.devnull)

    assert load_source_config("install").no_index is False


def test_existing_config_file_replaces_the_user_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user_config(tmp_path, "[global]\nno-index = true\n", monkeypatch)
    write_config(tmp_path, "[global]\nfind-links = /wheels\n", monkeypatch)

    config = load_source_config("install")

    assert config.no_index is False
    assert config.find_links == ["/wheels"]


def test_user_file_is_the_same_with_or_without_xdg_config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``~/.config/kpip/kpip.conf`` is read whether or not XDG_CONFIG_HOME
    names ``~/.config``; it used to be a different file in each case."""
    from kpip.cli.config import user_config_paths

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    without = user_config_paths()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    assert user_config_paths() == without
    assert without[-1] == str(tmp_path / ".config" / "kpip" / CONFIG_BASENAME)


def test_legacy_user_file_is_still_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / CONFIG_BASENAME).write_text(
        "[global]\nno-index = true\n", encoding="utf-8"
    )

    assert load_source_config("install").no_index is True
