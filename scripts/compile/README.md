# kpip compile

Builds a native `kpip` binary with [Nuitka](https://github.com/Nuitka/Nuitka),
taken from the latest upstream `develop` and patched with kpip's own changes.
Nothing here ships inside kpip; it only produces the binary.

Run from this directory:

```console
uv run kpip-compile build
```

The result is `build/kpip` (`build\kpip.exe` on Windows), a onefile binary
that embeds the Python version it was compiled with. Pick another interpreter
with `--python`, and pass extra Nuitka options after `--`:

```console
uv run kpip-compile build --python /opt/homebrew/bin/python3.14 -- --report=report.xml
```

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
