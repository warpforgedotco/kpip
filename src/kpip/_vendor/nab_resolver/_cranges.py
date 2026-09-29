"""Compiled implementation of :class:`nab_resolver.ranges.Range`.

Cython pure-Python-mode source: this file only works compiled, and
``ranges`` falls back to its own pure-Python class when the extension is
absent (PyPy, a source checkout, a platform without a wheel).

Same semantics as the pure-Python class, with the intervals held in a C array
instead of a tuple of tuples.  Infinite bounds are NULL pointers.  A bound
whose type compares exactly as ``bytes`` (kpip's ``Version`` does) caches its
buffer, its length and a big-endian 8-byte prefix, so most comparisons are one
integer compare and the rest one ``memcmp``, without touching the interpreter.
Any other bound type goes through its own rich comparison, so the generic
contract (ints, floats, ...) still holds.

The interval tuple the pure class stores is built only when something asks
for ``_intervals`` (repr, pickling, tests).
"""

import cython

if not cython.compiled:
    raise ImportError("nab_resolver._cranges only works compiled")

from types import GenericAlias

from cython.cimports.cpython.bytes import (
    PyBytes_AS_STRING,
    PyBytes_Check,
    PyBytes_GET_SIZE,
)
from cython.cimports.cpython.mem import PyMem_Free, PyMem_Malloc, PyMem_Realloc
from cython.cimports.cpython.object import (
    Py_EQ,
    Py_LT,
    Py_TYPE,
    PyObject,
    PyObject_Hash,
    PyObject_RichCompareBool,
    richcmpfunc,
)
from cython.cimports.cpython.ref import Py_INCREF, Py_XDECREF
from cython.cimports.libc.stdint import uint64_t
from cython.cimports.libc.string import memcmp

PyObjectPtr = cython.typedef(cython.pointer(PyObject))

Bd = cython.struct(
    v=PyObjectPtr,  # NULL: the infinity for this slot (-inf low, +inf high)
    s=cython.p_char,  # the bytes' buffer, when ``fast``
    n=cython.Py_ssize_t,  # its length, when ``fast``
    p=uint64_t,  # its first eight bytes, big-endian and zero-padded
    fast=cython.int,  # compares exactly as bytes
)

Iv = cython.struct(lo=Bd, hi=Bd, lo_inc=cython.int, hi_inc=cython.int)

Buf = cython.struct(data=cython.pointer(Iv), n=cython.Py_ssize_t, cap=cython.Py_ssize_t)

BYTES_RICHCMP = cython.declare(richcmpfunc, Py_TYPE(b"").tp_richcompare)

# Installed by ranges.py once its sentinels and RangeRelation exist.
NEG = cython.declare(object, None)
POS = cython.declare(object, None)
NEG_TYPE = cython.declare(type, None)
POS_TYPE = cython.declare(type, None)
REL_EMPTY = cython.declare(object, None)
REL_SUBSET = cython.declare(object, None)
REL_DISJOINT = cython.declare(object, None)
REL_OVERLAPPING = cython.declare(object, None)

NO_BOUND = cython.declare(Bd)  # zero-initialised: an infinity


def _install(neg, pos, empty, subset, disjoint, overlapping):
    global NEG, POS, NEG_TYPE, POS_TYPE
    global REL_EMPTY, REL_SUBSET, REL_DISJOINT, REL_OVERLAPPING
    NEG = neg
    POS = pos
    NEG_TYPE = type(neg)
    POS_TYPE = type(pos)
    REL_EMPTY = empty
    REL_SUBSET = subset
    REL_DISJOINT = disjoint
    REL_OVERLAPPING = overlapping


# -- bounds -------------------------------------------------------------------


@cython.cfunc
@cython.inline
@cython.exceptval(check=False)
def bytes_prefix(s: cython.p_char, n: cython.Py_ssize_t) -> uint64_t:
    """The first eight bytes as a big-endian integer, zero-padded."""
    u: cython.p_uchar = cython.cast(cython.p_uchar, s)
    i: cython.Py_ssize_t
    p: uint64_t = 0
    if n >= 8:
        # Written as shifts so the compiler emits one load and a byte swap.
        return (
            (cython.cast(uint64_t, u[0]) << 56)
            | (cython.cast(uint64_t, u[1]) << 48)
            | (cython.cast(uint64_t, u[2]) << 40)
            | (cython.cast(uint64_t, u[3]) << 32)
            | (cython.cast(uint64_t, u[4]) << 24)
            | (cython.cast(uint64_t, u[5]) << 16)
            | (cython.cast(uint64_t, u[6]) << 8)
            | cython.cast(uint64_t, u[7])
        )
    for i in range(8):
        p <<= 8
        if i < n:
            p |= u[i]
    return p


