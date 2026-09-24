"""A lock is replayed only while its inputs and every page it read are unchanged."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kpip.cli import lock_replay
from kpip.cli.fast import run_lock
from kpip.cli.lock_format import previous_lock_digest
from kpip.core.appdirs import http_cache_path, resolve_cache_dir
from kpip.index.config import DEFAULT_INDEX_URL
from kpip.network.cache import SafeFileCache
from kpip.network.freshness import CacheMetadataReader, encode_metadata

PAGE = "https://pypi.org/simple/demo/"
RENDERED = 'lock-version = "1.0"\n# replayed\n'


def store_page(
    cache_dir: str,
    url: str = PAGE,
    *,
    etag: str | None = '"v1"',
    fresh: bool = True,
) -> None:
    """An HTTP cache entry shaped like the ones ``NetworkSession`` writes."""

    now = time.time()
    metadata = {
        "status": 200,
        "reason": "OK",
        "url": url,
        "headers": {"Cache-Control": "max-age=600"},
        "expires_at": now + 600 if fresh else now - 1,
        "stored_at": now,
        "etag": etag,
        "last_modified": None,
    }
    SafeFileCache(http_cache_path(cache_dir)).set_with_body(
        url,
        encode_metadata(metadata),
        b"{}",
    )


def key_for(requirement_file: Path, **overrides: object) -> bytes:
    arguments: dict[str, object] = {
        "requirements": [],
        "requirement_files": [str(requirement_file)],
        "constraint_files": [],
        "index_urls": (DEFAULT_INDEX_URL,),
    }
    arguments.update(overrides)
    key = lock_replay.replay_key(**arguments)  # type: ignore[arg-type]
    assert key is not None
    return key


@pytest.fixture
def requirements(tmp_path: Path) -> Path:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "demo>=1\n# a comment\nother; python_version >= '3'\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def cache_dir(tmp_path: Path) -> str:
    return resolve_cache_dir(str(tmp_path / "cache"))


def record(cache_dir: str, key: bytes, *, fresh: bool = True) -> None:
    store_page(cache_dir, fresh=fresh)
    http_cache = lock_replay.open_http_cache(cache_dir)
    pages = lock_replay.page_validators(http_cache, [PAGE])
    assert pages is not None
    lock_replay.save_record(cache_dir, key, pages, RENDERED)


class TestReplayKey:
    def test_every_input_is_part_of_the_key(
        self, requirements: Path, tmp_path: Path
    ) -> None:
        base = key_for(requirements)
        constraints = tmp_path / "constraints.txt"
        constraints.write_text("demo<3\n", encoding="utf-8")

        variants = [
            key_for(requirements, requirements=["extra"]),
            key_for(requirements, constraint_files=[str(constraints)]),
            key_for(requirements, index_urls=("https://mirror.invalid/simple",)),
            key_for(requirements, no_binary=[":all:"]),
            key_for(requirements, no_build_isolation=True),
            key_for(requirements, python_version="3.9"),
            key_for(requirements, previous_lock="started from another lock"),
        ]

        assert base == key_for(requirements)
        assert len({base, *variants}) == len(variants) + 1

    def test_a_requirement_file_is_keyed_on_its_bytes(self, requirements: Path) -> None:
        before = key_for(requirements)
        requirements.write_text("demo>=2\n", encoding="utf-8")

        assert key_for(requirements) != before

    def test_a_new_kpip_does_not_replay_an_old_lock(
        self,
        requirements: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before = key_for(requirements)
        monkeypatch.setattr(lock_replay, "code_identity", lambda: ("another kpip",))

        assert key_for(requirements) != before

    @pytest.mark.parametrize(
        "requirement",
        [
            "demo @ https://files.invalid/demo-1.0-py3-none-any.whl",
            "./local/project",
            "demo-1.0-py3-none-any.whl",
            "git+https://example.invalid/demo.git",
        ],
    )
    def test_requirements_the_index_does_not_answer_are_not_replayed(
        self,
        requirement: str,
    ) -> None:
        assert (
            lock_replay.replay_key(
                requirements=[requirement],
                requirement_files=[],
                constraint_files=[],
                index_urls=(DEFAULT_INDEX_URL,),
            )
            is None
        )

    @pytest.mark.parametrize(
        "line",
        [
            "-r other.txt",
            "--index-url https://mirror.invalid/simple",
            "-e .",
            "demo \\",
        ],
    )
    def test_a_file_with_options_is_not_replayed(
        self, tmp_path: Path, line: str
    ) -> None:
        path = tmp_path / "requirements.txt"
        path.write_text(f"demo\n{line}\n", encoding="utf-8")

        assert (
            lock_replay.replay_key(
                requirements=[],
                requirement_files=[str(path)],
                constraint_files=[],
                index_urls=(DEFAULT_INDEX_URL,),
            )
            is None
        )

    def test_a_pylock_input_is_not_replayed(self, tmp_path: Path) -> None:
        path = tmp_path / "pylock.toml"
        path.write_text("", encoding="utf-8")

        assert (
            lock_replay.replay_key(
                requirements=[],
                requirement_files=[str(path)],
                constraint_files=[],
                index_urls=(DEFAULT_INDEX_URL,),
            )
            is None
        )


class TestPageState:
    def test_unchanged_fresh_pages_replay(self, cache_dir: str) -> None:
        store_page(cache_dir)
        http_cache = lock_replay.open_http_cache(cache_dir)
        pages = lock_replay.page_validators(http_cache, [PAGE])
        assert pages == ((PAGE, '"v1"', None),)

        assert lock_replay.page_state(http_cache, pages) == lock_replay.FRESH

    def test_an_expired_page_must_be_revalidated_first(self, cache_dir: str) -> None:
        store_page(cache_dir, fresh=False)
        http_cache = lock_replay.open_http_cache(cache_dir)

        state = lock_replay.page_state(http_cache, ((PAGE, '"v1"', None),))

        assert state == lock_replay.STALE_SAME

    def test_a_page_with_a_new_validator_changed(self, cache_dir: str) -> None:
        store_page(cache_dir, etag='"v2"')
        http_cache = lock_replay.open_http_cache(cache_dir)

        state = lock_replay.page_state(http_cache, ((PAGE, '"v1"', None),))

        assert state == lock_replay.CHANGED

    def test_a_missing_page_changed(self, cache_dir: str) -> None:
        http_cache = lock_replay.open_http_cache(cache_dir)

        state = lock_replay.page_state(http_cache, ((PAGE, '"v1"', None),))

        assert state == lock_replay.CHANGED

    def test_a_page_without_a_validator_cannot_be_recorded(
        self, cache_dir: str
    ) -> None:
        store_page(cache_dir, etag=None)
        http_cache = lock_replay.open_http_cache(cache_dir)

        assert lock_replay.page_validators(http_cache, [PAGE]) is None


class TestRecord:
    def test_a_record_round_trips(self, cache_dir: str, requirements: Path) -> None:
        key = key_for(requirements)
        record(cache_dir, key)

        loaded = lock_replay.load_record(cache_dir, key)

        assert loaded is not None
        assert loaded.rendered == RENDERED
        assert loaded.pages == ((PAGE, '"v1"', None),)

    def test_a_record_for_another_key_in_the_same_file_is_a_miss(
        self,
        cache_dir: str,
        requirements: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(lock_replay, "_digest", lambda value: "collision")
        record(cache_dir, key_for(requirements))

        assert (
            lock_replay.load_record(
                cache_dir, key_for(requirements, python_version="3.9")
            )
            is None
        )

    def test_a_corrupt_record_is_a_miss(
        self, cache_dir: str, requirements: Path
    ) -> None:
        key = key_for(requirements)
        record(cache_dir, key)
        Path(lock_replay.record_path(cache_dir, key)).write_bytes(b"\x00garbage")

        assert lock_replay.load_record(cache_dir, key) is None


def test_the_metadata_reader_agrees_with_the_cache(
    cache_dir: str, tmp_path: Path
) -> None:
    store_page(cache_dir)
    directory = http_cache_path(cache_dir)
    split = "https://pypi.org/simple/split/"
    SafeFileCache(directory).set(split, b'{"etag": "\\"s\\""}')
    Path(SafeFileCache(directory).get_cache_path(split) + ".body").write_bytes(b"")

    reader = CacheMetadataReader(directory)
    files = SafeFileCache(directory)

    for url in (PAGE, split, "https://pypi.org/simple/absent/"):
        assert reader.get(url) == files.get(url)


class TestFastPath:
    def arguments(
        self, requirements: Path, output: Path, cache_root: Path
    ) -> list[str]:
        return [
            "--cache-dir",
            str(cache_root),
            "-r",
            str(requirements),
            "--output",
            str(output),
        ]

    def test_an_unchanged_lock_is_replayed(
        self, tmp_path: Path, requirements: Path
    ) -> None:
        cache_root = tmp_path / "cache"
        cache_dir = resolve_cache_dir(str(cache_root))
        record(cache_dir, key_for(requirements))
        output = tmp_path / "pylock.toml"

        assert run_lock(self.arguments(requirements, output, cache_root)) == 0
        assert output.read_text(encoding="utf-8") == RENDERED

    def test_a_lock_is_replayed_only_from_the_lock_it_started_from(
        self, tmp_path: Path, requirements: Path
    ) -> None:
        """The lock at the output decides which versions are preferred.

        A record is kept for the run after it, which starts from the lock the
        record holds; from any other starting point, or with ``--upgrade``,
        the answer may differ, so it is not replayed.
        """
        cache_root = tmp_path / "cache"
        output = tmp_path / "pylock.toml"
        output.write_text(RENDERED, encoding="utf-8")
        started_from = previous_lock_digest(RENDERED.encode("utf-8"), [])
        record(
            resolve_cache_dir(str(cache_root)),
            key_for(requirements, previous_lock=started_from),
        )
        arguments = self.arguments(requirements, output, cache_root)

        assert run_lock(arguments) == 0
        assert run_lock([*arguments, "--upgrade"]) is None
        assert run_lock([*arguments, "-P", "demo"]) is None

        output.write_text(RENDERED + "# edited\n", encoding="utf-8")

        assert run_lock(arguments) is None

    def test_a_stale_page_goes_to_the_full_command(
        self,
        tmp_path: Path,
        requirements: Path,
    ) -> None:
        cache_root = tmp_path / "cache"
        record(resolve_cache_dir(str(cache_root)), key_for(requirements), fresh=False)
        output = tmp_path / "pylock.toml"

        assert run_lock(self.arguments(requirements, output, cache_root)) is None
        assert not output.exists()

    def test_no_cache_dir_never_replays(
        self, tmp_path: Path, requirements: Path
    ) -> None:
        cache_root = tmp_path / "cache"
        record(resolve_cache_dir(str(cache_root)), key_for(requirements))
        output = tmp_path / "pylock.toml"

        arguments = [
            *self.arguments(requirements, output, cache_root),
            "--no-cache-dir",
        ]

        assert run_lock(arguments) is None


class Revalidating:
    """An index whose pages answer conditional requests, recording each one."""

    def __init__(
        self,
        cache_dir: str,
        *,
        changed: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
    ) -> None:
        from kpip.network.session import NetworkSession
        from kpip_test_support.transport_mocks import make_response

        requests: list[tuple[str, str | None]] = []
        self.requests = requests

        class Session(NetworkSession):
            def open_internal(
                self, method, url, headers, body, timeout, *, stream=False
            ):
                requests.append((url, headers.get("if-none-match")))
                if url in failing:
                    raise OSError("unreachable")
                if url in changed:
                    page = (
                        b'{"meta": {"api-version": "1.0"}, "name": "demo", "files": []}'
                    )
                    return make_response(
                        status=200,
                        reason="OK",
                        url=url,
                        headers={
                            "Content-Type": "application/vnd.pypi.simple.v1+json",
                            "Cache-Control": "max-age=600",
                            "ETag": '"v2"',
                            "Content-Length": str(len(page)),
                        },
                        body=page,
                    )
                return make_response(
                    status=304,
                    reason="Not Modified",
                    url=url,
                    headers={
                        "ETag": headers.get("if-none-match", ""),
                        "Cache-Control": "max-age=600",
                    },
                    body=b"",
                )

        self.session = Session(cache=http_cache_path(cache_dir))

    def deferred(self, cache_dir: str):
        from kpip.network.deferred import DeferredNetworkSession

        deferred = DeferredNetworkSession(cache_dir=cache_dir)
        deferred.session = self.session
        return deferred


class TestRevalidationWave:
    OTHER = "https://pypi.org/simple/other/"

    def recorded(self, tmp_path: Path, requirements: Path) -> str:
        cache_dir = resolve_cache_dir(str(tmp_path / "cache"))
        store_page(cache_dir, fresh=False)
        store_page(cache_dir, self.OTHER, fresh=False)
        http_cache = lock_replay.open_http_cache(cache_dir)
        pages = lock_replay.page_validators(http_cache, [PAGE, self.OTHER])
        assert pages is not None
        lock_replay.save_record(cache_dir, key_for(requirements), pages, RENDERED)
        return cache_dir

    def options(self, requirements: Path, tmp_path: Path):
        from kpip.cli.parsers.lock import create_parser

        return create_parser().parse_args(
            [
                "-r",
                str(requirements),
                "--output",
                str(tmp_path / "pylock.toml"),
                "--cache-dir",
                str(tmp_path / "cache"),
            ],
        )

    def test_unchanged_pages_are_revalidated_together_and_replayed(
        self, tmp_path: Path, requirements: Path
    ) -> None:
        from kpip.cli.lock import replay_after_revalidation

        cache_dir = self.recorded(tmp_path, requirements)
        index = Revalidating(cache_dir)

        replayed = replay_after_revalidation(
            self.options(requirements, tmp_path),
            cache_dir,
            index.deferred(cache_dir),
            None,
        )

        assert replayed
        assert (tmp_path / "pylock.toml").read_text(encoding="utf-8") == RENDERED
        assert sorted(index.requests) == [(PAGE, '"v1"'), (self.OTHER, '"v1"')]
        record = lock_replay.load_record(cache_dir, key_for(requirements))
        assert record is not None
        assert (
            lock_replay.page_state(lock_replay.open_http_cache(cache_dir), record.pages)
            == lock_replay.FRESH
        )

    def test_a_changed_page_is_resolved_again(
        self, tmp_path: Path, requirements: Path
    ) -> None:
        from kpip.cli.lock import replay_after_revalidation

        cache_dir = self.recorded(tmp_path, requirements)
        index = Revalidating(cache_dir, changed=frozenset({self.OTHER}))

        replayed = replay_after_revalidation(
            self.options(requirements, tmp_path),
            cache_dir,
            index.deferred(cache_dir),
            None,
        )

        assert not replayed
        assert not (tmp_path / "pylock.toml").exists()
        http_cache = lock_replay.open_http_cache(cache_dir)
        # The resolve that follows finds the new page already fresh.
        assert lock_replay.stale_pages(http_cache, ((self.OTHER, '"v2"', None),)) == []

    def test_an_unreachable_page_is_left_to_resolution(
        self, tmp_path: Path, requirements: Path
    ) -> None:
        from kpip.cli.lock import replay_after_revalidation

        cache_dir = self.recorded(tmp_path, requirements)
        index = Revalidating(cache_dir, failing=frozenset({PAGE}))

        replayed = replay_after_revalidation(
            self.options(requirements, tmp_path),
            cache_dir,
            index.deferred(cache_dir),
            None,
        )

        assert not replayed

    def test_the_lock_command_skips_resolution(
        self,
        tmp_path: Path,
        requirements: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kpip.cli import lock as lock_command

        cache_dir = self.recorded(tmp_path, requirements)
        index = Revalidating(cache_dir)
        monkeypatch.setattr(
            lock_command,
            "DeferredNetworkSession",
            lambda cache_dir: index.deferred(cache_dir),
        )

        def unexpected(*args: object, **kwargs: object) -> None:
            pytest.fail("resolved a lock whose pages were all unchanged")

        monkeypatch.setattr(lock_command.ResolutionEngine, "resolve", unexpected)

        assert (
            lock_command.run_lock(
                [
                    "-r",
                    str(requirements),
                    "--output",
                    str(tmp_path / "pylock.toml"),
                    "--cache-dir",
                    str(tmp_path / "cache"),
                ],
            )
            == 0
        )
        assert (tmp_path / "pylock.toml").read_text(encoding="utf-8") == RENDERED
