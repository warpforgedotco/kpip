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
[![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.txt)

**pip, compiled, and made fast.**

> [!WARNING]
> kpip is an early-alpha experiment. It is not a supported pip distribution
> and should not be used to manage critical or system Python environments.
> Interfaces and behavior may change.

[Get started](#installation) · [How kpip relates to pip](#how-kpip-relates-to-pip) ·
[Benchmarks](#benchmarking) · [Contribute](#development)

## Why kpip exists

I started kpip out of curiosity about Python's performance limits. I wanted
to question the idea that you need Rust to write a "blazingly fast" program.
[uv](https://github.com/astral-sh/uv) gave me a concrete target: could I make
pip's familiar workflow that fast in Python?

## Installation

kpip is a single binary, for Linux and Windows, with its own Python inside:
it needs no Python to run, only one to install into. It is not installed
with pip.

Download `kpip-Linux` or `kpip-Windows` from the latest successful
[Compile run](https://github.com/warpforgedotco/kpip/actions/workflows/compile.yml?query=branch%3Amain)
on `main`, put the binary on your `PATH`, and on Linux make it executable:

```console
chmod +x kpip
kpip --version
```

To upgrade, replace the binary. To build it yourself:

```console
git clone https://github.com/warpforgedotco/kpip.git
cd kpip/scripts/compile
uv run kpip-compile build    # writes build/kpip
```

### Which Python kpip installs for

Like pip, kpip installs into one Python's environment. `--python` names it --
an interpreter, or an environment's directory -- and may come before or after
the command. Without it, kpip uses the first of:

1. the active virtual environment (`VIRTUAL_ENV`);
2. the active conda environment (`CONDA_PREFIX`), unless it is conda's base;
3. a `.venv` in the current directory or the nearest one above it, or the
   environment the current directory is inside;
4. conda's base environment, when it is the active one;
5. the first `python3` or `python` on `PATH` that runs.

Everything kpip decides for that Python is that Python's: where packages go,
what is already installed, which wheels fit, how markers and
`Requires-Python` evaluate, the shebangs of scripts and the bytecode it
writes. Source distributions are built with it too. kpip learns all of this
by asking the interpreter once, and remembers the answer until the
interpreter or its search path changes.

With no Python at all, kpip can still resolve (`lock`, `download`, `wheel`
for wheels), using the Python it bundles; installing and building say that
no Python was found, and do nothing.

kpip reads pip's configuration files, cache and `PIP_*` environment
variables, as pip does: the site configuration file is the one in the
environment it installs for.

## Quick start

Create an environment, then point kpip at it -- or activate it, and leave
`--python` out:

```console
python -m venv .venv
kpip --python .venv install httpx
kpip --python .venv list
```

Install a requirements file:

```console
kpip --python .venv install -r requirements.txt
```

Resolve into a `pylock.toml` lock file, then install from it:

```console
kpip lock -r requirements.in
kpip --python .venv install -r pylock.toml
```

kpip's commands and options are pip's: `kpip <command> --help` lists them,
and [pip's documentation](https://pip.pypa.io/en/stable/) describes them.

## How kpip relates to pip

kpip is upstream pip, renamed to `kpip`, with a series of patches on top.
[`scripts/vendor_pip.py`](scripts/vendor_pip.py) generates `src/kpip` and
`tests` from pip's `main` at the commit in
[`tools/vendoring/pip-upstream.txt`](tools/vendoring/pip-upstream.txt) and
the patches in [`tools/vendoring/patches/pip`](tools/vendoring/patches/pip),
so kpip follows pip as pip changes.

The patches do what a compiled pip needs -- above all, installing for a
Python other than the one running it -- and make kpip faster. Each is meant
to be small enough, and measured well enough, to be offered to pip.

kpip is compiled with [Nuitka](https://github.com/Nuitka/Nuitka), taken from
its latest `develop` and patched too
([`tools/vendoring/patches/nuitka`](tools/vendoring/patches/nuitka)).

## Benchmarking

A fast microbenchmark is a lead, not a conclusion. Following the spirit of the
[X-Ray Performance Laboratory](https://github.com/KRRT7/xray).

[`benchmarks/`](benchmarks/README.md) holds the
[CodSpeed](https://codspeed.io) suite CI runs on every change: uv's own warm
resolver benchmarks (jupyter and airflow), replayed offline, plus installing,
unpacking and parsing.

The [Hyperfine](https://github.com/sharkdp/hyperfine) harness in
[`scripts/benchmark`](scripts/benchmark/README.md) compares kpip and uv from
the command line, with the same inputs and isolated targets. With `hyperfine`
and `uv` on `PATH`:

```console
cd scripts/benchmark
uv run kpip-bench --workload offline
```

It covers startup, cold and warm locking, cold and warm installation, and
incremental installation. The workloads uv's public benchmarks use are
opt-in, as live indexes make them less reproducible. Results depend on the
workload, the machine and the cache state.

## Development

```console
git clone https://github.com/warpforgedotco/kpip.git
cd kpip
uv sync --locked --group test
```

`src/kpip` and `tests` are generated: never edit them. To change kpip, make
the change in the `build/pip` checkout `vendor_pip.py sync` leaves, as a
commit, and turn the commits into patches:

```console
uv run scripts/vendor_pip.py sync --commit "$(cat tools/vendoring/pip-upstream.txt)"
# commit in build/pip, on its kpip branch
uv run scripts/vendor_pip.py export
uv run scripts/vendor_pip.py sync --commit "$(cat tools/vendoring/pip-upstream.txt)"
```

A bare `sync` moves to pip's latest `main`. [AGENTS.md](AGENTS.md) describes
the workflow, and the rules for code the compiled binary runs.

Run pip's unit and functional tests against kpip:

```console
uv run pytest tests/unit -n auto -m "not network"
uv run python -m kpip wheel -w tests/data/common_wheels --group test-common-wheels
uv run pytest tests/functional -n auto -m "not network"
```

The [checks workflow](.github/workflows/checks.yml) is the source of truth
for what CI runs. A performance change comes with a comparable
before-and-after benchmark; a locally faster microbenchmark is not enough on
its own.

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

kpip is [pip](https://github.com/pypa/pip): its code, its tests and the
knowledge of the PyPA maintainers who wrote them. It learns from the
techniques and public workloads of [uv](https://github.com/astral-sh/uv), and
is compiled with [Nuitka](https://github.com/Nuitka/Nuitka). The
[X-Ray Performance Laboratory](https://github.com/KRRT7/xray) shares the spirit
behind the experiments here: curiosity, reproducible measurements, and a
willingness to be wrong.

Thank you to [Damian Shaw](https://github.com/notatallshaw) for his work on
[nab](https://github.com/notatallshaw/nab), whose PubGrub resolver the first
kpip was built on.

Thank you to the [Astral team](https://github.com/astral-sh) for their work on uv and their contributions to
the Python ecosystem. Their work helped inspire the questions this project
explores. Thank you also for getting me into the
[Codex for OSS](https://openai.com/form/codex-for-oss/) program.

On a personal note, thank you to [Samuel Colvin](https://github.com/samuelcolvin)
for the motivation to work on kpip.

The libraries pip vendors, and so kpip ships, are listed in
[`src/kpip/_vendor/vendor.txt`](src/kpip/_vendor/vendor.txt).

## License

kpip is available under the [MIT License](LICENSE.txt). pip, which kpip is
built from, is also MIT-licensed.
