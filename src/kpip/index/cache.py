"""Cache-key and origin metadata helpers for built artifacts."""

from __future__ import annotations

import json
import os

from kpip.core.digests import sha256_hexdigest
from kpip.core.logger import get_logger
from kpip.core.appdirs import WHEEL_CACHE_BUCKET

logger = get_logger(__name__)


"""Directory under the cache directory holding wheels built from source."""


def wheel_cache_path(root: str, url: str) -> str:
    """The entry directory for ``url``'s built wheel under cache directory ``root``."""
    digest = sha256_hexdigest(url.encode("utf-8"))
    return os.path.join(root, WHEEL_CACHE_BUCKET, digest[:2], digest[2:4], digest)


def origin_hashes(
    path: str | os.PathLike[str],
    *,
    expected_cache_key: str | None = None,
) -> dict[str, str] | None:
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (FileNotFoundError, IsADirectoryError):
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid cache entry origin file %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring invalid cache entry origin file %s", path)
        return None
    if expected_cache_key is not None and data.get(
        "cache_key_hash"
    ) != sha256_hexdigest(expected_cache_key.encode()):
        logger.warning("Ignoring mismatched cache entry origin file %s", path)
        return None
    archive_info = data.get("archive_info")
    if not isinstance(archive_info, dict):
        return None
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, dict):
        return None
    return {str(name): str(value) for name, value in hashes.items()}
