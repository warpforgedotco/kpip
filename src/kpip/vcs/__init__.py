"""Version control system support for kpip."""

# Each backend registers itself with ``versioncontrol.vcs`` when imported.
from kpip.vcs import bazaar, git, mercurial, subversion  # noqa: F401
