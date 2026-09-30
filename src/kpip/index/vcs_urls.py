"""Reading a version-control URL, without the machinery for fetching one.

Split from ``kpip.index.vcs`` because the two halves are asked for at very
different rates. "Is this a VCS URL" is asked of every artifact a resolve
looks at, and the answer is almost always no; cloning one is asked for by
the rare requirement that names a repository. Keeping them together meant
the common question loaded ``shutil`` and ``tempfile`` to be answered by
string parsing.
"""

from __future__ import annotations

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
