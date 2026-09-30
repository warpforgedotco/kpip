"""PEP 508 environment markers: a real parser, not a pair of string splits.

The grammar has grouping, so a marker cannot be evaluated by splitting on
``or`` and then on ``and``: ``(a or b) and c`` splits into ``(a`` and
``b) and c``, neither of which is an expression. Roughly one real marker in
forty on PyPI is parenthesised, and the split answers those wrong in both
directions -- dropping a dependency the environment needs, or admitting one
that an unrequested extra was guarding.

So: tokenize, then recursive descent over the grammar in the dependency
specifiers specification::

    marker      = marker_or
    marker_or   = marker_and ('or' marker_and)*
    marker_and  = marker_expr ('and' marker_expr)*
    marker_expr = '(' marker ')' | marker_var marker_op marker_var
    marker_var  = VARIABLE | QUOTED_STRING
    marker_op   = '<=' | '<' | '!=' | '==' | '>=' | '>' | '~=' | '==='
                | 'in' | 'not' 'in'

Two consequences of the grammar that the old evaluator did not have:

* either side may be the variable, so ``"3.9" <= python_version`` is a
  legal marker and means what it reads as;
* the variable set includes the dotted legacy spellings (``os.name``) and
  the aliases (``python_implementation``), which normalise to the canonical
  names rather than resolving to the empty string.

Comparison semantics follow :mod:`packaging` exactly, because that is what
pip evaluates markers with. The rules are less obvious than they look:

* only the four version-valued variables in :data:`VERSION_MARKERS` compare
  with PEP 440 semantics; everything else compares as text;
* for text comparison ``<`` and ``>`` are always false and ``<=`` and ``>=``
  mean equality -- ordering two arbitrary strings is meaningless, so the
  specification declines to define it rather than falling back to
  lexicographic order;
* ``in``/``not in`` are Python's containment on strings, so
  ``platform_machine in "x86_64 aarch64"`` is a substring test, not a lookup
  in a comma-separated list;
* extra names compare normalised, per PEP 685.

Parsed markers are interned by text: evaluation happens once per requirement
per candidate during a resolve, and the parse is the expensive half.
"""

from __future__ import annotations

import re

from kpip.core.caches import bounded_put, register_table
from kpip.core.names import canonicalize_name

# `packaging` reaches this module only from inside marker_applies(), so
# importing it here is not a cycle -- and keeping the import at module level
# means the version-comparison path does not re-resolve it per clause.
from kpip.core.packaging import Specifier
from kpip.core.versions import InvalidVersion, Version

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


class InvalidMarker(ValueError):
    """The text is not a marker."""


class UndefinedEnvironmentName(ValueError):
    """The marker names a variable the environment does not define."""


class UndefinedComparison(ValueError):
    """The marker compares values the operator has no meaning for."""


VERSION_MARKERS = frozenset(
    (
        "implementation_version",
        "platform_release",
        "python_full_version",
        "python_version",
    ),
)
"""Variables whose values are versions, and so compare with PEP 440 semantics."""

SET_MARKERS = frozenset(("extras", "dependency_groups"))
"""Variables whose values are sets of names, compared canonicalised."""

VARIABLE_ALIASES = {
    "python_implementation": "platform_python_implementation",
}
"""Spellings that are not the canonical name of the variable they select.

The dotted legacy forms (``os.name``, ``platform.machine``) are folded to
underscores before this table is consulted, as the reference parser does.
"""

_VARIABLE_NAMES = frozenset(
    (
        "python_version",
        "python_full_version",
        "os_name",
        "sys_platform",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform_machine",
        "platform_python_implementation",
        "python_implementation",
        "implementation_name",
        "implementation_version",
        "extra",
        "extras",
        "dependency_groups",
    ),
)

