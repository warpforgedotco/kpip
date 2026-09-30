"""``freeze --local``, as pip takes it."""

from __future__ import annotations

from pathlib import Path

import pytest
from kpip.cli.main import main
from kpip.cli.parsers.freeze import create_parser as freeze_parser


class TestFreezeLocal:
    @pytest.mark.parametrize("spelling", ["-l", "--local"])
    def test_local_is_passed_to_the_listing(
        self, spelling: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kpip.cli import freeze

        seen: dict[str, object] = {}

        def listing(**options: object) -> list[str]:
            seen.update(options)
            return []

        monkeypatch.setattr(freeze, "freeze", listing)

        assert freeze.run_freeze([spelling]) == 0
        assert seen["local_only"] is True

        assert freeze.run_freeze([]) == 0
        assert seen["local_only"] is False

    @pytest.mark.parametrize("scope", ["--local", "--user"])
    def test_a_path_cannot_be_combined_with_a_scope(
        self, scope: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["freeze", "--path", str(tmp_path), scope]) != 0

        assert (
            "Cannot combine '--path' with '--user' or '--local'"
            in capsys.readouterr().err
        )

    def test_the_parser_takes_it(self) -> None:
        assert freeze_parser().parse_args(["-l"]).local
