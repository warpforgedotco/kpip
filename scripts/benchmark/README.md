# kpip benchmark

Local `hyperfine` benchmarks for comparing `kpip` against `uv`.

Requirements:

- `hyperfine` on `PATH`
- `uv` on `PATH`, or pass `--uv-path`

Run from this directory:

```console
uv run kpip-bench --workload offline --benchmark startup-help --benchmark lock-warm
```

The default `offline` workload is generated locally and never touches the
network. List the mirrored official uv workloads and their capabilities with:

```console
uv run kpip-bench --list-workloads
```

Run Jupyter's cold and warm resolver and installer comparisons:

```console
uv run kpip-bench --workload jupyter
```

Run every official uv workload. Resolver-only workloads run the cold and warm
lock cases; workloads with an upstream `compiled/*.txt` fixture also run cold
and warm installation cases:

```console
uv run kpip-bench --workload live
```

To limit the complete corpus to resolver benchmarks:

```console
uv run kpip-bench \
  --workload live \
  --benchmark lock-cold \
  --benchmark lock-warm
```

`lock-refresh` locks from a warm cache with `--refresh` on both tools, so
every cached page is revalidated: the lock a cache gets once the index's
`max-age` has passed. It runs only when asked for:

```console
uv run kpip-bench --workload jupyter --benchmark lock-refresh
```

By default, kpip is measured as `python -m kpip`. To measure the direct
console-script style launcher, pass `--kpip-launcher direct`:

```console
uv run kpip-bench --kpip-launcher direct --benchmark startup-help
```

To measure a compiled kpip as well, build it with `scripts/compile` and pass
the binary. It runs as its own `kpip-compiled` command beside `kpip` and `uv`:

```console
(cd ../compile && uv run kpip-compile build --mode standalone)
uv run kpip-bench --kpip-compiled ../compile/build/kpip.dist/kpip.bin \
  --benchmark lock-warm
```

A onefile build (`../compile/build/kpip`) works too. Its first run unpacks the
payload, which hyperfine's warm-up runs absorb. `--compiled-only` leaves out
the kpip launched from source, so the compiled binary is compared with uv
alone. With `--json`, `meta.json` records the compiled build's version and
embedded Python as `kpip_compiled_version` and the binary's SHA-256 as
`kpip_compiled_sha256`. `kpip-bench-compare` warns when the version or
embedded Python differs, names the two binaries when they differ, and warns
when both runs measured the same one.

Startup-focused cases:

```console
uv run kpip-bench \
  --benchmark startup-help \
  --benchmark startup-version \
  --benchmark startup-install-help \
  --benchmark startup-lock-help \
  --benchmark startup-list-help \
  --benchmark startup-invalid-command \
  --benchmark startup-list-empty \
  --benchmark startup-fast-lock \
  --benchmark startup-fast-install
```

`startup-fast-lock`/`startup-fast-install` measure a single dependency-free
package against an already-warm cache, isolating per-invocation overhead
(process start, arg parsing, provider setup) from the graph-resolution cost
that `lock-warm`/`install-warm` measure against the full offline workload.

To run the Trio/PyPI workload used by uv's public benchmark documentation:

```console
uv run kpip-bench --workload trio --benchmark lock-cold --benchmark install-cold
```

`live` is the suite selector for the complete official corpus; concrete names
such as `trio` select one workload. The corpus is mirrored in
[`requirements`](requirements/README.md), including source inputs, compiled
installer inputs, Airflow constraints, explicit backtracking cases, and the
Transformers project fixture.

Official uv workloads are intentionally opt-in because they can depend on
network latency, current PyPI state, VCS availability, target Python, platform
wheels, and cache behavior outside this repository. `--list-workloads` reports
cases with an upstream recommended Python version.

A workload with a recommended Python is locked for that version rather than
for whichever interpreter runs the suite, since that is the version its
constraints were curated against: `airflow2` pins releases that no interpreter
past 3.10 can install, and resolving it for the suite's own Python fails
outright. kpip is given `--python-version`, because its own floor is 3.10 and
it cannot run on the 3.8 that workload wants; uv is pointed at a real
interpreter with `--python`, because it builds source distributions with the
interpreter it runs on.

