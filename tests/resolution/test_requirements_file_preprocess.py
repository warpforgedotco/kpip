"""Logical-line assembly for requirements files, against pip's own routine.

Backslash continuations were never joined -- the first physical line was
emitted alone and the rest arrived as its own line -- so the output of
``pip-compile --generate-hashes``, which puts every ``--hash`` on a
continuation, failed to parse. Comments were only recognised after a literal
space, so a tab before ``#`` made the comment part of the requirement.
"""

from __future__ import annotations

import pytest
from kpip.resolution.files.parser import preprocess_requirement_lines

CASES = [
    pytest.param(
        "demopkg==1.0 \\\n    --hash=sha256:abc\n",
        [(1, "demopkg==1.0     --hash=sha256:abc")],
        id="continuation-before-hash",
    ),
    pytest.param(
        "x==1 \\\n  --hash=sha256:a \\\n  --hash=sha256:b\n",
        [(1, "x==1   --hash=sha256:a   --hash=sha256:b")],
        id="several-continuations",
    ),
    pytest.param("x==1 \\\n", [(1, "x==1")], id="backslash-at-end-of-file"),
    pytest.param(
        "demopkg==1.0\t# pinned release\n",
        [(1, "demopkg==1.0")],
        id="tab-before-comment",
    ),
    pytest.param(
        "demopkg==1.0 # pinned\n",
        [(1, "demopkg==1.0")],
        id="space-before-comment",
    ),
    pytest.param(
        "# just a comment\ndemopkg==1.0\n",
        [(2, "demopkg==1.0")],
        id="whole-line-comment",
    ),
    pytest.param(
        "a==1 \\\n# comment\nb==2\n",
        [(1, "a==1"), (3, "b==2")],
        id="comment-ends-a-continuation",
    ),
    pytest.param(
        "https://x/y.whl#sha256=deadbeef\n",
        [(1, "https://x/y.whl#sha256=deadbeef")],
        id="url-fragment-is-not-a-comment",
    ),
    pytest.param(
        "demopkg==1.0#notacomment\n",
        [(1, "demopkg==1.0#notacomment")],
        id="hash-without-leading-whitespace",
    ),
    pytest.param("\n\n  \nx==1\n\n", [(4, "x==1")], id="blank-lines-are-dropped"),
    pytest.param("", [], id="empty-file"),
    pytest.param(
        "\\a==1 \\\n  --hash=sha256:x\n",
        [(1, "a==1   --hash=sha256:x")],
        id="continuation-strips-a-leading-backslash",
    ),
]


@pytest.mark.parametrize("text, expected", CASES)
def test_preprocess(text: str, expected: list[tuple[int, str]]) -> None:
    assert preprocess_requirement_lines(text) == expected


@pytest.mark.parametrize("text, expected", CASES)
def test_matches_pip(text: str, expected: list[tuple[int, str]]) -> None:
    """The same text through pip's own preprocess(), where it is importable."""
    pip_preprocess = pytest.importorskip(
        "pip._internal.req.req_file",
        reason="upstream pip is not installed",
    ).preprocess
    assert preprocess_requirement_lines(text) == list(pip_preprocess(text))


def test_reported_line_number_is_where_the_entry_starts() -> None:
    """A joined line points at its first physical line, which is what a user
    reading the error will look for."""
    text = "a==1\nb==2 \\\n  --hash=sha256:x\nc==3\n"
    assert preprocess_requirement_lines(text) == [
        (1, "a==1"),
        (2, "b==2   --hash=sha256:x"),
        (4, "c==3"),
    ]


def test_only_uppercase_environment_variables_expand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip's requirements file format: ``${NAME}``, POSIX and uppercase."""
    from kpip.resolution.files.options import expand_env_variables

    monkeypatch.setenv("TOKEN_1", "secret")
    monkeypatch.setenv("lower", "nope")

    assert expand_env_variables("https://${TOKEN_1}@h/${lower}") == (
        "https://secret@h/${lower}"
    )
