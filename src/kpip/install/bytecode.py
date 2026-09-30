"""Byte-compilation across worker processes.

Compiling is the dominant cost of filling the archive cache -- for a sixteen
wheel set it was measured at 2.5s of a 2.7s fill -- and ``compile()`` holds
the GIL, so the thread pool that extracts wheels cannot overlap any of it.

The work therefore goes to child interpreters running
:mod:`kpip.install._compile_worker`. They are started once and reused for
every module in the session, because starting an interpreter costs about as
much as compiling a small module.

Everything here is optional. If workers cannot be started, misbehave, or
time out, the caller compiles in-process instead: this makes installs
faster, it is never the reason one fails.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from typing import TYPE_CHECKING

from kpip.core.utils import default_worker_count

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterable

CompileJob = tuple[str, str, str]
"""A module to compile: source path, ``.pyc`` path, and the name to record
inside the code object."""

MAX_WORKERS = 4
"""Worker processes to run.

Compilation is CPU-bound -- 0.98 CPU seconds per wall second, single
threaded -- so this is bounded by cores, not by I/O. Four is where the
benchmark set stops improving: swept against in-process on an eight core
machine, 2 workers gave 1.18x, 4 gave 1.31x, and 6, 8 and 12 gave 1.29x,
1.24x and 1.36x, which is noise around the same number. The wheels are
already extracted across a wider thread pool competing for the same cores,
so more compile workers mostly take turns.
"""

STARTUP_TIMEOUT = 30.0

COMPILE_TIMEOUT = 60.0
"""Longer than any single module should ever take."""

_READY = "Ready"


class _Worker:
    """One child interpreter, and the pipe it answers on.

    A reader thread drains stdout into a queue rather than the parent polling
    the pipe. ``select`` cannot do that portably -- on Windows it accepts only
    sockets, so handing it a ``subprocess.PIPE`` raises and every worker would
    look unstartable, silently costing that platform the pool entirely. One
    thread per worker, started once, is both portable and less code.
    """

    __slots__ = ("_lines", "_reader", "process")

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process

        self._lines: queue.Queue[str | None] = queue.Queue()

        self._reader = threading.Thread(
            target=self._drain,
            name="kpip-compile-read",
            daemon=True,
        )

        self._reader.start()

    def _drain(self) -> None:
        stdout = self.process.stdout

        if stdout is not None:
            try:
                # readline rather than iterating the stream: iteration reads
                # ahead into a buffer, so on a pipe a line can sit unseen. Every
                # round trip here waits on one line, so that is added latency.
                for line in iter(stdout.readline, ""):
                    self._lines.put(line)

            except (OSError, ValueError):
                pass

        # A sentinel, so a waiting caller learns the worker is gone instead of
        # sitting out the whole timeout for a pipe that will never speak.
        self._lines.put(None)

    def _readline(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=timeout)

        except queue.Empty:
            return None

    def greet(self, timeout: float) -> bool:
        """Whether the worker announced itself ready."""
        return self._readline(timeout) == f"{_READY}\n"

    def compile(self, job: CompileJob) -> bool:
        """Compile one module. Returns ``False`` if this worker is unusable."""
        source, destination, display = job

        stdin = self.process.stdin

        if stdin is None:
            return False

        try:
            stdin.write(f"{source}\t{destination}\t{display}\n")

            stdin.flush()

        except (BrokenPipeError, OSError, ValueError):
            return False

        echoed = self._readline(COMPILE_TIMEOUT)

        # The worker echoes the path it was handed. Anything else means we are
        # not talking to the script we think we are, so stop trusting it.
        return echoed is not None and echoed.rstrip("\n") == source

    def close(self) -> None:
        process = self.process

        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()

            except OSError:
                pass

        try:
            process.wait(timeout=5)

        except Exception:
            process.kill()


def _worker_script() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_compile_worker.py"
    )


def _spawn() -> _Worker | None:
    """Start one worker, or ``None`` if it will not answer."""
    import subprocess

    try:
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, _worker_script()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
        )

    except (OSError, ValueError):
        return None

    worker = _Worker(process)

    if not worker.greet(STARTUP_TIMEOUT):
        worker.close()

        return None

    return worker


class _Batch:
    """One caller's jobs, and the count still outstanding."""

    __slots__ = ("done", "failed", "lock", "remaining")

    def __init__(self, total: int) -> None:
        self.remaining = total

        self.failed: list[CompileJob] = []

        self.lock = threading.Lock()

        self.done = threading.Event()

    def finish(self, job: CompileJob | None) -> None:
        with self.lock:
            if job is not None:
                self.failed.append(job)

            self.remaining -= 1

            if self.remaining == 0:
                self.done.set()


