# Vendored dependencies

The Python dependencies in this directory are generated with
[`vendoring`](https://github.com/pradyunsg/vendoring), pinned in `vendor.txt`,
and imported through the `kpip._vendor` namespace. The vendoring workflow
requires Python 3.11 or newer. Refresh them from the repository root with:

```console
uv run --group vendoring vendoring sync -v
```

| Package | Version/source | License |
| --- | --- | --- |
| urllib3 | 2.6.3 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| idna | 3.18 | BSD-3-Clause |
| nab-resolver | [`bd5a5bdf72c8`](https://github.com/notatallshaw/nab/commit/bd5a5bdf72c876a9532a1e7a6215a42a36dcc630) | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tomli | 2.4.1 | MIT |

The tool extracts license texts beside their packages, with single-module
licenses at this directory's root. Tests verify that every expected license
is shipped.

## Local patches

Patches under `tools/vendoring/patches` are applied to un-namespaced wheel
sources before imports are rewritten. A second `vendoring sync` must reproduce
the same tracked tree.

| Distribution | Patch | Purpose |
| --- | --- | --- |
| certifi | `certifi.patch` | Resolve `cacert.pem` through the `kpip._vendor.certifi` resource package. |
| nab-resolver | `nab-resolver.patch` | Preserve kpip's late-extras invalidation contract and provider priority invalidations; accelerate large discrete ranges, membership, dependency-clause construction, exact-parent clause dispatch, and unit propagation; compare infinity bounds safely. The startup-oriented value types, root hashing, snapshot truthiness, backtracking, and reused decision and derivation ranges are supplied by upstream. |

The resolver is pinned to an exact upstream commit from the `nab-resolver`
subdirectory. It includes [nab PR #1204](https://github.com/notatallshaw/nab/pull/1204),
whose short-circuit snapshot truthiness scan replaces our earlier local
implementation, plus [nab PR #1212](https://github.com/notatallshaw/nab/pull/1212)
and [nab PR #1213](https://github.com/notatallshaw/nab/pull/1213), whose
affected-package backtracking and reused decision and derivation ranges replace
our local versions of the same. The remaining optimizations and provider hooks
are layered on top through `nab-resolver.patch`.

## Windows launchers

The distlib-compatible Windows launchers are frozen repository resources under
`kpip._launchers`, outside this tool-owned directory. They are not refreshed by
`vendoring`. Their SHA-256 digests are:

| File | SHA-256 |
| --- | --- |
| `t32.exe` | `6b4195e640a85ac32eb6f9628822a622057df1e459df7c17a12f97aeabc9415b` |
| `t64-arm.exe` | `ebc4c06b7d95e74e315419ee7e88e1d0f71e9e9477538c00a93a9ff8c66a6cfc` |
| `t64.exe` | `81a618f21cb87db9076134e70388b6e9cb7c2106739011b6a51772d22cae06b7` |
| `w32.exe` | `47872cc77f8e18cf642f868f23340a468e537e64521d9a3a416c8b84384d064b` |
| `w64-arm.exe` | `c5dc9884a8f458371550e09bd396e5418bf375820a31b9899f6499bf391c7b2e` |
| `w64.exe` | `7a319ffaba23a017d7b1e18ba726ba6c54c53d6446db55f92af53c279894f8ad` |
