"""Byte-compilation across worker processes.

Compiling is the dominant cost of filling the archive cache -- for a sixteen
wheel set it was measured at 2.5s of a 2.7s fill -- and ``compile()`` holds
the GIL, so the thread pool that extracts wheels cannot overlap any of it.

The work therefore goes to child interpreters running the loop in
:mod:`kpip.install._compile_worker`. They are started once and reused for
every module in the session, because starting an interpreter costs about as
much as compiling a small module.

The bytecode is the target interpreter's: the Python kpip installs for,
which need not be the one running kpip, and is not when kpip is compiled.
The workers are always that interpreter. When it compiles just as this
process does -- the same cache tag and magic number -- this process may
compile what they decline, and the archive cache keeps their bytecode for
later installs. Otherwise nothing is compiled here: this process's
``marshal`` neither reads nor writes another version's code.

Everything here is optional. If workers cannot be started, misbehave, or
time out, the caller compiles in-process instead: this makes installs
faster, it is never the reason one fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import atexit
import importlib.util
import os
import py_compile
import queue
import subprocess
import sys
import threading

from kpip.core.compiled import is_compiled
from kpip.core.utils import default_worker_count
from kpip.host.interpreter_facts import target_interpreter
from kpip.install._compile_worker import SOURCE

if TYPE_CHECKING:
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

            except OSError, ValueError:
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

        except BrokenPipeError, OSError, ValueError:
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


def _target_bytecode() -> tuple[str | None, str]:
    """The target interpreter's cache tag and magic number.

    This process's own when it is the target -- run from source, with no
    ``--python`` -- read from ``sys.implementation`` rather than from every
    fact the probe gathers, which a warm install did not otherwise need.
    """
    if not is_compiled() and not os.environ.get("KPIP_PYTHON"):
        return sys.implementation.cache_tag, importlib.util.MAGIC_NUMBER.hex()

    interpreter = target_interpreter(installing=False)

    return interpreter.cache_tag, interpreter.magic


def compiles_as_this_process() -> bool:
    """Whether the target interpreter's bytecode is what this process
    compiles: the same cache tag and magic number."""
    return _target_bytecode() == (
        sys.implementation.cache_tag,
        importlib.util.MAGIC_NUMBER.hex(),
    )


def pyc_name(module: str) -> str | None:
    """The file name of the ``.pyc`` the target interpreter reads for the
    module file ``module``, or ``None`` if it reads none.

    ``cache_from_source`` would answer for this process: its cache tag, and
    its ``-O`` level, where kpip compiles unoptimized.
    """
    cache_tag = _target_bytecode()[0]

    if cache_tag is None:
        return None

    return f"{module[:-3]}.{cache_tag}.pyc"


def pyc_path(module_path: str) -> str | None:
    """Where the target interpreter reads the bytecode of the module at
    ``module_path``: ``__pycache__`` beside it, whatever
    ``sys.pycache_prefix`` says. ``None`` if it reads none."""
    directory, module = os.path.split(module_path)

    name = pyc_name(module)

    return None if name is None else os.path.join(directory, "__pycache__", name)


def compile_in_process(job: CompileJob) -> None:
    """Compile one module here. Only for bytecode this process compiles as
    the target does: see :func:`compiles_as_this_process`."""
    source, output, display = job

    try:
        py_compile.compile(
            source,
            cfile=output,
            dfile=display,
            doraise=False,
            optimize=0,
            quiet=2,
        )

    except OSError, ValueError, RecursionError, MemoryError:
        pass


def compile_modules(jobs: list[CompileJob]) -> None:
    """Compile ``jobs`` as the target interpreter would: in its workers, and
    what they decline here when this process compiles as the target does --
    otherwise that goes without bytecode, as a module that will not compile
    does."""
    declined = compile_jobs(jobs)

    if declined and compiles_as_this_process():
        for job in declined:
            compile_in_process(job)


def bytecode_key() -> str | None:
    """What the target interpreter's bytecode is cached under -- its cache
    tag and magic number -- or ``None`` for one that reads no bytecode."""
    cache_tag, magic = _target_bytecode()

    return None if cache_tag is None else f"{cache_tag}-{magic}"


def target_magic() -> bytes:
    """The magic number the target interpreter's ``.pyc`` files begin with."""
    return bytes.fromhex(_target_bytecode()[1])


def place_pyc(cached: str, source: str, output: str, magic: bytes) -> bytes | None:
    """Copy the cached ``.pyc`` of ``source``'s module to ``output``, its
    header naming ``source``; what was written, or ``None`` if there is no
    usable cached one.

    The cache compiled the same bytes ``source`` holds, so only the header's
    source mtime and size need changing -- not for a hash-based ``.pyc``,
    whose hash still holds -- and the body is copied whatever version wrote
    it. The ``co_filename`` inside names where it was compiled: the target
    interpreter puts the module's real path there when it imports it.
    """
    try:
        with open(cached, "rb") as file:
            body = file.read()

    except OSError:
        return None

    if len(body) < 16 or body[:4] != magic:
        return None

    if not int.from_bytes(body[4:8], "little") & 1:
        try:
            stat = os.stat(source)

        except OSError:
            return None

        body = b"".join(
            (
                body[:8],
                (int(stat.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little"),
                (stat.st_size & 0xFFFFFFFF).to_bytes(4, "little"),
                body[16:],
            )
        )

    try:
        try:
            file = open(output, "wb")  # noqa: SIM115

        except FileNotFoundError:
            # The first module of its package: make __pycache__ once.
            os.makedirs(os.path.dirname(output), exist_ok=True)

            file = open(output, "wb")  # noqa: SIM115

        with file:
            file.write(body)

    except OSError:
        return None

    return body


def _worker_command() -> list[str]:
    """How to start a worker: the target interpreter, handed the loop.

    Text, with ``-c``: a compiled kpip has no script on disk to point it at,
    and its ``sys.executable`` is a ``python`` beside the binary that does
    not exist.
    """

    return [target_interpreter(installing=False).executable, "-c", SOURCE]


def _spawn(command: list[str]) -> _Worker | None:
    """Start one worker, or ``None`` if it will not answer."""

    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
        )

    except OSError, ValueError:
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

            command = _worker_command()

            for _ in range(self._limit):
                worker = _spawn(command)

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

    Returns the jobs no worker took, for the caller to compile in-process
    if :func:`compiles_as_this_process`, else to leave without bytecode.
    """
    global _POOL

    if not jobs:
        return []

    with _POOL_LOCK:
        if _POOL is None:
            _POOL = CompilePool(MAX_WORKERS)

            atexit.register(shutdown)

        pool = _POOL

    remaining = pool.compile(jobs)

    return jobs if remaining is None else remaining


def shutdown() -> None:
    """Stop the workers. Safe to call more than once."""
    global _POOL

    with _POOL_LOCK:
        pool, _POOL = _POOL, None

    if pool is not None:
        pool.close()
