"""Compiled page-to-catalog loop for :meth:`IndexPageParser.catalog_from_json`.

Cython pure-Python-mode source: this file only works compiled, and
``page_parsing`` keeps its own Python loop when the extension is absent (PyPy,
a source checkout, a platform without a compiler).

A cold airflow lock compiles 587 index pages listing 265,000 files, and in
Python each file was a dozen calls -- joining its URL, matching it, cutting its
name out, building its record, parsing its wheel name into a ``WheelFile``
whose tags nothing reads -- 13 million calls in all. Here the plain case, every
file PyPI serves, is one pass over each file's fields with the string work done
in C; a wheel's name is checked the way ``parse_wheel_file`` checks it but only
its project and version are kept. Anything unusual -- a URL that is not
``https://host/path``, a name ``PathComponent`` has to fix, a source archive --
goes back to the Python functions that own those rules, handed in by
:func:`_install`, so the catalog is the one the Python loop builds.
"""

import cython

if not cython.compiled:
    raise ImportError("kpip.index._page_catalog only works compiled")

import os
import urllib.parse

_join_index_url = None
_record_from_fields = None
_identity_for = None
_version = None
_invalid_version = None
_canonicalize_name = None
_plain_url_match = None
_wheel_kind = None
_metadata_kind = None
_attestation_kind = None
_sdist_kind = None
_unknown_kind = None
_source_suffixes = None
_WHEEL_RECORD = 1
_RECORD_REQUIRES_PYTHON = 3
_RECORD_YANKED = 4


def _install(
    join_index_url,
    record_from_fields,
    identity_for,
    version,
    invalid_version,
    canonicalize_name,
    plain_url,
    artifact_kinds,
    source_suffixes,
    record_positions,
):
    """Hand over the Python rules the loop defers to.

    Called once by ``page_parsing``; an extension built from an older source
    takes other arguments and raises ``TypeError``, which keeps the Python loop.
    """
    global _join_index_url, _record_from_fields, _identity_for, _version
    global _invalid_version, _canonicalize_name, _plain_url_match
    global _wheel_kind, _metadata_kind, _attestation_kind, _sdist_kind
    global _unknown_kind, _source_suffixes
    global _WHEEL_RECORD, _RECORD_REQUIRES_PYTHON, _RECORD_YANKED
    _join_index_url = join_index_url
    _record_from_fields = record_from_fields
    _identity_for = identity_for
    _version = version
    _invalid_version = invalid_version
    _canonicalize_name = canonicalize_name
    _plain_url_match = plain_url.match
    (
        _wheel_kind,
        _metadata_kind,
        _attestation_kind,
        _sdist_kind,
        _unknown_kind,
    ) = artifact_kinds
    _source_suffixes = tuple(source_suffixes)
    _WHEEL_RECORD, _RECORD_REQUIRES_PYTHON, _RECORD_YANKED = record_positions


@cython.cfunc
@cython.inline
def _joined(base_url: object, href: str) -> object:
    """``join_index_url`` where the answer is ``href`` itself; the rest there."""
    size: cython.Py_ssize_t = len(href)
    start: cython.Py_ssize_t
    if (
        size > 8
        and (href.startswith("https://") or href.startswith("http://"))
        and href.isascii()
        and href.isprintable()
        and href[size - 1] not in ";?#"
        and "?#" not in href
        and ";?" not in href
        and ";#" not in href
        and "[" not in href
        and "]" not in href
    ):
        start = 8 if href[4] == "s" else 7
        if href[start] not in "/?#":
            return href
    return _join_index_url(base_url, href)