@cython.cfunc
@cython.inline
@cython.exceptval(check=False)
def bd_set(b: cython.pointer(Bd), v: object) -> cython.void:
    """Point ``b`` at ``v`` (borrowed); ``None`` here never means infinity."""
    b.v = cython.cast(PyObjectPtr, v)
    b.fast = 0
    b.p = 0
    b.s = cython.NULL
    b.n = 0
    if PyBytes_Check(v) and Py_TYPE(v).tp_richcompare == BYTES_RICHCMP:
        b.fast = 1
        b.s = PyBytes_AS_STRING(v)
        b.n = PyBytes_GET_SIZE(v)
        b.p = bytes_prefix(b.s, b.n)


@cython.cfunc
@cython.inline
@cython.exceptval(check=False)
def bd_bound(b: cython.pointer(Bd), v: object, infinity: type) -> cython.void:
    """``bd_set``, with an instance of ``infinity`` meaning NULL."""
    if type(v) is infinity:
        b[0] = NO_BOUND
    else:
        bd_set(b, v)


@cython.cfunc
@cython.inline
@cython.exceptval(-2, check=True)
def vcmp(a: cython.pointer(Bd), b: cython.pointer(Bd)) -> cython.int:
    """-1, 0 or 1 for two finite bounds."""
    r: cython.int
    shortest: cython.Py_ssize_t
    if a.v == b.v:
        return 0
    if a.fast and b.fast:
        if a.p != b.p:
            return -1 if a.p < b.p else 1
        if a.n > 8 and b.n > 8:
            shortest = a.n if a.n < b.n else b.n
            r = memcmp(a.s + 8, b.s + 8, shortest - 8)
            if r:
                return -1 if r < 0 else 1
        return (a.n > b.n) - (a.n < b.n)
    left: object = cython.cast(object, a.v)
    right: object = cython.cast(object, b.v)
    if PyObject_RichCompareBool(left, right, Py_LT):
        return -1
    if PyObject_RichCompareBool(left, right, Py_EQ):
        return 0
    return 1


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def veq(a: cython.pointer(Bd), b: cython.pointer(Bd)) -> cython.int:
    """Equality the way a tuple compares its items: identity, then ``==``."""
    if a.v == b.v:
        return 1
    if a.v == cython.NULL or b.v == cython.NULL:
        return 0
    if a.fast and b.fast:
        return a.p == b.p and vcmp(a, b) == 0
    return PyObject_RichCompareBool(
        cython.cast(object, a.v), cython.cast(object, b.v), Py_EQ
    )


# -- interval buffers ---------------------------------------------------------


@cython.cfunc
@cython.exceptval(-1, check=True)
def buf_init(buf: cython.pointer(Buf), cap: cython.Py_ssize_t) -> cython.int:
    if cap < 4:
        cap = 4
    buf.data = cython.cast(cython.pointer(Iv), PyMem_Malloc(cap * cython.sizeof(Iv)))
    if buf.data == cython.NULL:
        raise MemoryError()
    buf.n = 0
    buf.cap = cap
    return 0


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def buf_push(
    buf: cython.pointer(Buf),
    lo: cython.pointer(Bd),
    lo_inc: cython.int,
    hi: cython.pointer(Bd),
    hi_inc: cython.int,
) -> cython.int:
    grown: cython.pointer(Iv)
    slot: cython.pointer(Iv)
    if buf.n == buf.cap:
        grown = cython.cast(
            cython.pointer(Iv),
            PyMem_Realloc(buf.data, 2 * buf.cap * cython.sizeof(Iv)),
        )
        if grown == cython.NULL:
            raise MemoryError()
        buf.data = grown
        buf.cap *= 2
    slot = cython.address(buf.data[buf.n])
    slot.lo = lo[0]
    slot.hi = hi[0]
    slot.lo_inc = lo_inc
    slot.hi_inc = hi_inc
    buf.n += 1
    return 0


@cython.cfunc
@cython.inline
@cython.exceptval(check=False)
def buf_free(buf: cython.pointer(Buf)) -> cython.void:
    if buf.data != cython.NULL:
        PyMem_Free(buf.data)
        buf.data = cython.NULL


