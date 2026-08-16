"""Where the server writes on disk.

The cache holds extracted metadata — ABAP routine source, query definitions, object names — which
is customer intellectual property at rest. Where that lands matters for a deployed install, and the
previous default (a ``cache/`` directory relative to the current working directory) was wrong in
three ways: it depends on how the client happened to launch the process, it can drop customer
metadata inside an unrelated repository, and it is invisible to anyone auditing the machine.

Resolution order, first match wins:

1. ``SAPBW_CACHE_DIR`` — explicit operator control, which is what a managed install needs.
2. The platform's per-user cache location (``%LOCALAPPDATA%`` on Windows, ``XDG_CACHE_HOME`` or
   ``~/.cache`` elsewhere), under a named subdirectory.

Per-user rather than system-wide is deliberate: the cache inherits the reach of the credentials
that filled it, so it must not be readable by other accounts on a shared host.

No new dependency: the two platform conventions are short enough to implement directly, and adding
a package to compute one path is not worth the supply-chain surface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .identity import storage_key

APP_NAME = "mcp-server-sapbw"
CACHE_DIR_ENV = "SAPBW_CACHE_DIR"


def _platform_cache_root(env: dict[str, str]) -> Path:
    if sys.platform == "win32":
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        if base:
            return Path(base) / APP_NAME / "cache"
        return Path.home() / "AppData" / "Local" / APP_NAME / "cache"
    xdg = env.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".cache" / APP_NAME


def cache_dir(env: dict[str, str] | None = None) -> Path:
    """The directory the per-profile cache files live in (not created here)."""
    source = env if env is not None else dict(os.environ)
    explicit = source.get(CACHE_DIR_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return _platform_cache_root(source)


def cache_file(
    system: str,
    env: dict[str, str] | None = None,
    *,
    tenant: str | None = None,
    directory: Path | None = None,
) -> Path:
    """The cache file for one profile. Named from the profile identity, never from a host.

    ``directory`` overrides the resolved cache root, which is what lets the runtime call this
    rather than assembling the path itself. It used to do the latter, and so bypassed the sanitising
    function performs - a profile alias containing ``..`` escaped the cache root entirely. The
    guarantee only holds if there is one way to get a path, so this is it.
    """
    root = directory if directory is not None else cache_dir(env)
    return root / f"{storage_key(system, tenant)}.sqlite"
