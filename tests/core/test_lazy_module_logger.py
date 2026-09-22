"""A logger that does not cost ``logging`` until it has something to say."""

from __future__ import annotations

import subprocess
import sys

import pytest
from kpip.core.logger import get_logger


def test_a_dropped_record_does_not_import_logging() -> None:
    """The whole point: a quiet run never pays for the logging machinery.

    Run out of process, because the test runner has imported ``logging``
    long before this and the stand-in rightly defers to it once it has.
    """
    source = (
        "import sys\n"
        "from kpip.core.logger import get_logger\n"
        "get_logger('demo').debug('nobody is listening')\n"
        "get_logger('demo').info('nor to this')\n"
        "print('logging' in sys.modules)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_a_warning_is_never_dropped() -> None:
    """Warnings reach ``logging.lastResort`` as they always did."""
    source = (
        "from kpip.core.logger import get_logger\nget_logger('demo').warning('heard')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "heard" in result.stderr


def test_records_flow_once_anything_has_imported_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("kpip.test.lazy")

    with caplog.at_level("DEBUG", logger="kpip.test.lazy"):
        logger.debug("a debug record")
        logger.warning("a warning record")

    assert [record.message for record in caplog.records] == [
        "a debug record",
        "a warning record",
    ]


def test_the_named_logger_is_the_one_that_is_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("kpip.test.named")

    with caplog.at_level("DEBUG", logger="kpip.test.named"):
        logger.debug("under its own name")

    assert caplog.records[0].name == "kpip.test.named"


def test_a_level_nothing_can_hear_is_not_enabled() -> None:
    source = (
        "import sys\n"
        "from kpip.core.logger import get_logger\n"
        "print(get_logger('demo').isEnabledFor(10), 'logging' in sys.modules)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False False"
