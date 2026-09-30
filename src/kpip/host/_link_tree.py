"""Compiled directory and hard-link loops for :func:`kpip.host.clone.clone_path`.

Cython pure-Python-mode source: this file only works compiled, and ``clone``
runs the same loops in Python when the extension is absent (PyPy, a source
checkout, a platform without a compiler). Windows builds leave it out and
keep the per-file walk.

A warm install hard links every file of every cached wheel tree into place:
12,000 files for jupyter. ``clone`` lists a tree with ``scandir`` and hands
the names over; the directories are made and the files linked here in C, with
the GIL released for the whole loop, so the slices ``clone`` splits a large
tree into link side by side instead of taking turns in the interpreter. Any
failure stops the loop and returns its errno and position, and ``clone``
handles that one entry by its own rules -- a copy, a verdict on the device
pair, or the exception -- then resumes.
"""

lazy import cython

if not cython.compiled:
    raise ImportError("kpip.host._link_tree only works compiled")

lazy from cython.cimports.libc.errno import ENAMETOOLONG, errno
lazy from cython.cimports.libc.stdlib import free, malloc
lazy from cython.cimports.libc.string import memcpy
lazy from cython.cimports.posix.stat import chmod, mkdir
lazy from cython.cimports.posix.unistd import link

_PATH_CAPACITY = cython.declare(cython.Py_ssize_t, 4096)
_SEPARATOR = cython.declare(cython.char, 47)  # "/"


@cython.cfunc
@cython.nogil
@cython.exceptval(check=False)
def _join(
    buffer: cython.p_char,
    root_length: cython.Py_ssize_t,
    name: cython.p_const_char,
    name_length: cython.Py_ssize_t,
) -> cython.bint:
    """Write ``name`` after the root already in ``buffer``; False if too long."""
    if root_length + 1 + name_length >= _PATH_CAPACITY:
        return False
    buffer[root_length] = _SEPARATOR
    memcpy(buffer + root_length + 1, name, name_length)
    buffer[root_length + 1 + name_length] = 0
    return True


def make_directories(root: bytes, names: list, modes: list) -> tuple:
    """``mkdir`` each of ``names`` under ``root`` with its mode from ``modes``.

    Parents come before their children. Returns ``(0, len(names))``, or the
    errno and index of the first directory that failed.
    """
    return _each_directory(root, names, modes, False)


def change_modes(root: bytes, names: list, modes: list) -> tuple:
    """``chmod`` each of ``names`` under ``root`` to its mode from ``modes``.

    Returns ``(0, len(names))``, or the errno and index of the first failure.
    """
    return _each_directory(root, names, modes, True)


@cython.cfunc
def _each_directory(
    root: bytes, names: list, modes: list, change: cython.bint
) -> tuple:
    count: cython.Py_ssize_t = len(names)
    if count == 0:
        return (0, 0)
    pointers: cython.pp_const_char = cython.cast(
        cython.pp_const_char, malloc(count * cython.sizeof(cython.p_const_char))
    )
    lengths: cython.p_Py_ssize_t = cython.cast(
        cython.p_Py_ssize_t, malloc(count * cython.sizeof(cython.Py_ssize_t))
    )
    permissions: cython.p_uint = cython.cast(
        cython.p_uint, malloc(count * cython.sizeof(cython.uint))
    )
    buffer: cython.p_char = cython.cast(cython.p_char, malloc(_PATH_CAPACITY))
    root_length: cython.Py_ssize_t = len(root)
    index: cython.Py_ssize_t
    error: cython.int = 0
    name: bytes
    try:
        if (
            pointers == cython.NULL
            or lengths == cython.NULL
            or permissions == cython.NULL
            or buffer == cython.NULL
        ):
            raise MemoryError
        if root_length + 1 >= _PATH_CAPACITY:
            return (ENAMETOOLONG, 0)
        for index in range(count):
            name = names[index]
            pointers[index] = name
            lengths[index] = len(name)
            permissions[index] = modes[index]
        memcpy(buffer, cython.cast(cython.p_const_char, root), root_length)
        with cython.nogil:
            for index in range(count):
                if not _join(buffer, root_length, pointers[index], lengths[index]):
                    error = ENAMETOOLONG
                    break
                if (
                    chmod(buffer, permissions[index])
                    if change
                    else mkdir(buffer, permissions[index])
                ) != 0:
                    error = errno
                    break
        return (error, index if error else count)
    finally:
        free(pointers)
        free(lengths)
        free(permissions)
        free(buffer)


def link_files(
    source_root: bytes,
    destination_root: bytes,
    names: list,
    start: cython.Py_ssize_t,
) -> tuple:
    """Hard link ``names[start:]`` from under ``source_root`` to ``destination_root``.

    Returns ``(0, len(names))``, or the errno and index of the first file that
    failed; the ones before it are linked.
    """
    count: cython.Py_ssize_t = len(names)
    if start >= count:
        return (0, count)
    pointers: cython.pp_const_char = cython.cast(
        cython.pp_const_char, malloc(count * cython.sizeof(cython.p_const_char))
    )
    lengths: cython.p_Py_ssize_t = cython.cast(
        cython.p_Py_ssize_t, malloc(count * cython.sizeof(cython.Py_ssize_t))
    )
    source: cython.p_char = cython.cast(cython.p_char, malloc(_PATH_CAPACITY))
    destination: cython.p_char = cython.cast(cython.p_char, malloc(_PATH_CAPACITY))
    source_length: cython.Py_ssize_t = len(source_root)
    destination_length: cython.Py_ssize_t = len(destination_root)
    index: cython.Py_ssize_t
    error: cython.int = 0
    name: bytes
    try:
        if (
            pointers == cython.NULL
            or lengths == cython.NULL
            or source == cython.NULL
            or destination == cython.NULL
        ):
            raise MemoryError
        if (
            source_length + 1 >= _PATH_CAPACITY
            or destination_length + 1 >= _PATH_CAPACITY
        ):
            return (ENAMETOOLONG, start)
        for index in range(start, count):
            name = names[index]
            pointers[index] = name
            lengths[index] = len(name)
        memcpy(source, cython.cast(cython.p_const_char, source_root), source_length)
        memcpy(
            destination,
            cython.cast(cython.p_const_char, destination_root),
            destination_length,
        )
        with cython.nogil:
            for index in range(start, count):
                if not _join(
                    source, source_length, pointers[index], lengths[index]
                ) or not _join(
                    destination, destination_length, pointers[index], lengths[index]
                ):
                    error = ENAMETOOLONG
                    break
                if link(source, destination) != 0:
                    error = errno
                    break
        return (error, index if error else count)
    finally:
        free(pointers)
        free(lengths)
        free(source)
        free(destination)