@cython.cfunc
def adopt(cls: type, buf: cython.pointer(Buf)) -> Range:
    """A new ``cls`` owning ``buf``'s intervals, with references to the bounds.

    Takes ``buf.data``; the caller frees it only if this raises first.
    """
    result: Range = cls.__new__(cls)
    i: cython.Py_ssize_t
    data: cython.pointer(Iv)
    if buf.n == 0:
        buf_free(buf)
        return result
    data = buf.data
    if buf.cap != buf.n:
        data = cython.cast(
            cython.pointer(Iv), PyMem_Realloc(buf.data, buf.n * cython.sizeof(Iv))
        )
        if data == cython.NULL:
            data = buf.data
    buf.data = cython.NULL
    for i in range(buf.n):
        if data[i].lo.v != cython.NULL:
            Py_INCREF(cython.cast(object, data[i].lo.v))
        if data[i].hi.v != cython.NULL:
            Py_INCREF(cython.cast(object, data[i].hi.v))
    result.iv = data
    result.n = buf.n
    return result


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def is_empty_iv(
    lo: cython.pointer(Bd),
    lo_inc: cython.int,
    hi: cython.pointer(Bd),
    hi_inc: cython.int,
) -> cython.int:
    c: cython.int
    if lo.v == cython.NULL or hi.v == cython.NULL:
        return 0
    c = vcmp(lo, hi)
    return c > 0 or (c == 0 and not (lo_inc and hi_inc))


# -- the class ----------------------------------------------------------------


