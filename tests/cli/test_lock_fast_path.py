from __future__ import annotations

from kpip.cli.fast import parse_lock_arguments
from kpip.cli.lock import applies_to_target, remote_hashed_sdist, remote_hashed_wheel
from kpip.core.versions import Version


class Candidate:
    name = "demo"
    version = Version("1.2.3")
    source_kind = "wheel"
    source_url = "https://packages.invalid/demo-1.2.3-py3-none-any.whl"
    source_filename = "demo-1.2.3-py3-none-any.whl"
    source_hashes = {"sha256": "abc123"}
    source_is_direct = False

    @property
    def path(self) -> str:
        raise AssertionError("the remote hashed wheel must not be materialized")


def test_remote_hashed_wheel_uses_index_facts() -> None:
    candidate = Candidate()

    assert remote_hashed_wheel(candidate) == {
        "name": "demo",
        "version": "1.2.3",
        "wheels": [
            {
                "name": "demo-1.2.3-py3-none-any.whl",
                "url": "https://packages.invalid/demo-1.2.3-py3-none-any.whl",
                "hashes": {"sha256": "abc123"},
            },
        ],
    }


def test_remote_wheel_without_sha256_uses_materialization_fallback() -> None:
    candidate = Candidate()
    candidate.source_hashes = {"sha512": "def456"}

    assert remote_hashed_wheel(candidate) is None


def test_remote_hashed_sdist_uses_index_facts() -> None:
    candidate = Candidate()
    candidate.source_kind = "sdist"
    candidate.source_url = "https://packages.invalid/demo-1.2.3.tar.gz"
    candidate.source_filename = "demo-1.2.3.tar.gz"

    assert remote_hashed_sdist(candidate) == {
        "name": "demo",
        "version": "1.2.3",
        "sdist": {
            "name": "demo-1.2.3.tar.gz",
            "url": "https://packages.invalid/demo-1.2.3.tar.gz",
            "hashes": {"sha256": "abc123"},
        },
    }


def test_remote_sdist_without_sha256_uses_materialization_fallback() -> None:
    candidate = Candidate()
    candidate.source_kind = "sdist"
    candidate.source_hashes = {"sha512": "def456"}

    assert remote_hashed_sdist(candidate) is None


def test_direct_artifacts_do_not_use_index_fast_paths() -> None:
    candidate = Candidate()
    candidate.source_is_direct = True

    assert remote_hashed_wheel(candidate) is None

    candidate.source_kind = "sdist"

    assert remote_hashed_sdist(candidate) is None


def test_the_fast_path_declines_a_requirement_carrying_a_marker() -> None:
    """A marker is a question about the target, which this path cannot ask.

    It reads requirement lines as strings and resolves them against a
    wheelhouse; nothing there knows which interpreter the lock is for. So it
    hands the invocation back to the full command, which does.
    """
    plain = parse_lock_arguments(["--no-index", "-f", "/wheels", "base==0.1.0"])

    assert plain is not None
    assert plain.requirements == ["base==0.1.0"]

    assert (
        parse_lock_arguments(
            ["--no-index", "-f", "/wheels", 'base==0.1.0; python_version < "3.9"'],
        )
        is None
    )


def test_a_requirement_with_no_marker_always_applies() -> None:
    assert applies_to_target("base==0.1.0") is True


def test_a_line_that_is_not_a_requirement_is_kept() -> None:
    """Dropping what cannot be parsed would be a worse answer than resolving it."""
    assert applies_to_target("--index-url https://packages.invalid/simple") is True
