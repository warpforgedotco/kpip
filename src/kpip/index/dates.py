from __future__ import annotations

lazy import datetime


def parse_iso_datetime(isodate: str) -> datetime.datetime:
    """Parse an ISO datetime, including a trailing ``Z``."""
    return datetime.datetime.fromisoformat(isodate)
