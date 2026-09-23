"""The environment write lock serializes installers without touching the target.

pip's maintainers demonstrated a runnable race when two installers drive one
environment (pypa/pip#8187); the transaction now serializes on an advisory
lock keyed by the target path. The lock must (a) exclude a second holder
while held, (b) leave the target completely untouched -- a failed install
must leave an absent target absent, and installed trees must not grow
bookkeeping files -- and (c) degrade to running unlocked rather than failing
an install it cannot protect.  It must also stay out of the temp directory:
the file outlives the transaction that created it, so a scratch directory
(which callers and the OS are entitled to sweep) is the wrong home.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest
from kpip.host import lock
from kpip.host.lock import environment_write_lock, lock_path_for


def test_the_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    target = str(tmp_path / "site-packages")
    order: list[str] = []
    first_holds = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with environment_write_lock(target):
            order.append("first-acquired")
            first_holds.set()
            assert release_first.wait(timeout=10)
            order.append("first-releasing")

    def second() -> None:
        assert first_holds.wait(timeout=10)
        with environment_write_lock(target):
            order.append("second-acquired")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    threads[1].start()

    assert first_holds.wait(timeout=10)
    release_first.set()
    for thread in threads:
        thread.join(timeout=10)

    assert order == ["first-acquired", "first-releasing", "second-acquired"]


def test_the_target_is_never_touched(tmp_path: Path) -> None:
    target = tmp_path / "venv" / "lib" / "site-packages"

    with environment_write_lock(str(target)):
        assert not target.exists()

    assert not target.parent.exists()
    assert os.path.exists(lock_path_for(str(target)))


def test_the_lock_path_is_stable_per_target(tmp_path: Path) -> None:
    target = str(tmp_path / "site-packages")
    other = str(tmp_path / "elsewhere")

    assert lock_path_for(target) == lock_path_for(target)
    assert lock_path_for(target) != lock_path_for(other)


def test_an_uncreatable_lock_still_runs_the_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> int:
        raise OSError("no locks here")

    monkeypatch.setattr(lock.os, "open", refuse)

    ran = False
    with environment_write_lock(str(tmp_path / "site-packages")):
        ran = True

    assert ran


def test_the_lock_lives_outside_the_temp_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller pointing TMPDIR at a scratch dir gets no lock file in it.

    The functional harness runs kpip under a private ``TMPDIR`` and asserts
    it is empty afterwards, so a lock left there fails every install test.
    """

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "scratch"))
    path = os.path.realpath(lock_path_for(str(tmp_path / "site-packages")))
    temp_root = os.path.realpath(tempfile.gettempdir())

    assert not path.startswith(temp_root + os.sep)


def test_the_lock_path_ignores_the_configured_cache_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installers that disagree about the cache still meet on one lock file."""

    target = str(tmp_path / "site-packages")
    monkeypatch.delenv("KPIP_CACHE_DIR", raising=False)
    default = lock_path_for(target)

    monkeypatch.setenv("KPIP_CACHE_DIR", str(tmp_path / "elsewhere"))

    assert lock_path_for(target) == default


def test_a_missing_lock_directory_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lock, "lock_dir", lambda: str(tmp_path / "fresh" / "locks"))

    with environment_write_lock(str(tmp_path / "site-packages")):
        pass

    assert os.path.isdir(str(tmp_path / "fresh" / "locks"))


def test_an_uncreatable_lock_directory_still_runs_the_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("no directory here")

    monkeypatch.setattr(lock.os, "makedirs", refuse)

    ran = False
    with environment_write_lock(str(tmp_path / "site-packages")):
        ran = True

    assert ran


@pytest.mark.skipif(
    os.path.normcase("A") == "A",
    reason="only a case-folding platform can spell one target two ways",
)
def test_case_spellings_of_one_target_share_a_lock(tmp_path: Path) -> None:
    """Two Windows spellings of an absent target must not take two locks.

    ``ntpath.realpath`` canonicalizes case only for an existing path, so an
    install target that does not exist yet reaches the digest exactly as the
    caller spelled it.
    """

    target = str(tmp_path / "Lib" / "site-packages")

    assert not os.path.exists(target)
    assert lock_path_for(target) == lock_path_for(target.upper())
    assert lock_path_for(target) == lock_path_for(target.lower())
