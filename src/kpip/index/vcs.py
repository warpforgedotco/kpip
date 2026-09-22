"""Version-control URL parsing and source-tree materialization."""

from __future__ import annotations

import atexit
import threading
import os
import shutil
import tempfile
import urllib.parse

from kpip.index.source_models import VcsReference

VCS_SCHEMES = ("git", "hg", "svn", "bzr")


def vcs_scheme(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "+" not in parsed.scheme:
        if parsed.scheme in VCS_SCHEMES:
            return parsed.scheme
        return None
    vcs, _, _ = parsed.scheme.partition("+")
    return vcs or None


def vcs_reference(url: str) -> VcsReference:
    vcs = vcs_scheme(url)
    if vcs is None:
        raise OSError(f"Unsupported VCS URL: {url}")
    parsed_url = urllib.parse.urlparse(url)
    bare_url = parsed_url._replace(
        scheme=parsed_url.scheme.partition("+")[2] or parsed_url.scheme,
        fragment="",
    ).geturl()
    parsed = urllib.parse.urlsplit(bare_url)
    requested_revision = None
    path = parsed.path
    if "@" in path:
        path, requested_revision = path.rsplit("@", 1)
        if requested_revision == "":
            raise OSError(f"VCS URL has an empty revision: {url}")
    repo_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment),
    )
    if requested_revision is not None:
        requested_revision = urllib.parse.unquote(requested_revision)
    return VcsReference(
        vcs=vcs,
        repo_url=repo_url,
        requested_revision=requested_revision,
    )


_shared_checkouts: dict[str, str] = {}
"""VCS URL -> the checkout every caller in this process shares; see below."""

_shared_checkouts_lock = threading.Lock()


def _remove_shared_checkouts() -> None:
    with _shared_checkouts_lock:
        paths = list(_shared_checkouts.values())
        _shared_checkouts.clear()
        _announced_resolutions.clear()
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def release_checkout(path: str) -> None:
    """Give back a checkout from ``materialize_vcs``.

    A shared checkout stays for the next caller and is removed at exit; any
    other path is a private temporary and is removed now.
    """
    with _shared_checkouts_lock:
        shared = path in _shared_checkouts.values()
    if not shared:
        shutil.rmtree(path, ignore_errors=True)


def materialize_vcs(
    url: str,
    *,
    emit_resolution: bool = True,
    prompting: bool = True,
) -> str:
    """A checkout of ``url``, one per URL for the life of the process.

    A VCS requirement was cloned and built three times in one resolve: once
    to learn its name and version, once more for the same when its release
    was chosen, and once for its metadata, each caller removing its checkout
    when done.  Callers now share one checkout and hand it back with
    ``release_checkout``; the process removes it at exit.
    """
    with _shared_checkouts_lock:
        shared = _shared_checkouts.get(url)
    if shared is not None and os.path.isdir(shared):
        if emit_resolution:
            _announce_resolution(url, shared)
        return shared
    path = _clone_vcs(url, emit_resolution=False, prompting=prompting)
    with _shared_checkouts_lock:
        first = _shared_checkouts.get(url)
        if first is None or not os.path.isdir(first):
            # A caller that copied the checkout away and removed it (an
            # editable install) leaves a stale entry; the fresh clone
            # replaces it rather than being discarded for a path that is gone.
            _shared_checkouts[url] = path
            first = path
            if len(_shared_checkouts) == 1:
                atexit.register(_remove_shared_checkouts)
    if first is not path:
        shutil.rmtree(path, ignore_errors=True)
    if emit_resolution:
        _announce_resolution(url, first)
    return first


_announced_resolutions: set[str] = set()


def _announce_resolution(url: str, checkout: str) -> None:
    """Print ``Resolved <repo> to commit <sha>`` once per URL per process.

    The first clone of a URL is often the silent one that learns its
    candidate, so the message is printed for the first caller that asks
    for it, whichever clone it is served from.
    """
    if url in _announced_resolutions or os.environ.get("KPIP_QUIET"):
        return
    _announced_resolutions.add(url)
    reference = vcs_reference(url)
    print(f"Resolved {reference.repo_url} to commit {git_revision(checkout)}")


def _clone_vcs(
    url: str,
    *,
    emit_resolution: bool,
    prompting: bool,
) -> str:
    import subprocess

    reference = vcs_reference(url)
    if reference.vcs != "git":
        raise OSError(f"Unsupported VCS URL: {url}")
    target_text = tempfile.mkdtemp(prefix="kpip-index-vcs-")
    environment = os.environ.copy()
    if not prompting:
        environment["GIT_TERMINAL_PROMPT"] = "0"
    process = subprocess.run(
        ["git", "clone", reference.repo_url, target_text],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        shutil.rmtree(target_text, ignore_errors=True)
        raise OSError(f"Failed to clone {url}: {detail}")
    if reference.requested_revision is not None:
        process = subprocess.run(
            ["git", "checkout", "-q", reference.requested_revision],
            cwd=target_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            fetch = subprocess.run(
                ["git", "fetch", "-q", "origin", reference.requested_revision],
                cwd=target_text,
                text=True,
                capture_output=True,
                check=False,
            )
            if fetch.returncode == 0:
                process = subprocess.run(
                    ["git", "checkout", "-q", "FETCH_HEAD"],
                    cwd=target_text,
                    text=True,
                    capture_output=True,
                    check=False,
                )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()
            shutil.rmtree(target_text, ignore_errors=True)
            raise OSError(f"Failed to checkout {url}: {detail}")
    commit_id = git_revision(target_text)
    if emit_resolution and not os.environ.get("KPIP_QUIET"):
        print(f"Resolved {reference.repo_url} to commit {commit_id}")
    return target_text


def git_revision(source_dir: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def is_immutable_vcs_link(url: str) -> bool:
    if vcs_scheme(url) != "git":
        return False
    try:
        revision = vcs_reference(url).requested_revision
    except OSError:
        return False
    return bool(
        revision
        and len(revision) == 40
        and all(character in "0123456789abcdefABCDEF" for character in revision),
    )