@cython.cclass
class Range:
    """A set of versions represented as a canonical list of intervals.

    See the pure-Python ``nab_resolver.ranges`` for the invariant; this class
    keeps it the same way and neither checks nor normalizes its input.
    """

    iv = cython.declare(cython.pointer(Iv))
    n = cython.declare(cython.Py_ssize_t)
    _hash = cython.declare(cython.Py_hash_t)
    _tuple = cython.declare(object)

    def __cinit__(self):
        self.iv = cython.NULL
        self.n = 0
        self._hash = 0
        self._tuple = None

    def __dealloc__(self):
        i: cython.Py_ssize_t
        if self.iv != cython.NULL:
            for i in range(self.n):
                Py_XDECREF(self.iv[i].lo.v)
                Py_XDECREF(self.iv[i].hi.v)
            PyMem_Free(self.iv)
            self.iv = cython.NULL

    def __init__(self, intervals=()):
        buf: Buf
        lo: Bd
        hi: Bd
        i: cython.Py_ssize_t
        if self.iv != cython.NULL:
            raise TypeError("Range is immutable")
        intervals = tuple(intervals)
        if not intervals:
            return
        buf_init(cython.address(buf), len(intervals))
        try:
            for item in intervals:
                lower, lower_inclusive, upper, upper_inclusive = item
                # By type, not identity: a pickled range brings back copies
                # of the sentinels.
                if type(lower) is POS_TYPE or type(upper) is NEG_TYPE:
                    raise ValueError(f"interval {item!r} breaks the Range invariant")
                bd_bound(cython.address(lo), lower, NEG_TYPE)
                bd_bound(cython.address(hi), upper, POS_TYPE)
                buf_push(
                    cython.address(buf),
                    cython.address(lo),
                    bool(lower_inclusive),
                    cython.address(hi),
                    bool(upper_inclusive),
                )
            for i in range(buf.n):
                if buf.data[i].lo.v != cython.NULL:
                    Py_INCREF(cython.cast(object, buf.data[i].lo.v))
                if buf.data[i].hi.v != cython.NULL:
                    Py_INCREF(cython.cast(object, buf.data[i].hi.v))
            self.iv = buf.data
            self.n = buf.n
            buf.data = cython.NULL
        finally:
            buf_free(cython.address(buf))

    __class_getitem__ = classmethod(GenericAlias)

    # -- views ----------------------------------------------------------------

    @property
    def _intervals(self):
        i: cython.Py_ssize_t
        out: list
        if self._tuple is None:
            out = []
            for i in range(self.n):
                out.append(
                    (
                        NEG
                        if self.iv[i].lo.v == cython.NULL
                        else cython.cast(object, self.iv[i].lo.v),
                        bool(self.iv[i].lo_inc),
                        POS
                        if self.iv[i].hi.v == cython.NULL
                        else cython.cast(object, self.iv[i].hi.v),
                        bool(self.iv[i].hi_inc),
                    )
                )
            self._tuple = tuple(out)
        return self._tuple

    def _as_points(self):
        i: cython.Py_ssize_t
        values: list = []
        for i in range(self.n):
            if (
                self.iv[i].lo.v == cython.NULL
                or self.iv[i].hi.v == cython.NULL
                or not self.iv[i].lo_inc
                or not self.iv[i].hi_inc
                or not veq(cython.address(self.iv[i].lo), cython.address(self.iv[i].hi))
            ):
                return None
            values.append(cython.cast(object, self.iv[i].lo.v))
        return frozenset(values)

    # -- constructors -----------------------------------------------------------

    @classmethod
    def empty(cls):
        return cls.__new__(cls)

    @classmethod
    def full(cls):
        return one_interval(cls, None, 0, None, 0)

    @classmethod
    def singleton(cls, version):
        return one_interval(cls, version, 1, version, 1)

    @classmethod
    def from_versions(cls, versions):
        buf: Buf
        b: Bd
        i: cython.Py_ssize_t
        count: cython.Py_ssize_t
        values: list = versions if type(versions) is list else list(versions)
        count = len(values)
        buf_init(cython.address(buf), count)
        try:
            # Callers nearly always pass distinct ascending versions; push
            # while confirming that, and sort only if it turns out otherwise.
            for i in range(count):
                bd_set(cython.address(b), values[i])
                if buf.n and vcmp(cython.address(buf.data[buf.n - 1].lo), cython.address(b)) >= 0:
                    break
                buf_push(cython.address(buf), cython.address(b), 1, cython.address(b), 1)
            else:
                return adopt(cls, cython.address(buf))
            buf.n = 0
            values = sorted(frozenset(values))
            for i in range(len(values)):
                bd_set(cython.address(b), values[i])
                buf_push(cython.address(buf), cython.address(b), 1, cython.address(b), 1)
            # ``values`` keeps the bounds alive until adopt takes references.
            return adopt(cls, cython.address(buf))
        finally:
            buf_free(cython.address(buf))

    @classmethod
    def at_least(cls, version):
        return one_interval(cls, version, 1, None, 0)

    @classmethod
    def greater_than(cls, version):
        return one_interval(cls, version, 0, None, 0)

    @classmethod
    def at_most(cls, version):
        return one_interval(cls, None, 0, version, 1)

    @classmethod
    def less_than(cls, version):
        return one_interval(cls, None, 0, version, 0)

    @classmethod
    def between(cls, lower, upper, *, lower_inclusive=True, upper_inclusive=False):
        lo: Bd
        hi: Bd
        li: cython.int = bool(lower_inclusive)
        ui: cython.int = bool(upper_inclusive)
        bd_bound(cython.address(lo), lower, NEG_TYPE)
        bd_bound(cython.address(hi), upper, POS_TYPE)
        if is_empty_iv(cython.address(lo), li, cython.address(hi), ui):
            return cls.__new__(cls)
        return one_interval(
            cls,
            None if lo.v == cython.NULL else lower,
            li,
            None if hi.v == cython.NULL else upper,
            ui,
        )

    # -- queries --------------------------------------------------------------

    @property
    def is_empty(self):
        return self.n == 0

    def __bool__(self):
        return self.n != 0

    def __contains__(self, version):
        v: Bd
        low: cython.Py_ssize_t
        high: cython.Py_ssize_t
        middle: cython.Py_ssize_t
        found: cython.pointer(Iv)
        c: cython.int
        if self.n == 0:
            return False
        bd_set(cython.address(v), version)
        if self.n == 1:
            found = cython.address(self.iv[0])
            if found.lo.v != cython.NULL:
                c = vcmp(cython.address(v), cython.address(found.lo))
                if c < 0 or (c == 0 and not found.lo_inc):
                    return False
        else:
            low = 0
            high = self.n
            while low < high:
                middle = (low + high) >> 1
                if (
                    self.iv[middle].lo.v == cython.NULL
                    or vcmp(cython.address(v), cython.address(self.iv[middle].lo)) >= 0
                ):
                    low = middle + 1
                else:
                    high = middle
            if low == 0:
                return False
            found = cython.address(self.iv[low - 1])
            if found.lo.v != cython.NULL and vcmp(cython.address(v), cython.address(found.lo)) == 0:
                return bool(found.lo_inc)
        if found.hi.v == cython.NULL:
            return True
        c = vcmp(cython.address(v), cython.address(found.hi))
        return not (c > 0 or (c == 0 and not found.hi_inc))

    def select_sorted(self, ordered):
        selected: list = []
        seq: list = ordered if type(ordered) is list else list(ordered)
        count: cython.Py_ssize_t = len(seq)
        i: cython.Py_ssize_t
        start: cython.Py_ssize_t
        stop: cython.Py_ssize_t
        lo_i: cython.Py_ssize_t
        hi_i: cython.Py_ssize_t
        mid: cython.Py_ssize_t
        probe: Bd
        cur: cython.pointer(Iv)
        c: cython.int
        for i in range(self.n):
            cur = cython.address(self.iv[i])
            if cur.lo.v == cython.NULL:
                start = 0
            else:
                # bisect_left for an inclusive bound, bisect_right otherwise
                lo_i = 0
                hi_i = count
                while lo_i < hi_i:
                    mid = (lo_i + hi_i) >> 1
                    bd_set(cython.address(probe), seq[mid])
                    c = vcmp(cython.address(probe), cython.address(cur.lo))
                    if c < 0 or (c == 0 and not cur.lo_inc):
                        lo_i = mid + 1
                    else:
                        hi_i = mid
                start = lo_i
            if cur.hi.v == cython.NULL:
                stop = count
            else:
                lo_i = start
                hi_i = count
                while lo_i < hi_i:
                    mid = (lo_i + hi_i) >> 1
                    bd_set(cython.address(probe), seq[mid])
                    c = vcmp(cython.address(probe), cython.address(cur.hi))
                    if c < 0 or (c == 0 and cur.hi_inc):
                        lo_i = mid + 1
                    else:
                        hi_i = mid
                stop = lo_i
            if start < stop:
                selected.extend(seq[start:stop])
        return selected

    # -- algebra --------------------------------------------------------------

    def __and__(self, other):
        if not isinstance(self, Range) or not isinstance(other, Range):
            return NotImplemented
        return intersect(self, other)

    def __or__(self, other):
        if not isinstance(self, Range) or not isinstance(other, Range):
            return NotImplemented
        return unite(self, other)

    def __invert__(self):
        return complement(self)

    def __sub__(self, other):
        if not isinstance(self, Range) or not isinstance(other, Range):
            return NotImplemented
        return difference(self, other)

    def is_subset(self, other: Range) -> bool:
        return is_subset(self, other)

    def is_superset(self, other):
        return other.is_subset(self)

    def is_disjoint(self, other: Range) -> bool:
        return is_disjoint(self, other)

    def relation(self, other: Range):
        return relation(self, other)

    # -- identity -------------------------------------------------------------

    def __eq__(self, other):
        right: Range
        i: cython.Py_ssize_t
        if not isinstance(other, Range):
            return NotImplemented
        right = other
        if right is self:
            return True
        if right.n != self.n:
            return False
        for i in range(self.n):
            if (
                self.iv[i].lo_inc != right.iv[i].lo_inc
                or self.iv[i].hi_inc != right.iv[i].hi_inc
                or not veq(cython.address(self.iv[i].lo), cython.address(right.iv[i].lo))
                or not veq(cython.address(self.iv[i].hi), cython.address(right.iv[i].hi))
            ):
                return False
        return True

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        h: cython.Py_hash_t = self._hash
        i: cython.Py_ssize_t
        item: cython.Py_hash_t
        if h == 0:
            h = 0x345678
            for i in range(self.n):
                if self.iv[i].lo.v == cython.NULL:
                    item = 0x2B
                else:
                    item = PyObject_Hash(cython.cast(object, self.iv[i].lo.v))
                h = (h ^ item) * 1000003
                if self.iv[i].hi.v == cython.NULL:
                    item = 0x3D
                else:
                    item = PyObject_Hash(cython.cast(object, self.iv[i].hi.v))
                h = (h ^ item) * 1000003
                h ^= (self.iv[i].lo_inc << 1) | self.iv[i].hi_inc
            if h == 0 or h == -1:
                h = 1
            self._hash = h
        return h

    def __reduce__(self):
        return (type(self), (self._intervals,))

    def __repr__(self):
        return f"Range({self._intervals!r})"

    def __str__(self):
        if self.n == 0:
            return "<empty>"
        if self.n == 1 and self.iv[0].lo.v == cython.NULL and self.iv[0].hi.v == cython.NULL:
            return "*"
        parts = []
        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            if lower == upper and lower_inclusive and upper_inclusive:
                parts.append(str(lower))
            else:
                left_bracket = "[" if lower_inclusive else "("
                right_bracket = "]" if upper_inclusive else ")"
                parts.append(f"{left_bracket}{lower}, {upper}{right_bracket}")
        return " | ".join(parts)


