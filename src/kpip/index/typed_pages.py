"""Simple API JSON pages decoded into only the fields kpip reads, when msgspec is.

``json.loads`` builds a dict for every file a page lists and a string for
every one of its fields, most of them never read: provenance, the upload's
size in the core metadata's hashes, data-dist-info-metadata beside
core-metadata. A cold airflow lock decodes 587 pages listing 265,000 files.
msgspec decodes each file straight into a struct of the eight fields the
catalog stores and skips the rest: a cold airflow resolve replayed offline
takes 7.3 s rather than 8.2 s.

The binary always bundles msgspec; a source run may lack it. Without it, or
for a page it will not take (an entry that is not an object, ``files`` that
is not a list), :func:`decode_page` returns None and the page is read with
``json`` as before. Every field is
typed ``Any``, so an odd value is passed through for the record builder to
judge exactly as it judges one read by ``json``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    import msgspec
except ImportError:
    msgspec = None  # ty: ignore[invalid-assignment]

UNSET: object
"""A field the entry does not have: msgspec's own marker, when it is there."""

_decoder: Callable[[Any], Any] | None

_errors: tuple[type[BaseException], ...]

if msgspec is None:
    UNSET = object()
    _decoder = None
    _errors = ()
else:
    # Built with defstruct, from field types rather than annotations: from
    # 3.14 a class's annotations are a function evaluated on demand, and a
    # compiled kpip's are not what annotationlib needs -- the Nuitka binary
    # failed on its first page.
    File = msgspec.defstruct(
        "File",
        [
            ("url", Any, None),
            ("filename", Any, None),
            ("hashes", Any, None),
            ("requires_python", Any, None),
            ("yanked", Any, None),
            ("upload_time", Any, None),
            ("size", Any, None),
            # Present and null is not absent: a null core-metadata hides an
            # older dist-info-metadata beside it.
            ("core_metadata", Any, msgspec.UNSET),
            ("dist_info_metadata", Any, msgspec.UNSET),
        ],
        rename={
            "requires_python": "requires-python",
            "upload_time": "upload-time",
            "core_metadata": "core-metadata",
            "dist_info_metadata": "dist-info-metadata",
        },
    )

    Page = msgspec.defstruct(
        "Page",
        [("meta", Any, None), ("files", list[File], [])],  # ty: ignore[invalid-type-form]
    )

    UNSET = msgspec.UNSET

    _decoder = msgspec.json.Decoder(Page).decode

    # ValidationError is a DecodeError.
    _errors = (msgspec.DecodeError,)


def decode_page(body: str | bytes) -> Any | None:
    """``body`` as a page of file structs, or None to read it with ``json``."""
    if _decoder is None:
        return None

    try:
        return _decoder(body)
    except _errors:
        return None