class CompilePool:
    """Worker processes behind one shared queue, started on first use.

    The queue is what makes this pay. Two shapes that look reasonable do not:
    fanning each batch across every worker builds threads per batch, so a
    three module wheel pays for four of them; handing each batch a single
    worker leaves the one big wheel -- half the work in a typical set --
    compiling serially while the other workers idle.

    Batches arrive from the pool that extracts wheels, one per wheel, and
    their sizes differ by two orders of magnitude. Pushing every job onto one
    queue drained by persistent consumers spreads a large batch over all the
    workers without costing a small one anything, and lets concurrent callers
    interleave rather than queue behind each other.
    """

    def __init__(self, workers: int) -> None:
        self._limit = max(1, min(workers, default_worker_count()))

        self._queue: queue.Queue[tuple[CompileJob, _Batch] | None] = queue.Queue()

        self._workers: list[_Worker] = []

        self._threads: list[threading.Thread] = []

        self._lock = threading.Lock()

        self._started = False

        self._broken = False

        self._live = 0

    def _ensure_started(self) -> bool:
        with self._lock:
            if self._started:
                return not self._broken

            self._started = True

            for _ in range(self._limit):
                worker = _spawn()

                if worker is None:
                    break

                self._workers.append(worker)

                thread = threading.Thread(
                    target=self._consume,
                    args=(worker,),
                    name="kpip-compile",
                    daemon=True,
                )

                thread.start()

                self._threads.append(thread)

                self._live += 1

            self._broken = not self._workers

            return not self._broken

    def _consume(self, worker: _Worker) -> None:
        """Drain the queue through one worker until it is closed or breaks."""
        while True:
            item = self._queue.get()

            if item is None:
                return

            job, batch = item

            if worker.compile(job):
                batch.finish(None)

                continue

            # A worker that breaks the protocol will keep breaking it, so this
            # consumer stops. The job goes back to its caller to compile.
            batch.finish(job)

            self._retire()

            return

    def _retire(self) -> None:
        """Stand a consumer down, and strand nothing if it was the last.

        Whatever is still queued has no one left to compile it, so it is
        failed back to its callers rather than left to time out.
        """
        with self._lock:
            self._live -= 1

            self._broken = True

            last = self._live == 0

        if not last:
            return

        while True:
            try:
                item = self._queue.get_nowait()

            except queue.Empty:
                return

            if item is not None:
                job, batch = item

                batch.finish(job)

    def compile(self, jobs: Iterable[CompileJob]) -> list[CompileJob] | None:
        """Compile ``jobs``, returning the ones a worker could not take.

        ``None`` means no worker was available at all and the whole batch is
        the caller's to compile.
        """
        pending: list[CompileJob] = []

        rejected: list[CompileJob] = []

        for job in jobs:
            (pending if _is_transmittable(job) else rejected).append(job)

        if not pending:
            return rejected

        if not self._ensure_started():
            return None

        with self._lock:
            if not self._live:
                return None

        batch = _Batch(len(pending))

        for job in pending:
            self._queue.put((job, batch))

        # Bounded so a worker that stops answering cannot hang an install; the
        # per-job timeout inside the worker already bounds each round trip.
        if not batch.done.wait(COMPILE_TIMEOUT * 2):
            return None

        rejected.extend(batch.failed)

        return rejected

    def close(self) -> None:
        with self._lock:
            self._started = True

            self._broken = True

            workers, self._workers = self._workers, []

            threads, self._threads = self._threads, []

            self._live = 0

        for _ in threads:
            self._queue.put(None)

        for worker in workers:
            worker.close()

        for thread in threads:
            thread.join(timeout=5)


def _is_transmittable(job: CompileJob) -> bool:
    """Whether ``job`` survives a tab-separated line.

    Paths holding a tab or a newline would be misparsed by the worker; they
    are rare enough to just compile in-process.
    """
    return not any(character in field for field in job for character in "\t\r\n")


_POOL: CompilePool | None = None

_POOL_LOCK = threading.Lock()


def compile_jobs(jobs: list[CompileJob]) -> list[CompileJob]:
    """Compile ``jobs`` across worker processes.

    Returns the jobs no worker took, for the caller to compile in-process.
    """
    global _POOL

    if not jobs:
        return []

    if _in_subinterpreter():
        return jobs

    with _POOL_LOCK:
        if _POOL is None:
            import atexit

            _POOL = CompilePool(MAX_WORKERS)

            atexit.register(shutdown)

        pool = _POOL

    remaining = pool.compile(jobs)

    return jobs if remaining is None else remaining


def _in_subinterpreter() -> bool:
    """Whether this runs in a subinterpreter, e.g. an archive worker.

    One takes no worker processes: each needs a daemon thread to read its
    answers, which a subinterpreter refuses, so the job failed there and was
    unpacked again in the main interpreter -- every install that compiles.
    Nor does it need them. It is one of several unpacking side by side, each
    with a lock of its own, so compiling in it is already in parallel.
    """
    try:
        import _interpreters

    except ImportError:
        return False

    return _interpreters.get_current()[0] != _interpreters.get_main()[0]


def shutdown() -> None:
    """Stop the workers. Safe to call more than once."""
    global _POOL

    with _POOL_LOCK:
        pool, _POOL = _POOL, None

    if pool is not None:
        pool.close()
