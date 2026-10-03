# kpip compile

Builds a native `kpip` binary with [Nuitka](https://github.com/Nuitka/Nuitka),
taken from the latest upstream `develop` and patched with kpip's own changes.
Nothing here ships inside kpip's own distributions; it produces the binary.

Run from this directory:

```console
uv run kpip-compile build
```

The result is `build/kpip` (`build\kpip.exe` on Windows), a onefile binary
that embeds the Python version it was compiled with. Pick another interpreter
with `--python`, and pass extra Nuitka options after `--`:

```console
uv run kpip-compile build --python /opt/homebrew/bin/python3.15 -- --report=report.xml
```

## Faster onefile builds

```console
uv run kpip-compile build --fast-compress
```

Nuitka compresses the onefile payload at zstd level 22, whose window is so
large that zstd compresses the whole payload as one job: about 100 seconds
for an unstripped build, on one core. `--fast-compress` compresses at level
19 instead, in 4 MB jobs that zstd shares across the build's jobs: about 15
seconds for a stripped build's 50 MB program, for a payload about 1.3%
larger. Both need an interpreter whose zstd is built
multithreaded, as MonolithPy's is. Release builds leave it off.

## Onefile cache mode

By default, the binary unpacks once into
`{CACHE_DIR}/kpip-onefile/{VERSION}/<interpreter>`, such as
`~/.cache/kpip-onefile/0.0.1/cpython-314`, and later runs start from there.
It stays outside kpip's own cache, which `kpip cache` may clear, and each
Python gets its own directory because unpacking never removes files. With the patches below, a run that finds an
unchanged unpacking starts within a few milliseconds of an uncompressed
standalone build. `--cache-mode=temporary` keeps Nuitka's default, which
unpacks into a fresh temporary directory on every run and removes it on
exit. `--mode=standalone` builds a directory instead of a single file, with the
binary at `build/kpip.dist/kpip.bin` (`kpip.exe` on Windows).

## C modules

`kpip_compile/native_plugin.py` is a Nuitka user plugin that compiles kpip's
C sources into the binary as built-in modules, with Nuitka's own compiler,
LTO and profile, on every platform. Each source registers itself before the
interpreter starts, so no extension file is shipped or loaded, and kpip
falls back to its Python twin wherever the module is absent. Today there is
one: `src/kpip/_acceleration/_kpip_link_tree.c`, the loops that hard link a
cached wheel tree into place with the GIL released, beside
`_kpip_link_tree.py`. kpip's tests build it as an extension with the test
interpreter (`cc`, or MSVC's `cl` on Windows) and run both.

## Profile-guided optimization

```console
uv run kpip-compile build --pgo
```

This uses Nuitka's `--pgo-c`, as fixed by the vendored patch `0005`:

1. **Instrumented build:** Nuitka compiles kpip with profiling and assembles its standalone distribution.
2. **Training run:** Nuitka runs `build/pgo-train`, which runs `python -m kpip_compile.pgo` over 72 kpip commands in that distribution. They cover startup, help, locks of every benchmark requirement set (from a fresh cache and from a warm one, with backtracking and unsatisfiable sets), a download, installs, `list`, `freeze`, `inspect`, an sdist wheel build and `cache`. Each process writes a profile, and Nuitka merges them.
3. **Final build:** Nuitka compiles again with the profile and builds the binary you asked for.

A failed training step fails the build. Steps that need the network, git or a build backend, or that are meant to fail, are allowed to fail.

On a warm airflow resolve this cuts CPU time by about 10% and wall time by about 5%. Output is identical. Startup and small locks don't change, since most of their time is spent in the CPython runtime, which the profile doesn't cover. The build takes about twice as long, and training needs network access.

Requirements:

- **Compiler:** Nuitka's default compiler: clang on macOS, gcc on Linux, or clang with `--clang` after `--`. With clang, `llvm-profdata` from the same LLVM is needed; on macOS it comes from Xcode.
- **Windows:** not supported, since the training runs through a shell script.

## Training MonolithPy's interpreter on kpip

`--pgo` profiles kpip's own compiled code, not the CPython runtime it calls,
where startup and small locks spend most of their time. When the binary is
built with MonolithPy, that runtime has a profile of its own, from
MonolithPy's profile-guided build, which by default trains on a slice of
CPython's test suite. To train it on kpip as well, write the training script
and build MonolithPy with it:

```console
uv run kpip-compile profile-task /tmp/kpip-profile-task.py
MONOLITHPY_PROFILE_TASK=/tmp/kpip-profile-task.py bash build.mac.sh <target>
```

The script runs that slice of the test suite, then kpip from source over the
startup, lock, install and inspection steps of `--pgo`'s training, with five of
its requirement sets (`kpip_compile.interpreter_pgo`). The locks need network
access. It runs on the interpreter the build has just made, instrumented, which
is several times slower than the one it becomes. kpip installs for the CPython
`python3` on `PATH`, as a compiled kpip does: MonolithPy's own `monolithpy`
tags match no published wheel, so installing for it would build every sdist.
Each step that fails is named, with the last line it printed.

## Vendored Nuitka

`kpip-compile vendor` resolves the current commit of Nuitka's `develop`
branch, fetches it into `_vendor/nuitka`, and applies
`tools/vendoring/patches/nuitka/*.patch` from the repository root in name
order. `build` does the same first. The checkout is reused until `develop`
moves or a patch changes; `vendor --force` refetches it anyway. Without
network access, an existing checkout is used with a warning.

When `develop` moves in a way a patch no longer applies to, the command stops
and names the patch to rebase.

The patches live in a subdirectory so that `vendoring sync`, which applies
`tools/vendoring/patches/*.patch` to `src/kpip/_vendor`, never picks them up.

| Patch | Purpose |
| --- | --- |
| `0001-onefile-cache-manifest.patch` | Cached mode records every unpacked file's size, mtime and inode in a manifest tied to the payload hash, so an unchanged unpacking skips decompression and checksums. Upstream issue [Nuitka#4028](https://github.com/Nuitka/Nuitka/issues/4028). |
| `0002-onefile-atomic-cached-unpacking.patch` | Cached mode writes each file under a temporary name and renames it into place, so concurrent first runs no longer truncate files that other processes are executing (`SIGBUS`). |
| `0003-lazy-inspect-typing.patch` | Standalone programs no longer import `inspect` and `typing` at startup just to patch them. `inspect` and `types` are patched once something first imports them, and the typing types are taken from the built-in `_typing`. |
| `0004-getattr-default.patch` | `getattr(obj, name, default)` gives the default only for `AttributeError`, as CPython does. Nuitka returned it for any error and left that error set, so a compiled kpip took an sdist that failed to build as having no dependencies and then crashed with `SystemError`. Upstream issue [Nuitka#4061](https://github.com/Nuitka/Nuitka/issues/4061). |
| `0005-pgo-clang-standalone.patch` | `--pgo-c` works with clang, writing LLVM raw profiles merged with the compiler's own `llvm-profdata`, and in standalone and onefile modes, where the instrumented binary now trains in a complete distribution rather than one missing its libraries and data. |
| `0006-unpack-tuples-without-iterator.patch` | Unpacking a tuple of exactly the unpacked size reads its items by index, as CPython 3.11+ does, instead of going through an iterator: compiled code had been slower than the interpreter at `a, b = t` and `for k, v in d.items()`. |
| `0007-annotate-code-cache.patch` | On 3.14, each `__annotate__` code object is unmarshalled once and reused, rather than on every execution of an annotated `def`. |
| `0008-subscript-tuple-fast-path.patch` | A tuple, or a subclass that keeps tuple's `__getitem__` such as a named tuple, is indexed by a constant directly instead of through the generic subscript slot. |
| `0009-method-call-no-dict.patch` | On 3.11+, a method of an object without an instance dictionary (`dict`, `list`, `str`, classes with `__slots__`) is called directly, without creating a bound method, as CPython's `LOAD_ATTR_METHOD_NO_DICT` does. |
| `0010-attribute-lookup-cache.patch` | On 3.12+, each attribute lookup site remembers the type it last saw and its version tag, and reads a `__slots__` member or a class constant directly while the type is unchanged. |
| `0011-include-package-once-per-module.patch` | `--include-package` includes a module present both as source and as an extension module once, as the file import resolution picks, rather than twice, which failed to link. |
| `0012-subinterpreter-traceback-dealloc.patch` | A traceback freed by a subinterpreter goes to CPython's own deallocator, not Nuitka's lock-free free list, which had handed the main interpreter memory from another interpreter's allocator and crashed the process. |
| `0013-constant-cache-without-slot-swapping.patch` | Deduplicating constants no longer swaps the hash and compare slots of built-in types, which every interpreter in the process shares: subinterpreters running meanwhile hashed and compared their own values wrongly. |
| `0014-subinterpreter-bytecode.patch` | `--subinterpreter-bytecode` embeds the named modules as bytecode in a frozen table that stays installed, so subinterpreters, which cannot import compiled modules, import them. |
| `0015-free-threaded-python.patch` | Compiled code builds and runs correctly on free-threaded 3.14, with threads running it at once; builds with the GIL get only the fixes that apply to them too. |
| `0016-walrus-genexpr-scope.patch` | An assignment expression in a generator expression binds its target in the enclosing function, as CPython does, rather than silently reading a module variable of that name. |
| `0018-monolithpy-macos-unistd-after-prelude.patch` | On macOS, `<unistd.h>` is included after the prelude, so MonolithPy's `mp_embed.h` renames `read`, `close` and the rest before the system declares them; otherwise every compile against MonolithPy failed. |
| `0019-onefile-zstd-without-threads.patch` | The onefile payload's zstd worker count is capped at what the zstd in use accepts, so a zstd built without threads compresses it rather than raising `ValueError`. |
| `0020-loader-entry-index.patch` | A module's loader entry is found through a hash index built at startup rather than by scanning the table, which `findEntry` did several times per import over kpip's ~1,300 entries. |
| `0021-onefile-exec-cached-child.patch` | In cached mode the Linux and macOS bootstrap execs the program in its place instead of forking, running it and waiting: nothing is left to clean up after it, so the fork, the second process and the wait (about 4 ms a run) bought nothing. The shell gets the program's own exit status, including death by a signal, which `WEXITSTATUS` misreported. Temporary-directory mode and Windows are unchanged. |
| `0022-onefile-compression-level-from-environment.patch` | `NUITKA_ONEFILE_COMPRESSION_LEVEL`, when set, is the onefile payload's zstd level, clamped to 1..22, and with workers the payload is cut into 4 MB jobs; the compression cache already keys each file on the level. `--fast-compress` sets it to 19. |

`_vendor/nuitka` is a git checkout whose `upstream` branch is the fetched
`develop` commit, so `git diff` inside it shows exactly what the patches
changed. To change or rebase a patch, commit the fixed changes on top of
`upstream` in the checkout, one commit per patch, and export them over the old
files:

```console
cd _vendor/nuitka
git format-patch --zero-commit --no-signature --no-numbered \
    -o ../../../../tools/vendoring/patches/nuitka upstream..HEAD
```

Keep the numeric prefixes when renaming the exported files.

## Tests

```console
uv run --group tests pytest
```
