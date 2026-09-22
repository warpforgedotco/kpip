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


def test_asking_about_a_level_agrees_with_logging_at_it() -> None:
    """The answer matches what the emit methods actually do.

    Nothing configured means debug goes nowhere and a warning still
    reaches ``lastResort``, so reporting every level as disabled would let
    ``if isEnabledFor(WARNING)`` drop a warning that ``warning()`` prints.
    """
    source = (
        "import sys\n"
        "from kpip.core.logger import get_logger\n"
        "log = get_logger('demo')\n"
        "print(log.isEnabledFor(10), log.isEnabledFor(20),"
        " log.isEnabledFor(30), log.isEnabledFor(40),"
        " 'logging' in sys.modules)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False False True True False"


def test_a_guarded_warning_is_not_dropped() -> None:
    """The shape the guard exists for, end to end."""
    source = (
        "from kpip.core.logger import get_logger\n"
        "log = get_logger('demo')\n"
        "if log.isEnabledFor(30):\n"
        "    log.warning('guarded and heard')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "guarded and heard" in result.stderr