@cython.cfunc
@cython.exceptval(-2, check=False)
def _plain_path_start(href: str) -> cython.Py_ssize_t:
    """Where ``href``'s path starts, when it is its own join and plain.

    One pass deciding what ``join_index_url`` returning ``href`` unchanged,
    ``PLAIN_URL`` matching it and it holding no ``&`` together decide: a
    lower-case ``http(s)://``, a non-empty host of ``[A-Za-z0-9.-_:]``, then a
    path free of whitespace, ``#``, ``?``, ``\\``, ``[`` and ``]``, every
    character printable ASCII and the last not ``;``. The path's index (the
    length when there is none), or -1 for anything else, which goes the long
    way. The separate scans -- ten substring searches, ``isascii``,
    ``isprintable`` and the regex -- were a fifth of a page's compile.
    """
    size: cython.Py_ssize_t = len(href)
    index: cython.Py_ssize_t
    start: cython.Py_ssize_t
    path_start: cython.Py_ssize_t
    character: cython.Py_UCS4
    if size <= 8 or not href.startswith("http"):
        return -1
    if href[4] == "s":
        if href[5] != ":" or href[6] != "/" or href[7] != "/":
            return -1
        start = 8
    elif href[4] == ":" and href[5] == "/" and href[6] == "/":
        start = 7
    else:
        return -1
    path_start = size
    index = start
    while index < size:
        character = href[index]
        if character == "/":
            path_start = index
            break
        if not (
            ("a" <= character <= "z")
            or ("A" <= character <= "Z")
            or ("0" <= character <= "9")
            or character == "."
            or character == "-"
            or character == "_"
            or character == ":"
        ):
            return -1
        index += 1
    if index == start:
        return -1
    while index < size:
        character = href[index]
        if (
            character <= " "
            or character >= "\x7f"
            or character == "#"
            or character == "?"
            or character == "\\"
            or character == "["
            or character == "]"
            or character == "&"
        ):
            return -1
        index += 1
    if href[size - 1] == ";":
        return -1
    return path_start


@cython.cfunc
def _kind(tail: str) -> object:
    """``Link.artifact_kind_from_filename``."""
    if tail.endswith(".whl"):
        return _wheel_kind
    if tail.endswith(".metadata"):
        return _metadata_kind
    if tail.endswith(".attestation"):
        return _attestation_kind
    if tail.endswith(_source_suffixes):
        return _sdist_kind
    return _unknown_kind


@cython.cfunc
def _stored(hashes: dict) -> object:
    """``_stored_hashes`` of a dict with string keys and values."""
    digest: object
    if len(hashes) == 1:
        digest = hashes.get("sha256")
        if digest is not None:
            return digest
    return dict(hashes)


@cython.cfunc
def _record(
    url: str,
    text: str,
    hashes: object,
    requires_python: object,
    yanked_reason: object,
    metadata: object,
    upload_time: object,
    size: object,
) -> tuple:
    """``json_record``."""
    stored_hashes: object
    stored_metadata: object
    digest: object
    if isinstance(hashes, dict):
        digest = hashes.get("sha256") if len(hashes) == 1 else None
        if type(digest) is str:
            stored_hashes = digest
        else:
            stored_hashes = _stored(
                {str(key): str(value) for key, value in hashes.items()}
            )
    else:
        stored_hashes = {}
    if isinstance(metadata, dict):
        if metadata:
            stored_metadata = _stored(
                {str(name): str(value) for name, value in metadata.items()}
            )
        else:
            stored_metadata = True
    else:
        stored_metadata = True if metadata is True else None
    return (
        url,
        text,
        stored_hashes,
        requires_python if isinstance(requires_python, str) else None,
        yanked_reason,
        stored_metadata,
        upload_time if isinstance(upload_time, str) else None,
        None,
        size if type(size) is int and size >= 0 else None,
    )


@cython.cfunc
def _wheel_identity(name: str, releases: dict) -> object:
    """``parse_wheel_file_once(name)``'s project and version, or None.

    The same checks, without the ``WheelFile`` and tags a catalog record does
    not keep; a wheel's tags are parsed when a release is read. A release's
    wheels share a project and version spelling, so ``releases`` keeps what
    each spelling came to for the rest of the page.
    """
    parts: list
    distribution: str
    build_tag: str
    count: cython.Py_ssize_t
    spelling: tuple
    identity: object
    if "/" in name or "\\" in name or ":" in name:
        name = os.path.basename(name)
    if not name.endswith(".whl"):
        return None
    parts = name[:-4].split("-")
    count = len(parts)
    if count != 5 and count != 6:
        return None
    if count == 6:
        build_tag = parts[2]
        if not ("0" <= build_tag[:1] <= "9"):
            return None
    spelling = (parts[0], parts[1])
    identity = releases.get(spelling, releases)
    if identity is not releases:
        return identity
    distribution = parts[0]
    if "__" in distribution or not (
        distribution.isalnum()
        or distribution.replace(".", "").replace("_", "").isalnum()
    ):
        identity = None
    else:
        try:
            version = _version(parts[1])
        except _invalid_version:
            identity = None
        else:
            identity = (_WHEEL_RECORD, _canonicalize_name(distribution), version.public)
    releases[spelling] = identity
    return identity


