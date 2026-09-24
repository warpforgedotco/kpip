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

By default, the binary unpacks once into `{CACHE_DIR}/kpip-onefile/{VERSION}`
(outside kpip's own cache, which `kpip cache` may clear) and
later runs start from there. With the patches below, a run that finds an
unchanged unpacking starts within a few milliseconds of an uncompressed
standalone build. `--cache-mode=temporary` keeps Nuitka's default, which
unpacks into a fresh temporary directory on every run and removes it on
exit. `--mode=standalone` builds a directory instead of a single file.

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