# -- the algebra, over the C arrays -------------------------------------------


@cython.cfunc
def one_interval(
    cls: type, lower: object, lo_inc: cython.int, upper: object, hi_inc: cython.int
) -> Range:
    """``cls`` holding one interval; ``None`` means the infinity."""
    buf: Buf
    lo: Bd = NO_BOUND
    hi: Bd = NO_BOUND
    if lower is not None:
        bd_set(cython.address(lo), lower)
    if upper is not None:
        bd_set(cython.address(hi), upper)
    buf_init(cython.address(buf), 1)
    try:
        buf_push(cython.address(buf), cython.address(lo), lo_inc, cython.address(hi), hi_inc)
        return adopt(cls, cython.address(buf))
    finally:
        buf_free(cython.address(buf))


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def max_lower(a: cython.pointer(Iv), b: cython.pointer(Iv), out: cython.pointer(Bd)) -> cython.int:
    """Set ``out`` to the higher lower bound; return its inclusivity."""
    c: cython.int
    if a.lo.v == cython.NULL and b.lo.v == cython.NULL:
        out[0] = a.lo
        return a.lo_inc and b.lo_inc
    if a.lo.v == cython.NULL:
        out[0] = b.lo
        return b.lo_inc
    if b.lo.v == cython.NULL:
        out[0] = a.lo
        return a.lo_inc
    c = vcmp(cython.address(a.lo), cython.address(b.lo))
    if c == 0:
        out[0] = a.lo
        return a.lo_inc and b.lo_inc
    if c < 0:
        out[0] = b.lo
        return b.lo_inc
    out[0] = a.lo
    return a.lo_inc


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def min_upper(a: cython.pointer(Iv), b: cython.pointer(Iv), out: cython.pointer(Bd)) -> cython.int:
    """Set ``out`` to the lower upper bound; return its inclusivity."""
    c: cython.int
    if a.hi.v == cython.NULL and b.hi.v == cython.NULL:
        out[0] = a.hi
        return a.hi_inc and b.hi_inc
    if a.hi.v == cython.NULL:
        out[0] = b.hi
        return b.hi_inc
    if b.hi.v == cython.NULL:
        out[0] = a.hi
        return a.hi_inc
    c = vcmp(cython.address(a.hi), cython.address(b.hi))
    if c == 0:
        out[0] = a.hi
        return a.hi_inc and b.hi_inc
    if c > 0:
        out[0] = b.hi
        return b.hi_inc
    out[0] = a.hi
    return a.hi_inc