Two benchmark modes from uv's own harness
(`astral-sh/uv/scripts/benchmark/src/benchmark/resolver.py`'s `Benchmark`
enum) are deliberately not in `BENCHMARKS` above: `resolve-incremental` (add
one new dependency to an existing lockfile, re-lock) and `resolve-noop`
(re-lock against a lockfile that already satisfies the input, expecting a
cheap confirmation). Both measure whether a tool reuses an existing lockfile
instead of fully re-resolving. `kpip lock` has no such reuse path -- it
always resolves from scratch regardless of what's already on disk at
`--output` -- so running either case against kpip would just be `lock-warm`
again under a different name, not a distinct measurement. Revisit if `kpip
lock` ever grows preferred-versions-from-an-existing-lockfile support.

## Recording a baseline

```console
uv run kpip-bench-record
```

Sweeps the offline and live workloads with `--json` into
`benchmark-runs/<branch>-<timestamp>/` (gitignored) and prints the
`kpip-bench-compare` line for a later run. `--workload` narrows the sweep,
`-o` picks the directory, and anything after `--` is forwarded to
`kpip-bench`.

It refuses to record unless the machine is actually quiet -- 1-minute load
average under `cores/4`, on mains power, not thermally limited -- and names
every blocker plus the top CPU consumers rather than stopping at the first
one. A baseline recorded under load is worse than no baseline: it looks
authoritative and quietly poisons every comparison made against it. `--force`
overrides, and says so in the output.

"Not thermally limited" means `pmset -g therm` reports no CPU limit below
100; the keys are printed whenever there is a CPU power status at all, so
their presence alone is not throttling. A platform with no load average of
its own -- Windows has no `os.getloadavg` -- is a blocker too, on the same
reasoning: an unverifiable machine is exactly the one whose numbers look
authoritative and are not.

Note that `kpip-bench` measures whatever `--kpip-python` points at, which
defaults to this harness's own interpreter -- pinned to 3.10 by
`.python-version`, not the 3.12 the CodSpeed job uses. `meta.json` records
which one ran, and `kpip-bench-compare` warns when two runs disagree.

## Comparing two runs

`--json` also writes a `meta.json` recording the interpreter/uv versions and
git commit used, alongside the per-benchmark `--export-json` files. To
compare a change against a baseline, run `--json` once per checkout into
separate directories, then:

```console
uv run kpip-bench-compare before/ after/
```

This prints a before/after/delta table per benchmark and tool, and warns if
`meta.json` shows the two runs used different interpreters -- a fresh `uv
sync` with no Python pin can silently resolve a different version than an
existing checkout, which will otherwise look like a real performance change.

## No shell in the measured path

hyperfine is invoked with `--shell=none`, so every benchmarked command is
exec'd directly instead of through `/bin/sh`. Two reasons:

- On macOS `/bin/sh` is SIP-protected and strips `DYLD_*` from the
  environment of everything it spawns, which silently detaches any profiler
  that attaches by injection (CodSpeed's walltime instrument, samply,
  Instruments) from the process actually being measured.
- It removes a `fork`+`exec` from every timed iteration. hyperfine calibrates
  and subtracts the mean shell startup, but that correction has its own
  variance, and the `startup-*` benchmarks measure ~10-50 ms.

With no shell there is nothing to interpret a command string, so two things
moved:

- Per-command env vars (`PYTHONPATH`, `KPIP_CACHE_DIR`) are set on the
  hyperfine process and inherited, rather than written as a `FOO=bar` prefix.
  Wrapping each command in a helper interpreter instead would put a whole
  Python startup inside the timed region. `Hyperfine.environment` raises if
  two commands in one benchmark want different values for the same variable.
- `--setup`/`--prepare` steps that used to chain with `&&` are now a single
  `kpip_benchmark.runner chain --spec '<json>'` call. Preparation is untimed,
  so the extra interpreter costs the measurement nothing.

Measured on `startup-help` (30 runs, two sequential pairs), dropping the
shell moved the mean by -4.2 ms and -2.4 ms, and roughly halved the spread:
sigma 21.8 -> 8.9 ms and 9.0 -> 4.2 ms. The tighter spread is the point; the
mean shift is small but real, so JSON exports recorded before this landed are
not comparable to ones recorded after -- re-record both sides of any
`kpip-bench-compare` baseline.
