# kpip

```text
          ____
     ____/ o  \_
    /    \______/>-
   /  /
  /  /      +-----+-----+
 |  |       |     |     |
 |  |       |    kpip   |
 |  |       |           |
  \  \      +-----------+
   \  '-------------------._
    '-----------------------'
```

Meet **Kip**, the courier snake.

[![Checks](https://github.com/warpforgedotco/kpip/actions/workflows/checks.yml/badge.svg)](https://github.com/warpforgedotco/kpip/actions/workflows/checks.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.txt)

**Pip reimagined for performance. Built to bring the useful parts upstream.**

kpip is an experimental Python package installer exploring how much faster
pip's familiar workflow can be. It puts performance work across startup,
dependency resolution, caching, and installation into one working system,
where the results can be tested and measured together.

The aim is to turn what works into improvements for
[pip](https://pip.pypa.io/). Welcome to the experiment.

> [!WARNING]
> kpip is an early-alpha experimental implementation, published on PyPI for
> testing and evaluation. It is not a supported pip distribution or a drop-in
> replacement and should not be used to manage critical or system Python
> environments. Interfaces, behavior, and cache formats may change.

[Get started](#installation) · [Commands](#commands) ·
[Benchmarks](#benchmarking) · [Architecture](docs/architecture.md) ·
[Contribute](#development)

## Why kpip exists

I started kpip out of curiosity about Python's performance limits. I wanted
to question the idea that you need Rust to write a "blazingly fast" program.
[uv](https://github.com/astral-sh/uv) gave me a concrete target: could I make
pip's familiar workflow that fast in Python?

That is a question I want to investigate through implementation and
measurement. How far can Python go when performance shapes the design from
the start, and where do its limits actually show up?

I want the results to be useful to pip. That means understanding which changes
make the familiar workflow faster, what they cost in complexity, and whether
they can preserve the behavior people depend on.

This is a place to try architectural changes, measure the whole installation
workflow, and work out which improvements can make their way upstream.
Some experiments narrow compatibility to isolate a performance effect;
turning those results into changes suitable for pip is part of the work.

## Installation

Install kpip from PyPI with pip:

```console
pip install kpip
kpip --version
```

Or install it as an isolated tool with uv:

```console
uv tool install kpip
kpip --version
```

Or run it from a source checkout:

```console
git clone https://github.com/warpforgedotco/kpip.git
cd kpip
uv sync --locked
uv run kpip --version
```

## Quick start

Create an environment, then point an isolated kpip installation at it with the
global `--python` option:

```console
python -m venv .venv
kpip --python .venv install httpx
kpip --python .venv list
```

Install a requirements file:

```console
kpip --python .venv install -r requirements.txt
```

Resolve an input file into `pylock.toml`, then install it:

```console
kpip lock -r requirements.in
kpip --python .venv install -r pylock.toml
```

If kpip is installed inside the environment it should manage, omit
`--python .venv` and invoke `kpip` directly.

## Questions I'm still exploring

- How can kpip improve on uv's user experience? Where could commands,
  defaults, and error messages make installing packages easier to understand?
- What would a better developer experience look like, both for people using
  kpip in their workflows and for contributors working on its internals?
- Can the [project layout and architecture](https://github.com/KRRT7/xray#x-ray-performance-laboratory) make the code easier to navigate,
  change, and test while keeping it fast?
- Which improvements hold up across real dependency graphs, source builds,
  and different machines, beyond the workloads used to develop them?
- How much do the gains depend on a warm cache, and what happens on a first
  install or when cached data can no longer be reused?
- Which experiments can preserve pip's compatibility requirements, and how
  much of their benefit survives adapting them to pip's architecture?
- Where does a faster path add more complexity than its measured benefit
  justifies?

These questions guide the experiments. A useful result can be a faster
implementation, a tradeoff made clearer, or evidence that an idea should be
left behind.

## Commands

| Task | Commands |
| --- | --- |
| Install or prepare packages | `install`, `wheel`, `download` |
| Remove packages | `uninstall` |
| Inspect an environment | `list`, `freeze`, `show`, `inspect`, `check` |
| Resolve reproducibly | `lock` |
| Work with indexes and artifacts | `index`, `hash` |
| Inspect or clear local state | `cache` |

Run `kpip <command> --help` for command-specific options.

## Benchmarking

A fast microbenchmark is a lead, not a conclusion. Following the spirit of the
[X-Ray Performance Laboratory](https://github.com/KRRT7/xray), kpip treats
performance as a question to investigate:

- **Measure the whole workflow.** Less time in the resolver only helps if it
  improves the command the user runs.
- **Make comparisons reproducible.** Keep inputs, interpreters, target
  environments, and cache conditions comparable.
- **Keep the behavior.** An optimization that skips required work needs more
  work, even if the timing looks good.
- **Keep the negative results.** Knowing where an idea fails helps the next
  experiment.

The [Hyperfine](https://github.com/sharkdp/hyperfine) harness compares kpip and
uv using the same inputs and isolated targets. uv is an external performance
reference; before-and-after kpip runs measure individual changes. Results
depend on the workload, machine, and cache state. An eventual pip patch still
needs measurement in pip's own architecture and test environment.

The default offline workload is generated locally to avoid network variance.

With `hyperfine` and `uv` available on `PATH`:

```console
cd scripts/benchmark
uv sync --locked --group tests
uv run kpip-bench --workload offline
```

The harness includes startup, cold and warm locking, cold and warm
installation, and incremental installation cases. It can also run the
workloads used by uv's public benchmarks, but those are opt-in because live
indexes and platform-specific wheels make them less reproducible.

See the [benchmark guide](scripts/benchmark/README.md) for workload selection,
recording quiet-machine baselines, exporting raw Hyperfine results, and
comparing two commits.

### Evaluating user and developer experience

I want to evaluate usability with concrete tasks, too. For each experiment,
record the starting conditions, the intended outcome, and where someone gets
stuck. Compare the same task before and after the change; when comparing with
uv, use equivalent inputs and goals.

| Question | Evidence to collect |
| --- | --- |
| Does an error help someone recover? | Whether they can identify the cause and complete the next step; time, failed attempts, and documentation lookups needed. |
| Is a common workflow simpler? | Commands, manual edits, and retries needed to reach the same correct result. |
| Can a contributor find and change the relevant code? | Time to locate the implementation, make a small change, and find and run the relevant tests; wrong turns and help needed. |

Record participants' familiarity with the tools and keep their feedback
alongside the counts and timings. Fewer commands only help if people can
understand what those commands do. These are criteria for future experiments;
the benchmark harness above measures runtime performance.

## Upstream is the destination

kpip is a workshop for improvements that can benefit pip. The intention is
for the useful implementation work, tests, and evidence to flow upstream,
rather than grow into a permanent, separate package-manager ecosystem.

That takes more than transferring commits. Each candidate improvement needs:

- a reproducible measurement of the problem and the improvement;
- behavioral and compatibility tests that preserve pip's contract;
- a focused implementation adapted to pip's architecture; and
- an honest account of tradeoffs and results that did not hold up.

An experiment landing here is the beginning of that process. Adoption depends
on whether it can meet pip's compatibility and maintenance needs.

## Design

The main path is intentionally layered:

```text
CLI -> resolution -> candidate discovery -> artifact preparation -> transaction
```

Each layer owns one part of the package-installation process. Fast paths are
narrow recognizers that decline to the general implementation whenever they
cannot preserve the same semantics. Persistent caches are optional: a missing,
stale, or corrupt entry must become a cache miss rather than a correctness
failure.

The [architecture guide](docs/architecture.md) maps these boundaries, the
runtime dependency rules between packages, the resolver flow, and every
persistent cache.

## Development

Contributions can start with a confusing workflow or a result that challenges
an assumption here. Useful starting points include:

- **A workflow that trips you up.** Share what you wanted to do, the commands
  you tried, the output, and what you expected. Explain where you needed to
  leave the CLI to look for help.
- **A slow install others can reproduce.** Include a minimal requirements
  file, the exact command, tool and Python versions, platform, cache conditions,
  and raw timings. The [benchmark guide](scripts/benchmark/README.md) explains
  how to record comparable runs.
- **An experiment that questions the design.** State the assumption, describe
  how you tested it, and share the results—including regressions or an idea
  that made no measurable difference.

Bring a concrete example to an issue or a pull request. Reports and
measurements are useful contributions even without an implementation.

Set up the test and typing environments:

```console
git clone https://github.com/warpforgedotco/kpip.git
cd kpip
uv sync --locked --group test --group typing
```

Run the main local checks:

```console
uv run ruff check src tests conftest.py
uv run ruff format --check src tests conftest.py
uv run ty check src
uv run pytest tests \
  --ignore=tests/cli/functional \
  --ignore=tests/benchmarks \
  -m "not network"
```

Functional tests exercise the real CLI in subprocesses:

```console
uv run pytest tests/cli/functional -n auto
```

The [checks workflow](.github/workflows/checks.yml) is the source of truth for
the supported CI matrix. Before proposing a performance change, record a
comparable before-and-after benchmark; a locally faster microbenchmark is not
enough on its own. A change is not finished merely because it lands in kpip:
identify how its implementation, tests, and evidence can move upstream.

## Why a courier snake?

Kip is a Python carrying a Python package. The job is to get it where it
belongs, quickly and intact.

Package installation makes the same promise. Resolving dependencies,
checking artifacts, and placing files correctly are all part of the delivery.
Kip is a reminder that a faster route still has to deliver the right package
to the right place. Every shortcut needs evidence that it preserves that
promise.

## Why the name kpip?

Naming it took a chat in the [Python Discord](https://discord.gg/python) and a
lot of occupied PyPI names.

The project started as **core-pip**. After [Ned](https://nedbatchelder.com) pointed out that the name
sounded like it came from Python's core developers, it became **cpip**—which
was already taken on PyPI. A naming session on August 31, 2026 followed: `piper`,
`pipy`, `cip`, `qpip`… one suggestion after another ran into an existing
package.

Then xelf suggested [**KPIP!**](https://discord.com/channels/267624335836053506/267624335836053506/1544063332800274552), after floating `krrpip`: a nod to **KRRT**, the
creator's handle, with `pip` keeping the purpose recognizable.

Short and personal: **kpip** stuck.

## Acknowledgements

kpip builds on the interfaces, behavior, and testing knowledge developed by
[pip and PyPA](https://github.com/pypa/pip), and learns from the techniques and
public workloads in [uv](https://github.com/astral-sh/uv). The
[X-Ray Performance Laboratory](https://github.com/KRRT7/xray) shares the spirit
behind the experiments here: curiosity, reproducible measurements, and a
willingness to be wrong.

Thank you to the Astral team for their work on uv and their contributions to
the Python ecosystem. Their work helped inspire the questions this project
explores. Thank you also for getting me into the
[Codex for OSS](https://openai.com/form/codex-for-oss/) program.

On a personal note, thank you to [Samuel Colvin](https://github.com/samuelcolvin)
for the motivation to work on kpip.

Third-party code shipped with kpip is documented in the [vendoring
manifest](src/kpip/_vendor/VENDORED.md).

## License

kpip is available under the [MIT License](LICENSE.txt).