@cython.cfunc
@cython.inline
@cython.exceptval(-2, check=True)
def upper_cmp(a: cython.pointer(Bd), b: cython.pointer(Bd)) -> cython.int:
    """Compare two upper bounds, +inf (NULL) highest."""
    if a.v == cython.NULL:
        return 0 if b.v == cython.NULL else 1
    if b.v == cython.NULL:
        return -1
    return vcmp(a, b)


@cython.cfunc
def intersect(left: Range, right: Range) -> Range:
    buf: Buf
    li: cython.Py_ssize_t = 0
    ri: cython.Py_ssize_t = 0
    lo: Bd
    hi: Bd
    lo_inc: cython.int
    hi_inc: cython.int
    c: cython.int
    if left.n == 0 or right.n == 0:
        return Range.__new__(Range)
    buf_init(cython.address(buf), left.n + right.n)
    try:
        while li < left.n and ri < right.n:
            lo_inc = max_lower(cython.address(left.iv[li]), cython.address(right.iv[ri]), cython.address(lo))
            hi_inc = min_upper(cython.address(left.iv[li]), cython.address(right.iv[ri]), cython.address(hi))
            if not is_empty_iv(cython.address(lo), lo_inc, cython.address(hi), hi_inc):
                buf_push(cython.address(buf), cython.address(lo), lo_inc, cython.address(hi), hi_inc)
            c = upper_cmp(cython.address(left.iv[li].hi), cython.address(right.iv[ri].hi))
            if c == 0:
                li += 1
                ri += 1
            elif c > 0:
                ri += 1
            else:
                li += 1
        return adopt(Range, cython.address(buf))
    finally:
        buf_free(cython.address(buf))


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def lower_key_le(a: cython.pointer(Iv), b: cython.pointer(Iv)) -> cython.int:
    """Whether ``_interval_sort_key(a) <= _interval_sort_key(b)``."""
    c: cython.int
    if a.lo.v == cython.NULL:
        return 1
    if b.lo.v == cython.NULL:
        return 0
    c = vcmp(cython.address(a.lo), cython.address(b.lo))
    if c != 0:
        return c < 0
    # An inclusive bound sorts first.
    return (0 if a.lo_inc else 1) <= (0 if b.lo_inc else 1)


@cython.cfunc
@cython.exceptval(-1, check=True)
def merge_into(buf: cython.pointer(Buf), item: cython.pointer(Iv)) -> cython.int:
    last: cython.pointer(Iv)
    c: cython.int
    overlap: cython.int
    if buf.n == 0:
        return buf_push(buf, cython.address(item.lo), item.lo_inc, cython.address(item.hi), item.hi_inc)
    last = cython.address(buf.data[buf.n - 1])
    if last.hi.v == cython.NULL or item.lo.v == cython.NULL:
        overlap = 1
    else:
        c = vcmp(cython.address(last.hi), cython.address(item.lo))
        overlap = c > 0 or (c == 0 and (last.hi_inc or item.lo_inc))
    if not overlap:
        return buf_push(buf, cython.address(item.lo), item.lo_inc, cython.address(item.hi), item.hi_inc)
    if last.hi.v == cython.NULL or item.hi.v == cython.NULL:
        last.hi = NO_BOUND
        last.hi_inc = 0
        return 0
    c = vcmp(cython.address(last.hi), cython.address(item.hi))
    if c > 0:
        return 0
    if c == 0:
        last.hi_inc = last.hi_inc or item.hi_inc
        return 0
    last.hi = item.hi
    last.hi_inc = item.hi_inc
    return 0