_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<OP>===|==|~=|!=|<=|>=|<|>)
    | (?P<STRING>'[^']*'|"[^"]*")
    | (?P<NAME>[A-Za-z_][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)

# Node shapes, chosen so a parsed marker is a plain hashable tuple tree:
#   ("or", (node, ...))     ("and", (node, ...))
#   ("cmp", left, op, right) where each side is ("var", name) or ("str", text)
_OR = "or"
_AND = "and"
_CMP = "cmp"
_VAR = "var"
_STR = "str"

_MARKERS_LIMIT = 4096
_markers: dict[str, tuple[Any, ...]] = register_table({})


def parse_marker(text: str) -> tuple[Any, ...]:
    """The parse tree for ``text``, interned.

    Raises :class:`InvalidMarker` if the text is not a marker. The tree is a
    tuple of tuples, so it is hashable and safe to share.
    """
    cached = _markers.get(text)
    if cached is not None:
        return cached
    parser = _Parser(text)
    tree = parser.parse_marker()
    parser.expect_end()
    bounded_put(_markers, text, tree, _MARKERS_LIMIT)
    return tree


class _Parser:
    """Recursive descent over the token stream, with the position as state."""

    __slots__ = ("_position", "_text", "_tokens")

    def __init__(self, text: str) -> None:
        self._text = text
        self._tokens = self._tokenize(text)
        self._position = 0

    def _tokenize(self, text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        position = 0
        length = len(text)
        while position < length:
            match = _TOKEN_RE.match(text, position)
            if match is None:
                raise InvalidMarker(
                    f"unexpected character at {position} in marker: {text!r}",
                )
            kind = match.lastgroup
            assert kind is not None
            if kind != "WS":
                value = match.group()
                if kind == "NAME":
                    lowered = value.lower()
                    if lowered == "and":
                        kind = "AND"
                    elif lowered == "or":
                        kind = "OR"
                    elif lowered == "in":
                        kind = "IN"
                    elif lowered == "not":
                        kind = "NOT"
                tokens.append((kind, value))
            position = match.end()
        return tokens

    def _peek(self) -> str:
        position = self._position
        if position >= len(self._tokens):
            return ""
        return self._tokens[position][0]

    def _read(self) -> tuple[str, str]:
        token = self._tokens[self._position]
        self._position += 1
        return token

    def _fail(self, expected: str) -> Any:
        raise InvalidMarker(
            f"expected {expected} at token {self._position} in marker: {self._text!r}",
        )

    def expect_end(self) -> None:
        if self._position != len(self._tokens):
            self._fail("end of marker")

    def parse_marker(self) -> tuple[Any, ...]:
        clauses = [self.parse_and()]
        while self._peek() == "OR":
            self._read()
            clauses.append(self.parse_and())
        if len(clauses) == 1:
            return clauses[0]
        return (_OR, tuple(clauses))

    def parse_and(self) -> tuple[Any, ...]:
        clauses = [self.parse_expression()]
        while self._peek() == "AND":
            self._read()
            clauses.append(self.parse_expression())
        if len(clauses) == 1:
            return clauses[0]
        return (_AND, tuple(clauses))

    def parse_expression(self) -> tuple[Any, ...]:
        if self._peek() == "LPAREN":
            self._read()
            inner = self.parse_marker()
            if self._peek() != "RPAREN":
                self._fail("')'")
            self._read()
            return inner
        left = self.parse_variable()
        operator = self.parse_operator()
        right = self.parse_variable()
        if left[0] == _STR and right[0] == _STR:
            raise InvalidMarker(
                f"comparison between two literals in marker: {self._text!r}",
            )
        # PEP 685: extra names compare normalised. Doing it here rather than at
        # evaluation time means the canonical spelling carries it too, so two
        # requirements that name the same extra differently compare equal.
        if left == (_VAR, "extra") and right[0] == _STR:
            right = (_STR, canonicalize_name(right[1]))
        elif right == (_VAR, "extra") and left[0] == _STR:
            left = (_STR, canonicalize_name(left[1]))
        return (_CMP, left, operator, right)

    def parse_variable(self) -> tuple[str, str]:
        kind = self._peek()
        if kind == "STRING":
            return (_STR, self._read()[1][1:-1])
        if kind == "NAME":
            name = self._read()[1].replace(".", "_")
            if name not in _VARIABLE_NAMES:
                raise InvalidMarker(
                    f"unknown environment marker variable {name!r} in {self._text!r}",
                )
            return (_VAR, VARIABLE_ALIASES.get(name, name))
        return self._fail("a marker variable or a quoted string")

    def parse_operator(self) -> str:
        kind = self._peek()
        if kind == "OP":
            return self._read()[1]
        if kind == "IN":
            self._read()
            return "in"
        if kind == "NOT":
            self._read()
            if self._peek() != "IN":
                self._fail("'in' after 'not'")
            self._read()
            return "not in"
        return self._fail("a marker operator")


def _text_compare(left: str, operator: str, right: Any) -> bool:
    """Compare two marker values as text.

    Ordering arbitrary strings is not meaningful, so the specification leaves
    ``<`` and ``>`` false and reads ``<=``/``>=`` as equality rather than
    inventing a lexicographic answer. ``in``/``not in`` are Python's.
    """
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "in":
        return left in right
    if operator == "not in":
        return left not in right
    if operator in ("<=", ">="):
        return left == right
    if operator in ("<", ">"):
        return False
    # `~=` and `===` on a non-version variable have no text meaning.
    raise UndefinedComparison(
        f"undefined comparison {operator!r} on {left!r} and {right!r}",
    )


_SPECIFIER_LIMIT = 1024
_specifiers: dict[tuple[str, str], Specifier | None] = register_table({})


def _specifier_for(operator: str, text: str) -> Specifier | None:
    """The Specifier for one marker clause, interned; None if it is not one.

    A marker's operator and literal are fixed by its text, so a resolve that
    evaluates the same marker against thousands of candidates would otherwise
    rebuild -- and revalidate -- the same clause every time.
    """
    key = (operator, text)
    try:
        return _specifiers[key]
    except KeyError:
        try:
            value: Specifier | None = Specifier(operator, text)
        except ValueError:
            value = None
        bounded_put(_specifiers, key, value, _SPECIFIER_LIMIT)
        return value


def _version_compare(left: str, operator: str, right: Any) -> bool:
    """Compare as versions when the clause is a version specifier, else as text.

    A version-valued variable can still hold something that is not a PEP 440
    version -- ``platform_release`` is a kernel string on most systems -- and
    the clause may not be a specifier at all (``in``). Either falls back to the
    text rules, which is what the reference implementation does.
    """
    if operator in ("in", "not in") or not isinstance(right, str):
        return _text_compare(left, operator, right)
    if operator == "===":
        # Arbitrary equality compares text, whether or not either side is a
        # version: ``platform_release === "5.15.0-1034-azure"`` must hold.
        return left.lower() == right.lower()
    specifier = _specifier_for(operator, right)
    if specifier is None:
        return _text_compare(left, operator, right)
    try:
        parsed = Version(left)
    except InvalidVersion:
        return _text_compare(left, operator, right)
    return specifier.contains(parsed)


def _resolve(side: tuple[str, str], environment: Mapping[str, Any]) -> Any:
    if side[0] == _STR:
        return side[1]
    name = side[1]
    try:
        return environment[name]
    except KeyError:
        raise UndefinedEnvironmentName(
            f"{name!r} does not exist in evaluation environment",
        ) from None


def evaluate_marker(tree: tuple[Any, ...], environment: Mapping[str, Any]) -> bool:
    """Whether ``tree`` holds in ``environment``."""
    kind = tree[0]
    if kind == _CMP:
        _, left_node, operator, right_node = tree
        left = _resolve(left_node, environment)
        right = _resolve(right_node, environment)
        key = left_node[1] if left_node[0] == _VAR else right_node[1]
        if key in SET_MARKERS:
            left = canonicalize_name(left) if isinstance(left, str) else left
            if isinstance(right, str):
                right = canonicalize_name(right)
            else:
                right = {canonicalize_name(value) for value in right}
        if key in VERSION_MARKERS:
            return _version_compare(left, operator, right)
        return _text_compare(left, operator, right)
    if kind == _AND:
        return all(evaluate_marker(node, environment) for node in tree[1])
    return any(evaluate_marker(node, environment) for node in tree[1])


def marker_matches(text: str, environment: Mapping[str, Any]) -> bool:
    """Parse ``text`` and evaluate it against ``environment``."""
    return evaluate_marker(parse_marker(text), environment)


_NORMALIZE_MARKER_RE = re.compile(r"\s+")


def format_marker(tree: tuple[Any, ...], *, _top: bool = True) -> str:
    """The canonical spelling of a parsed marker.

    Two markers that mean the same thing but are spelled differently -- one
    quoted with apostrophes, one with a redundant group, one with padding --
    format the same, which is what lets requirement identity be about meaning
    rather than about whitespace.
    """
    kind = tree[0]
    if kind == _CMP:
        _, left, operator, right = tree
        return f"{_format_side(left)} {operator} {_format_side(right)}"
    joined = " and " if kind == _AND else " or "
    parts = [format_marker(node, _top=False) for node in tree[1]]
    text = joined.join(parts)
    # `and` binds tighter than `or`, so only a nested `or` needs its group back.
    if kind == _OR and not _top:
        return f"({text})"
    return text


def _format_side(side: tuple[str, str]) -> str:
    if side[0] == _VAR:
        return side[1]
    return '"' + side[1] + '"'


_CANONICAL_LIMIT = 4096
_canonical: dict[str, str] = register_table({})


def canonical_marker(text: str) -> str:
    """The canonical spelling of a marker string, interned.

    Requirement identity reads this on every comparison, so it has to be a
    dict lookup rather than a re-format. Text that does not parse is returned
    with its whitespace collapsed -- a caller that only displays or compares
    the marker must not fail on it.
    """
    cached = _canonical.get(text)
    if cached is not None:
        return cached
    stripped = text.strip()
    try:
        formatted = format_marker(parse_marker(stripped))
    except InvalidMarker:
        formatted = _NORMALIZE_MARKER_RE.sub(" ", stripped)
    bounded_put(_canonical, text, formatted, _CANONICAL_LIMIT)
    return formatted
