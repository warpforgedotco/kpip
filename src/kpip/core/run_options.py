"""pip's general options, as the running command was given them.

Every command takes them, and what they decide -- how the network is
reached, where the cache is, whether to prompt -- is read where it is
needed rather than threaded through each command's own arguments.
"""

from __future__ import annotations


class RunOptions:
    __slots__ = (
        "cache_dir",
        "cert",
        "check_build_dependencies",
        "client_cert",
        "debug",
        "exists_action",
        "isolated",
        "keyring_provider",
        "no_cache_dir",
        "no_clean",
        "no_input",
        "proxy",
        "resume_retries",
        "retries",
        "timeout",
        "trusted_hosts",
    )

    def __init__(self) -> None:
        self.cache_dir: str | None = None
        self.no_cache_dir = False
        self.cert: str | None = None
        self.client_cert: str | None = None
        self.proxy: str | None = None
        self.retries: int | None = None
        self.resume_retries: int | None = None
        self.timeout: float | None = None
        self.trusted_hosts: tuple[str, ...] = ()
        self.no_input = False
        self.keyring_provider: str | None = None
        self.exists_action: tuple[str, ...] = ()
        self.isolated = False
        self.debug = False
        # Not general options: the requirement commands take them, and the
        # builds they start read them here.
        self.check_build_dependencies = False
        self.no_clean = False


current = RunOptions()


def reset() -> None:
    """Forget the last command's options."""
    for name in RunOptions.__slots__:
        setattr(current, name, getattr(_DEFAULTS, name))


_DEFAULTS = RunOptions()