@cython.cfunc
def unite(left: Range, right: Range) -> Range:
    buf: Buf
    li: cython.Py_ssize_t = 0
    ri: cython.Py_ssize_t = 0
    if left.n == 0:
        return right
    if right.n == 0:
        return left
    buf_init(cython.address(buf), left.n + right.n)
    try:
        # A stable merge of two sorted lists, left first on ties, is the
        # order the pure implementation's stable sort produces.
        while li < left.n and ri < right.n:
            if lower_key_le(cython.address(left.iv[li]), cython.address(right.iv[ri])):
                merge_into(cython.address(buf), cython.address(left.iv[li]))
                li += 1
            else:
                merge_into(cython.address(buf), cython.address(right.iv[ri]))
                ri += 1
        while li < left.n:
            merge_into(cython.address(buf), cython.address(left.iv[li]))
            li += 1
        while ri < right.n:
            merge_into(cython.address(buf), cython.address(right.iv[ri]))
            ri += 1
        return adopt(Range, cython.address(buf))
    finally:
        buf_free(cython.address(buf))


@cython.cfunc
def complement(self: Range) -> Range:
    buf: Buf
    i: cython.Py_ssize_t
    prev: Bd = NO_BOUND  # -inf before the first interval
    prev_inc: cython.int = 0
    prev_is_pos: cython.int = 0
    gap_lo_inc: cython.int
    gap_hi_inc: cython.int
    c: cython.int
    keep: cython.int
    cur: cython.pointer(Iv)
    if self.n == 0:
        return Range.full()
    buf_init(cython.address(buf), self.n + 1)
    try:
        for i in range(self.n):
            cur = cython.address(self.iv[i])
            if prev.v != cython.NULL or cur.lo.v != cython.NULL:
                gap_lo_inc = (not prev_inc) and prev.v != cython.NULL
                gap_hi_inc = not cur.lo_inc
                if cur.lo.v == cython.NULL:
                    keep = 0
                elif prev.v == cython.NULL:
                    keep = 1
                else:
                    c = vcmp(cython.address(prev), cython.address(cur.lo))
                    keep = c < 0 or (c == 0 and gap_lo_inc and gap_hi_inc)
                if keep:
                    buf_push(cython.address(buf), cython.address(prev), gap_lo_inc, cython.address(cur.lo), gap_hi_inc)
            prev = cur.hi
            prev_inc = cur.hi_inc
            prev_is_pos = cur.hi.v == cython.NULL
        if not prev_is_pos:
            buf_push(cython.address(buf), cython.address(prev), not prev_inc, cython.address(NO_BOUND), 0)
        return adopt(Range, cython.address(buf))
    finally:
        buf_free(cython.address(buf))


@cython.cfunc
def difference(left: Range, right: Range) -> Range:
    buf: Buf
    ri: cython.Py_ssize_t = 0
    scan: cython.Py_ssize_t
    i: cython.Py_ssize_t
    lo: Bd
    hi: Bd
    lo_inc: cython.int
    hi_inc: cython.int
    c: cython.int
    exhausted: cython.int
    below: cython.int
    r: cython.pointer(Iv)
    if left.n == 0 or right.n == 0:
        return left
    buf_init(cython.address(buf), left.n + right.n)
    try:
        for i in range(left.n):
            lo = left.iv[i].lo
            lo_inc = left.iv[i].lo_inc
            hi = left.iv[i].hi
            hi_inc = left.iv[i].hi_inc

            # A right interval below this one is below every later one too.
            while ri < right.n:
                r = cython.address(right.iv[ri])
                if r.hi.v == cython.NULL or lo.v == cython.NULL:
                    break
                c = vcmp(cython.address(r.hi), cython.address(lo))
                if c == 0:
                    if r.hi_inc and lo_inc:
                        break
                elif c > 0:
                    break
                ri += 1

            exhausted = 0
            scan = ri
            while scan < right.n:
                r = cython.address(right.iv[scan])
                if hi.v != cython.NULL and r.lo.v != cython.NULL:
                    c = vcmp(cython.address(hi), cython.address(r.lo))
                    if c == 0:
                        if not (hi_inc and r.lo_inc):
                            break
                    elif c < 0:
                        break

                # Whatever of the remainder sits below this right interval survives.
                if r.lo.v != cython.NULL:
                    if lo.v == cython.NULL:
                        below = 1
                    else:
                        c = vcmp(cython.address(lo), cython.address(r.lo))
                        below = not (c > 0 or (c == 0 and not (lo_inc and not r.lo_inc)))
                    if below:
                        buf_push(cython.address(buf), cython.address(lo), lo_inc, cython.address(r.lo), not r.lo_inc)

                if r.hi.v == cython.NULL:
                    exhausted = 1
                    break

                # Resume above this right interval, stopping when nothing of it is left.
                lo = r.hi
                lo_inc = not r.hi_inc
                if hi.v != cython.NULL:
                    c = vcmp(cython.address(lo), cython.address(hi))
                    if c > 0 or (c == 0 and not (lo_inc and hi_inc)):
                        exhausted = 1
                        break
                scan += 1

            if not exhausted and not is_empty_iv(cython.address(lo), lo_inc, cython.address(hi), hi_inc):
                buf_push(cython.address(buf), cython.address(lo), lo_inc, cython.address(hi), hi_inc)
        return adopt(Range, cython.address(buf))
    finally:
        buf_free(cython.address(buf))


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def skip_below(right: Range, index: cython.pointer(cython.Py_ssize_t), left: cython.pointer(Iv)) -> cython.int:
    """Advance ``index`` past right intervals entirely below ``left``."""
    r: cython.pointer(Iv)
    c: cython.int
    while index[0] < right.n:
        r = cython.address(right.iv[index[0]])
        if r.hi.v == cython.NULL or left.lo.v == cython.NULL:
            break
        c = vcmp(cython.address(r.hi), cython.address(left.lo))
        if c == 0:
            if r.hi_inc and left.lo_inc:
                break
        elif c > 0:
            break
        index[0] += 1
    return 0


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def covers(left: cython.pointer(Iv), right: cython.pointer(Iv)) -> cython.int:
    """Whether ``right`` holds every version of ``left``."""
    c: cython.int
    if left.lo.v == cython.NULL:
        if right.lo.v != cython.NULL:
            return 0
    elif right.lo.v != cython.NULL:
        c = vcmp(cython.address(left.lo), cython.address(right.lo))
        if c == 0:
            if left.lo_inc and not right.lo_inc:
                return 0
        elif c < 0:
            return 0
    if left.hi.v == cython.NULL:
        if right.hi.v != cython.NULL:
            return 0
    elif right.hi.v != cython.NULL:
        c = vcmp(cython.address(left.hi), cython.address(right.hi))
        if c == 0:
            if left.hi_inc and not right.hi_inc:
                return 0
        elif c > 0:
            return 0
    return 1


