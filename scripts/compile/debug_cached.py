"""Temporary diagnostic: what a cached catalog holds for requests."""
import os
import sys

from kpip.core.appdirs import http_cache_path, resolve_cache_dir
from kpip.index.catalog_cache import load_records, wheel_file_from_record
from kpip.network.cache import SafeFileCache

cache = SafeFileCache(http_cache_path(resolve_cache_dir(sys.argv[1])))
url = "https://pypi.org/simple/requests/"
records = load_records(cache, url)
print("records:", None if records is None else len(records))
for record in (records or [])[-3:]:
    print("record:", record)
    try:
        print("  wheel:", wheel_file_from_record(record))
    except Exception as exc:  # noqa: BLE001
        print("  wheel error:", repr(exc))
from kpip.core import packaging

print("target python:", packaging.target_python_version(), sys.version)
print("sep:", os.sep, "platform:", sys.platform)
