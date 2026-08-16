"""Tests for cache location resolution.

The cache holds customer metadata at rest, so *where* it lands is a compliance question, not a
detail. The previous default resolved ``cache/`` against the current working directory — whatever
directory the MCP client happened to launch the process in, which could drop customer object names
inside an unrelated git repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_server_sapbw.core.paths import APP_NAME, cache_dir, cache_file


def test_explicit_env_var_wins() -> None:
    resolved = cache_dir({"SAPBW_CACHE_DIR": "/opt/sapbw/cache", "LOCALAPPDATA": "C:\\ignored"})
    assert resolved == Path("/opt/sapbw/cache")


def test_explicit_env_var_expands_user() -> None:
    resolved = cache_dir({"SAPBW_CACHE_DIR": "~/sapbw-cache"})
    assert "~" not in str(resolved)


def test_never_relative_to_the_working_directory() -> None:
    """The regression: a relative path put customer metadata wherever the client was launched."""
    resolved = cache_dir({})
    assert resolved.is_absolute()
    assert resolved != Path("cache").resolve()


def test_named_subdirectory_so_it_is_auditable() -> None:
    assert APP_NAME in str(cache_dir({}))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows convention")
def test_windows_uses_localappdata() -> None:
    resolved = cache_dir({"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"})
    assert resolved == Path("C:\\Users\\x\\AppData\\Local") / APP_NAME / "cache"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX convention")
def test_posix_honours_xdg_cache_home() -> None:
    assert cache_dir({"XDG_CACHE_HOME": "/tmp/xdg"}) == Path("/tmp/xdg") / APP_NAME


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX convention")
def test_posix_falls_back_to_dot_cache() -> None:
    assert cache_dir({}) == Path.home() / ".cache" / APP_NAME


def test_cache_file_leads_with_the_profile_alias() -> None:
    """The alias leads so the file is identifiable; a digest follows so it is unique.

    See `core.identity.storage_key` - the digest is what makes two similar aliases distinct, which
    plain sanitising did not.
    """
    path = cache_file("qa", {"SAPBW_CACHE_DIR": "/c"})
    assert path.parent == Path("/c")
    assert path.name.startswith("qa-")
    assert path.suffix == ".sqlite"


def test_profile_alias_cannot_escape_the_cache_directory() -> None:
    """A profile name is operator-supplied; it must not become a path traversal."""
    resolved = cache_file("../../etc/passwd", {"SAPBW_CACHE_DIR": "/c"})
    assert resolved.parent == Path("/c")
    assert ".." not in resolved.name


def test_empty_profile_alias_still_yields_a_file() -> None:
    assert cache_file("   ", {"SAPBW_CACHE_DIR": "/c"}).name.startswith("profile-")


def test_a_tenant_separates_two_customers_using_the_same_alias() -> None:
    """The isolation property: everybody calls their production system `prd`."""
    one = cache_file("prd", {"SAPBW_CACHE_DIR": "/c"}, tenant="acme")
    two = cache_file("prd", {"SAPBW_CACHE_DIR": "/c"}, tenant="globex")
    assert one != two
    assert one.name.startswith("acme-prd-")
    assert two.name.startswith("globex-prd-")


def test_the_directory_argument_is_what_the_runtime_uses() -> None:
    """The runtime assembled its own path and so skipped the sanitising this function does."""
    assert cache_file("qa", directory=Path("/elsewhere")).parent == Path("/elsewhere")