@cython.cfunc
def is_subset(left: Range, right: Range) -> cython.bint:
    i: cython.Py_ssize_t
    ri: cython.Py_ssize_t = 0
    for i in range(left.n):
        skip_below(right, cython.address(ri), cython.address(left.iv[i]))
        if ri >= right.n:
            return False
        if not covers(cython.address(left.iv[i]), cython.address(right.iv[ri])):
            return False
    return True


@cython.cfunc
def is_disjoint(left: Range, right: Range) -> cython.bint:
    li: cython.Py_ssize_t = 0
    ri: cython.Py_ssize_t = 0
    lo: Bd
    hi: Bd
    lo_inc: cython.int
    hi_inc: cython.int
    while li < left.n and ri < right.n:
        lo_inc = max_lower(cython.address(left.iv[li]), cython.address(right.iv[ri]), cython.address(lo))
        hi_inc = min_upper(cython.address(left.iv[li]), cython.address(right.iv[ri]), cython.address(hi))
        if not is_empty_iv(cython.address(lo), lo_inc, cython.address(hi), hi_inc):
            return False
        # Whichever interval ends first cannot meet anything further along.
        if left.iv[li].hi.v == cython.NULL:
            if right.iv[ri].hi.v == cython.NULL:
                li += 1
            else:
                ri += 1
        elif right.iv[ri].hi.v == cython.NULL:
            li += 1
        elif vcmp(cython.address(left.iv[li].hi), cython.address(right.iv[ri].hi)) <= 0:
            li += 1
        else:
            ri += 1
    return True


@cython.cfunc
@cython.inline
@cython.exceptval(-1, check=True)
def ends_before(left: cython.pointer(Iv), right: cython.pointer(Iv)) -> cython.int:
    c: cython.int
    if left.hi.v == cython.NULL or right.lo.v == cython.NULL:
        return 0
    c = vcmp(cython.address(left.hi), cython.address(right.lo))
    if c == 0:
        # They meet at a point, which they share only if both ends include it.
        return not (left.hi_inc and right.lo_inc)
    return c < 0


@cython.cfunc
def relation(left: Range, right: Range) -> object:
    i: cython.Py_ssize_t
    ri: cython.Py_ssize_t = 0
    subset: cython.int = 1
    disjoint: cython.int = 1
    if left.n == 0:
        return REL_EMPTY
    for i in range(left.n):
        skip_below(right, cython.address(ri), cython.address(left.iv[i]))
        if ri >= right.n:
            subset = 0
            if not disjoint:
                return REL_OVERLAPPING
            break
        if ends_before(cython.address(left.iv[i]), cython.address(right.iv[ri])):
            subset = 0
            if not disjoint:
                return REL_OVERLAPPING
            continue
        disjoint = 0
        if not covers(cython.address(left.iv[i]), cython.address(right.iv[ri])):
            return REL_OVERLAPPING
    if subset:
        return REL_SUBSET
    if disjoint:
        return REL_DISJOINT
    return REL_OVERLAPPING
