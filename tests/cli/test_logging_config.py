"""Logging as pip sets it up: what goes where, and what -q and -v change."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from kpip.cli.entrypoint import main
from kpip.cli.inspect_hash import run_hash
from kpip.cli.logging_config import VERBOSE, configure_logging, level_for, set_log_file

logger = logging.getLogger("kpip.test")


def _say_everything() -> None:
    logger.debug("debug")
    logger.log(VERBOSE, "verbose")
    logger.info("info")
    logger.warning("warning")
    logger.error("error")
    logger.critical("critical")


@pytest.mark.parametrize(
    "verbosity, level",
    [
        (3, logging.DEBUG),
        (2, logging.DEBUG),
        (1, VERBOSE),
        (0, logging.INFO),
        (-1, logging.WARNING),
        (-2, logging.ERROR),
        (-3, logging.CRITICAL),
        (-4, logging.CRITICAL),
    ],
)
def test_verbosity_maps_to_a_level_by_pips_table(verbosity: int, level: int) -> None:
    assert level_for(verbosity) == level


def test_output_goes_to_stdout_and_problems_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(0)
    _say_everything()
    captured = capsys.readouterr()

    assert captured.out == "info\n"
    assert captured.err == "WARNING: warning\nERROR: error\nERROR: critical\n"


@pytest.mark.parametrize(
    "verbosity, out, err",
    [
        (2, "debug\nverbose\ninfo\n", 3),
        (1, "verbose\ninfo\n", 3),
        (-1, "", 3),
        (-2, "", 2),
        (-3, "", 1),
    ],
)
def test_quiet_and_verbose_change_how_much_is_shown(
    capsys: pytest.CaptureFixture[str], verbosity: int, out: str, err: int
) -> None:
    configure_logging(verbosity)
    _say_everything()
    captured = capsys.readouterr()

    assert captured.out == out
    assert len(captured.err.splitlines()) == err


def test_setting_up_again_replaces_the_handlers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(0)
    configure_logging(0)
    logger.info("once")

    assert capsys.readouterr().out == "once\n"


def test_a_log_file_records_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "kpip.log"
    set_log_file(str(log))
    configure_logging(-1)
    _say_everything()
    configure_logging(0)

    assert capsys.readouterr().out == ""
    assert log.read_text().splitlines() == [
        "debug",
        "verbose",
        "info",
        "WARNING: warning",
        "ERROR: error",
        "ERROR: critical",
    ]


def _site(tmp_path: Path) -> Path:
    info = tmp_path / "site" / "demo-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Name: demo\nVersion: 1.0\n")
    return tmp_path / "site"


def test_quiet_is_taken_before_or_after_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = str(_site(tmp_path))

    assert main(["list", "--path", site]) == 0
    assert "demo" in capsys.readouterr().out

    for argv in (
        ["list", "-q", "--path", site],
        ["-q", "list", "--path", site],
        ["--quiet", "list", "--path", site],
    ):
        assert main(argv) == 0
        assert capsys.readouterr().out == "", argv


def test_a_missing_package_is_a_warning_until_quiet_twice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["show", "no-such-package-here"]) == 1
    assert capsys.readouterr().err == (
        "WARNING: Package(s) not found: no-such-package-here\n"
    )

    assert main(["show", "-qq", "no-such-package-here"]) == 1
    assert capsys.readouterr().err == ""


def test_hash_is_written_as_pip_writes_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hello = tmp_path / "hello"
    hello.write_bytes(b"hello")

    assert run_hash([str(hello)]) == 0
    assert capsys.readouterr().out == (
        f"{hello}:\n"
        "--hash=sha256:"
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824\n"
    )

    assert run_hash(["-q", str(hello)]) == 0
    assert capsys.readouterr().out == ""
