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

**Pip reimagined for performance.**

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

To lock for a Python version other than the one running kpip, including
versions below kpip's own 3.10 floor:

```console
kpip lock -r requirements.in --python-version 3.8
```

Markers, `Requires-Python` and wheel tags are all read for that version. The
platform is not: this resolves for another Python, not another machine.

A release whose dependencies the index does not publish is read from one of
its own wheels, and built only when no wheel of it offers usable metadata --
which a release that ships wheels can still come to. Building happens on the
interpreter running kpip, not the one being locked for, so a release that
cannot report its metadata here will fail the lock rather than be recorded
without its dependencies.

If kpip is installed inside the environment it should manage, omit
`--python .venv` and invoke `kpip` directly.

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
[X-Ray Performance Laboratory](https://github.com/KRRT7/xray).

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

## Upstream

kpip is meant to be upstreamed into pip. The intention is
for the useful implementation work, tests, and evidence to flow upstream.


## Development

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
to the right place.

## Why the name kpip?

Naming it took a chat in the [Python Discord](https://discord.gg/python) and a
lot of occupied PyPI names.

The project started as **core-pip**. After [Ned](https://nedbatchelder.com) pointed out that the name
sounded like it came from Python's core developers, it became **cpip**—which
was already taken on PyPI. A naming session on August 31, 2026 followed: `piper`,
`pipy`, `cip`, `qpip`… one suggestion after another ran into an existing
package.

Then xelf suggested [**KPIP!**](https://discord.com/channels/267624335836053506/267624335836053506/1544063332800274552), after floating `krrpip`: a nod to **KRRT7**, the
creator's Discord handle, with `pip` keeping the purpose recognizable.

Short and personal: **kpip** stuck.

## Acknowledgements

kpip builds on the interfaces, behavior, and testing knowledge developed by
[pip and PyPA](https://github.com/pypa/pip), and learns from the techniques and
public workloads in [uv](https://github.com/astral-sh/uv). The
[X-Ray Performance Laboratory](https://github.com/KRRT7/xray) shares the spirit
behind the experiments here: curiosity, reproducible measurements, and a
willingness to be wrong.

Thank you to [Damian Shaw](https://github.com/notatallshaw) for his work on
[nab](https://github.com/notatallshaw/nab). kpip uses its `nab-resolver`
component, a PubGrub dependency resolver written in Python, with local
adaptations documented in the [vendoring manifest](src/kpip/_vendor/VENDORED.md).
That foundation supports kpip's experiments across the whole installation
workflow, from startup and resolution to caching and installation.

Thank you to the [Astral team](https://github.com/astral-sh) for their work on uv and their contributions to
the Python ecosystem. Their work helped inspire the questions this project
explores. Thank you also for getting me into the
[Codex for OSS](https://openai.com/form/codex-for-oss/) program.

On a personal note, thank you to [Samuel Colvin](https://github.com/samuelcolvin)
for the motivation to work on kpip.

Third-party code shipped with kpip is documented in the [vendoring
manifest](src/kpip/_vendor/VENDORED.md).

## License

kpip is available under the [MIT License](LICENSE.txt).