def compile_files(
    files: list,
    base_url: str,
    source_url: str,
    unset: object,
) -> tuple:
    """``catalog_from_json``'s groups and unparsed records for a page's files.

    ``files`` holds the page's decoded entries (``typed_pages``); ``unset``
    is the marker for a field an entry does not have.
    """
    grouped: dict = {}
    unparsed: list = []
    releases: dict = {}
    artifacts: list
    file_url: object
    metadata: object
    filename: object
    yanked: object
    url: str
    text: str
    path: str
    tail: str
    name: str
    yanked_reason: object
    record: object
    identity: object
    key: tuple
    path_start: cython.Py_ssize_t
    for entry in files:
        file_url = entry.url
        if not isinstance(file_url, str):
            continue
        metadata = entry.core_metadata
        if metadata is unset:
            metadata = entry.dist_info_metadata
            if metadata is unset:
                metadata = None
        filename = entry.filename
        yanked = entry.yanked
        path_start = _plain_path_start(file_url)
        if path_start >= 0:
            url = file_url
            path = url[path_start:]
        else:
            url = _joined(base_url, file_url)
            plain = _plain_url_match(url)
            if plain is None or "&" in url:
                path_start = -2
            else:
                path = plain.group(3) or ""
        if path_start == -2:
            record, identity = _record_from_fields(
                base_url,
                source_url,
                file_url,
                filename,
                yanked,
                entry.hashes,
                entry.requires_python,
                entry.upload_time,
                entry.size,
                metadata,
            )
        else:
            if "%" in path:
                path = urllib.parse.unquote(path)
            path = path.rstrip("/")
            tail = path[path.rfind("/") + 1 :]
            name = tail
            if "\\" in name or ":" in name:
                name = os.path.basename(name)
            if name == "" or name == "." or name == "..":
                # PathComponent names it from the host; its rules, not ours.
                record, identity = _record_from_fields(
                    base_url,
                    source_url,
                    file_url,
                    filename,
                    yanked,
                    entry.hashes,
                    entry.requires_python,
                    entry.upload_time,
                    entry.size,
                    metadata,
                )
            else:
                text = str(filename or "")
                if yanked is False or yanked is None:
                    yanked_reason = None
                elif yanked is True:
                    yanked_reason = ""
                else:
                    yanked_reason = str(yanked)
                record = _record(
                    url,
                    text,
                    entry.hashes,
                    entry.requires_python,
                    yanked_reason,
                    metadata,
                    entry.upload_time,
                    entry.size,
                )
                kind = _kind(tail)
                if kind is _wheel_kind:
                    identity = _wheel_identity(name, releases)
                else:
                    identity = _identity_for(kind, name)
        if identity is None:
            unparsed.append(record)
            continue
        key = (identity[1], identity[2])
        artifacts = grouped.get(key)
        if artifacts is None:
            grouped[key] = [(identity[0], record)]
        else:
            artifacts.append((identity[0], record))
    return _groups(grouped), unparsed


@cython.cfunc
def _groups(grouped: dict) -> list:
    """``compile_groups``."""
    result: list = []
    fact_masks: dict
    artifacts: list
    requires_python: object
    yanked: object
    kind: object
    record: tuple
    for key, artifacts in grouped.items():
        fact_masks = {}
        for kind, record in artifacts:
            requires_python = record[_RECORD_REQUIRES_PYTHON]
            yanked = record[_RECORD_YANKED]
            fact_key = (
                requires_python if isinstance(requires_python, str) else None,
                yanked if isinstance(yanked, str) else None,
            )
            fact_masks[fact_key] = fact_masks.get(fact_key, 0) | kind
        result.append(
            (
                key[0],
                key[1],
                artifacts,
                [
                    (kind_mask, fact[0], fact[1])
                    for fact, kind_mask in fact_masks.items()
                ],
            )
        )
    return result
