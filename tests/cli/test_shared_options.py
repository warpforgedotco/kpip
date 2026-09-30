"""The options pip's requirement commands share, as their parsers read them."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from kpip.cli.package_finder import apply_refresh
from kpip.cli.parsers.download import create_parser as download_parser
from kpip.cli.parsers.install import create_parser as install_parser
from kpip.cli.parsers.lock import create_parser as lock_parser
from kpip.cli.parsers.uninstall import create_parser as uninstall_parser
from kpip.cli.parsers.wheel import create_parser as wheel_parser
from kpip.core import expiry


class TestParsing:
    PARSERS = (install_parser, download_parser, wheel_parser, lock_parser)

    @pytest.mark.parametrize("parser", PARSERS)
    def test_refresh_package_collects_names_as_pip_does(self, parser) -> None:  # noqa: ANN001
        def given(*values: str) -> set[str]:
            args = [arg for value in values for arg in ("--refresh-package", value)]
            return set(parser().parse_args(["lib", *args]).refresh_package)

        assert given() == set()
        assert given("Foo_Bar,baz") == {"foo-bar", "baz"}
        assert given("foo", ":all:") == {":all:"}
        assert given(":all:", "foo") == {":all:", "foo"}
        assert given("foo", ":none:", "bar") == {"bar"}
        assert given("foo,:all:,:none:,bar") == {"bar"}

    @pytest.mark.parametrize("parser", PARSERS)
    def test_refresh_package_revalidates_what_was_cached(self, parser) -> None:  # noqa: ANN001
        assert expiry._refreshed_since == float("-inf")

        apply_refresh(parser().parse_args(["lib"]))
        apply_refresh(parser().parse_args(["lib", "--refresh-package", ":none:"]))

        assert expiry._refreshed_since == float("-inf")

        apply_refresh(parser().parse_args(["lib", "--refresh-package", "lib"]))

        assert expiry._refreshed_since > float("-inf")

    @pytest.mark.parametrize("parser", PARSERS)
    def test_uploaded_prior_to_is_a_datetime_or_a_number_of_days(self, parser) -> None:  # noqa: ANN001
        def given(value: str) -> datetime.datetime:
            return (
                parser()
                .parse_args(["lib", "--uploaded-prior-to", value])
                .uploaded_prior_to
            )

        assert given("2023-01-01T00:00:00Z") == datetime.datetime(
            2023, 1, 1, tzinfo=datetime.UTC
        )
        assert given("2023-01-01").tzinfo is not None

        cutoff = given("P3D")
        ago = datetime.datetime.now(datetime.UTC) - cutoff
        assert datetime.timedelta(days=3) <= ago < datetime.timedelta(days=3, minutes=1)

    def test_uploaded_prior_to_refuses_anything_else(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            install_parser().parse_args(["lib", "--uploaded-prior-to", "soon"])

        assert "Expected an ISO 8601 datetime string" in capsys.readouterr().err

    @pytest.mark.parametrize("parser", PARSERS)
    @pytest.mark.parametrize(
        "spelling", ["--src", "--source", "--source-dir", "--source-directory"]
    )
    def test_the_source_directory_is_made_absolute(
        self,
        parser,
        spelling: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        monkeypatch.chdir(tmp_path)

        options = parser().parse_args(["lib", spelling, "checkouts"])

        assert Path(options.src_dir) == Path.cwd() / "checkouts"

    @pytest.mark.parametrize("parser", PARSERS)
    def test_options_pip_takes_that_change_nothing_here(self, parser) -> None:  # noqa: ANN001
        parser().parse_args(
            ["lib", "--progress-bar", "off", "--no-clean", "--use-pep517"]
        )


def test_uninstall_takes_the_options_pip_has_for_managed_environments() -> None:
    options = uninstall_parser().parse_args(
        ["demo", "--break-system-packages", "--root-user-action", "ignore"]
    )

    assert options.break_system_packages
    assert options.root_user_action == "ignore"
