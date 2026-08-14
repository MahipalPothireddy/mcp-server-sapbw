"""Tests that the repositories actually USE the SQLite cache (not just that the cache works).

``test_cache.py`` covers the store in isolation. These tests cover the wiring, which is the part
that was missing: the cache class, the ``cache_get``/``cache_put`` helpers and the
``bw_refresh_cache`` tool all existed while no read path ever consulted them, so every call
re-queried the system and the tool cleared an always-empty store.

The property asserted here is the one that matters operationally: a second identical call issues
**no SQL at all**. Anything weaker (e.g. "the result is equal") would pass against no cache.

Fixtures are reused from ``test_transformations`` (synthetic names only).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp_server_sapbw.core.cache import SqliteCache
from mcp_server_sapbw.models.capability import CapabilityRecord
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from tests.test_transformations import ScriptedConnection, _capability


class CountingConnection:
    """Wraps the scripted connection and counts the statements executed."""

    def __init__(self) -> None:
        self._inner = ScriptedConnection()
        self.queries: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.queries.append(sql)
        return self._inner.execute_select(sql, parameters)

    @property
    def count(self) -> int:
        return len(self.queries)


def _cache(tmp_path: Path, *, fingerprint: str = "cap-1") -> SqliteCache:
    return SqliteCache(tmp_path / "cache.sqlite", system="qa", fingerprint=fingerprint)


def _repo(
    tmp_path: Path,
    *,
    connection: CountingConnection | None = None,
    cache: SqliteCache | None = None,
    capability: CapabilityRecord | None = None,
) -> tuple[TransformationsRepository, CountingConnection, SqliteCache]:
    conn = connection or CountingConnection()
    store = cache or _cache(tmp_path)
    repo = TransformationsRepository(conn, capability or _capability(), store)
    return repo, conn, store


# --- routine source: the largest table the server reads ------------------------------------


def test_routine_code_second_call_issues_no_sql(tmp_path: Path) -> None:
    repo, conn, cache = _repo(tmp_path)
    first = repo.get_routine_code("TRANSFORM01")
    assert not isinstance(first, UnsupportedResult)
    assert conn.count > 0
    after_first = conn.count

    # A fresh repository shares the cache but not the in-process memo.
    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    second = repo2.get_routine_code("TRANSFORM01")
    assert conn2.count == 0, "cached routine source must not re-read RSAABAP"
    assert not isinstance(second, UnsupportedResult)
    assert [code.code_id for code in second] == [code.code_id for code in first]
    assert [code.lines for code in second] == [code.lines for code in first]
    assert after_first > 0


def test_routine_analysis_second_call_issues_no_sql(tmp_path: Path) -> None:
    repo, _conn, cache = _repo(tmp_path)
    first = repo.analyze_routines("TRANSFORM01")
    assert not isinstance(first, UnsupportedResult)

    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    second = repo2.analyze_routines("TRANSFORM01")
    assert conn2.count == 0, "cached routine analysis must not re-read or re-parse"
    assert not isinstance(second, UnsupportedResult)
    assert [a.code_id for a in second] == [a.code_id for a in first]
    assert [len(a.table_dependencies) for a in second] == [len(a.table_dependencies) for a in first]


def test_transformation_second_call_issues_no_sql(tmp_path: Path) -> None:
    repo, _conn, cache = _repo(tmp_path)
    first = repo.get_transformation("TRANSFORM01")
    assert not isinstance(first, UnsupportedResult)

    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    second = repo2.get_transformation("TRANSFORM01")
    assert conn2.count == 0
    assert not isinstance(second, UnsupportedResult)
    assert second.model_dump() == first.model_dump()


# --- what must NOT be cached ---------------------------------------------------------------


def test_unsupported_result_is_not_cached(tmp_path: Path) -> None:
    """A release gap is capability state, not an extract. Caching it would outlive the gap."""
    cache = _cache(tmp_path)
    missing = _capability(present={"transformation"})  # routine_source absent
    repo, _conn, _ = _repo(tmp_path, cache=cache, capability=missing)
    assert isinstance(repo.get_routine_code("TRANSFORM01"), UnsupportedResult)

    # Same cache, same fingerprint, but the table is now present: must not serve the stale gap.
    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    result = repo2.get_routine_code("TRANSFORM01")
    assert not isinstance(result, UnsupportedResult)
    assert conn2.count > 0


def test_absent_object_is_not_cached(tmp_path: Path) -> None:
    """An object that does not exist yet may be transported later, so a miss is never cached."""
    repo, _conn, cache = _repo(tmp_path)
    first = repo.get_transformation("NOT_THERE")
    assert not isinstance(first, UnsupportedResult)
    assert first.active is False  # header row absent

    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    repo2.get_transformation("NOT_THERE")
    assert conn2.count > 0, "an empty header must be re-checked, not served from cache"


# --- invalidation --------------------------------------------------------------------------


def test_refresh_by_scope_forces_a_re_read(tmp_path: Path) -> None:
    repo, _conn, cache = _repo(tmp_path)
    repo.get_routine_code("TRANSFORM01")

    removed = cache.refresh("routine_code")
    assert removed == 1, "the object_type doubles as the bw_refresh_cache scope"

    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    repo2.get_routine_code("TRANSFORM01")
    assert conn2.count > 0


def test_refresh_all_forces_a_re_read(tmp_path: Path) -> None:
    repo, _conn, cache = _repo(tmp_path)
    repo.get_routine_code("TRANSFORM01")
    repo.get_transformation("TRANSFORM01")
    assert cache.refresh("all") >= 2

    repo2, conn2, _ = _repo(tmp_path, cache=cache)
    repo2.get_transformation("TRANSFORM01")
    assert conn2.count > 0


def test_capability_fingerprint_change_invalidates(tmp_path: Path) -> None:
    """After bw_refresh_capabilities the fingerprint changes, so extracts are re-read."""
    repo, _conn, _ = _repo(tmp_path, cache=_cache(tmp_path, fingerprint="cap-A"))
    repo.get_routine_code("TRANSFORM01")

    repo2, conn2, _ = _repo(tmp_path, cache=_cache(tmp_path, fingerprint="cap-B"))
    repo2.get_routine_code("TRANSFORM01")
    assert conn2.count > 0


def test_no_cache_configured_still_works(tmp_path: Path) -> None:
    """The cache is optional: with none configured every call simply re-reads."""
    conn = CountingConnection()
    repo = TransformationsRepository(conn, _capability(), None)
    assert not isinstance(repo.get_routine_code("TRANSFORM01"), UnsupportedResult)
    first = conn.count
    repo2 = TransformationsRepository(conn, _capability(), None)
    assert not isinstance(repo2.get_routine_code("TRANSFORM01"), UnsupportedResult)
    assert conn.count > first
