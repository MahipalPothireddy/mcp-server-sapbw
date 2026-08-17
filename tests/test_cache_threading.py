"""Regression tests for D4: the SQLite extract cache must be usable from FastMCP worker threads.

**What went wrong.** ``SqliteCache`` opened its connection without ``check_same_thread=False``, so
Python's default thread check applied. One instance is shared for the life of the process and MCP
tool calls are dispatched across a worker-thread pool, so the first thread to touch the cache owned
it and every later call from a different thread raised
``sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same
thread``. Cache-touching tools (``bw_describe_object``, ``bw_analyze_object``, anything through
``cached_model``) failed nondeterministically over the real stdio transport while DB-only tools kept
working - which made it present as a BW connection fault rather than a local cache defect.

**Why the whole offline suite missed it.** ``FakeRuntime`` builds every repository with no cache at
all (``ProvidersRepository(_Conn(), cap)``), so 1,272 passing tests never opened a ``SqliteCache``.
The tool-level tests here inject a real one against a temporary file - the cache is deliberately
*not* mocked, because a mock is exactly what failed to catch this.

Every test below fails on the pre-fix code.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from mcp_server_sapbw import server
from mcp_server_sapbw.core.cache import SqliteCache
from mcp_server_sapbw.core.snapshots import SnapshotStore
from mcp_server_sapbw.models.errors import BwError, from_exception
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.repositories.hana import HanaRepository
from mcp_server_sapbw.repositories.health import HealthRepository
from mcp_server_sapbw.repositories.providers import ProvidersRepository
from mcp_server_sapbw.repositories.queries import QueriesRepository
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.services.analysis import AnalysisReaders, AnalysisService
from mcp_server_sapbw.services.lineage import LineageService
from mcp_server_sapbw.services.load_closure import LoadClosureService
from tests.test_server import FakeRuntime, _Conn

_SUBJECT = "SALES_DSO"  # synthetic, from the shared fixture landscape


def _on_thread(fn: Any, *args: Any) -> Any:
    """Run ``fn`` on a genuinely different thread and return its result (or raise its exception)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


# --- the cache itself ---------------------------------------------------------------------


def _cache(tmp_path: Path, **kwargs: Any) -> SqliteCache:
    return SqliteCache(tmp_path / "cache.sqlite", system="qa", fingerprint="fp1", **kwargs)


def test_a_cache_opened_on_one_thread_is_readable_from_another(tmp_path: Path) -> None:
    """The defect in one line. Pre-fix this raises sqlite3.ProgrammingError."""
    cache = _cache(tmp_path)
    cache.put("provider", "OBJ_A", '{"v":1}')

    assert _on_thread(cache.get, "provider", "OBJ_A") == '{"v":1}'


def test_every_connection_touching_path_works_from_another_thread(tmp_path: Path) -> None:
    """Not only ``get``. A fix that guarded reads alone would leave the writes raising."""
    cache = _cache(tmp_path)

    _on_thread(cache.put, "provider", "OBJ_B", '{"v":2}')
    assert cache.get("provider", "OBJ_B") == '{"v":2}'
    assert _on_thread(cache.entry_counts) == {"provider": 1}
    assert _on_thread(cache.refresh, "all") == 1
    assert cache.get("provider", "OBJ_B") is None


def test_expiry_eviction_works_from_another_thread(tmp_path: Path) -> None:
    """``get`` calls ``_delete`` on the expiry path: a nested acquisition needing a re-entrant lock.

    With a plain ``Lock`` instead of an ``RLock`` this deadlocks rather than fails, which is why the
    lock type is asserted by behaviour here and not left to review.
    """
    now = [1_000.0]
    cache = _cache(tmp_path, structural_ttl=10, clock=lambda: now[0])
    cache.put("provider", "OBJ_C", '{"v":3}')

    now[0] += 11  # past the TTL, so the read must evict
    assert _on_thread(cache.get, "provider", "OBJ_C") is None
    assert cache.entry_counts() == {}


def test_fingerprint_eviction_works_from_another_thread(tmp_path: Path) -> None:
    """The other nested ``get`` -> ``_delete`` path: a capability refresh changed the
    fingerprint."""
    path = tmp_path / "cache.sqlite"
    SqliteCache(path, system="qa", fingerprint="fp1").put("provider", "OBJ_D", '{"v":4}')

    later = SqliteCache(path, system="qa", fingerprint="fp2")
    assert _on_thread(later.get, "provider", "OBJ_D") is None
    assert later.entry_counts() == {}


def test_close_from_another_thread(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    _on_thread(cache.close)


def test_concurrent_readers_and_writers_on_distinct_keys(tmp_path: Path) -> None:
    """Deterministic stress: every thread owns its own keys, so the expected end state is exact.

    Distinct keys deliberately - the point is that the shared *connection* survives concurrent use,
    not that SQLite arbitrates a contended row. A barrier makes the threads overlap rather than
    politely queue, which is what the pre-fix code could not survive.
    """
    cache = _cache(tmp_path)
    threads, per_thread = 8, 25
    barrier = threading.Barrier(threads)
    errors: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            for i in range(per_thread):
                key = f"OBJ_{index}_{i}"
                cache.put("provider", key, f'{{"t":{index},"i":{i}}}')
                assert cache.get("provider", key) == f'{{"t":{index},"i":{i}}}'
                cache.entry_counts()
        except Exception as exc:
            with lock:
                errors.append(f"thread {index}: {type(exc).__name__}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    assert not errors, f"concurrent access failed: {errors[:3]}"
    assert cache.entry_counts() == {"provider": threads * per_thread}


def test_in_memory_cache_is_also_cross_thread_safe() -> None:
    """The ``:memory:`` path skips the mkdir branch, so it is worth covering separately."""
    cache = SqliteCache(":memory:", system="qa", fingerprint="fp1")
    cache.put("provider", "OBJ_E", '{"v":5}')
    assert _on_thread(cache.get, "provider", "OBJ_E") == '{"v":5}'


# --- the snapshot store, audited alongside ------------------------------------------------


def test_snapshot_store_is_cross_thread_safe(tmp_path: Path) -> None:
    """It already set ``check_same_thread=False``, which is why its race stayed invisible.

    Asserted rather than assumed: it holds the same shared-instance lifecycle and the same
    execute-then-commit-then-read-rowcount shape as the cache.
    """
    store = SnapshotStore(tmp_path / "snap.sqlite")

    assert _on_thread(store.count) == 0
    assert _on_thread(store.list) == []
    assert _on_thread(store.get, "nosuch") is None
    assert _on_thread(store.delete, "nosuch") is False
    _on_thread(store.close)


# --- tool level, against a real SqliteCache ----------------------------------------------


class _CachedRuntime(FakeRuntime):
    """``FakeRuntime`` with a **real** ``SqliteCache`` wired into the cache-touching readers.

    The stock fake passes no cache, so the cached code path is unreachable from the offline suite.
    One cache instance is shared across readers exactly as ``ServerRuntime`` shares it, because
    sharing is the precondition for the defect.
    """

    def __init__(self, cache_path: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._real_cache = SqliteCache(cache_path, system="qa", fingerprint="fp1")

    def providers(self, system: str) -> ProvidersRepository:
        return ProvidersRepository(_Conn(), self._cap, self._real_cache)

    def chains(self, system: str) -> ChainsRepository:
        return ChainsRepository(_Conn(), self._cap, self._real_cache)

    def analysis(self, system: str) -> AnalysisService:
        conn = _Conn()
        cache = self._real_cache
        return AnalysisService(
            AnalysisReaders(
                system=system,
                capability=self._cap,
                providers=ProvidersRepository(conn, self._cap, cache),
                lineage=LineageService(conn, self._cap, cache),
                transformations=TransformationsRepository(conn, self._cap, cache),
                queries=QueriesRepository(conn, self._cap, cache),
                chains=ChainsRepository(conn, self._cap, cache),
                load_closure=LoadClosureService(conn, self._cap, cache),
                health=HealthRepository(conn, self._cap, cache),
                hana=HanaRepository(conn, self._cap, cache),
                query_auth_exposure=lambda query: self.query_auth_exposure(system, query),
            )
        )


@pytest.fixture
def cached_runtime(tmp_path: Path) -> Any:
    runtime = _CachedRuntime(tmp_path / "tool-cache.sqlite")
    server.set_runtime(runtime)
    yield runtime
    server.set_runtime(FakeRuntime())  # restore the shared fixture for the rest of the suite


def _require_ok(result: Any, label: str) -> None:
    assert not isinstance(result, BwError), (
        f"{label} failed with {getattr(result, 'code', '?')} "
        f"{getattr(result, 'detail', {})} - a cache-touching tool must not depend on which "
        "worker thread the MCP layer happened to dispatch it to"
    )


def test_describe_object_survives_a_thread_change(cached_runtime: Any) -> None:
    """Thread A populates the cache; thread B must still be served. Pre-fix, B returns BwError."""
    _require_ok(server.bw_describe_object("qa", _SUBJECT), "describe on thread A")
    _require_ok(_on_thread(server.bw_describe_object, "qa", _SUBJECT), "describe on thread B")


def test_analyze_object_survives_a_thread_change(cached_runtime: Any) -> None:
    """The compound tool that failed over real stdio at every depth."""
    _require_ok(server.bw_describe_object("qa", _SUBJECT), "describe on thread A")
    _require_ok(_on_thread(server.bw_analyze_object, "qa", _SUBJECT), "analyze on thread B")


def test_two_different_threads_after_a_third_populates(cached_runtime: Any) -> None:
    """Three distinct threads, none of them the one that opened the cache."""
    _require_ok(_on_thread(server.bw_describe_object, "qa", _SUBJECT), "describe on thread A")
    _require_ok(_on_thread(server.bw_describe_object, "qa", _SUBJECT), "describe on thread B")
    _require_ok(_on_thread(server.bw_analyze_object, "qa", _SUBJECT), "analyze on thread C")


def test_a_db_only_tool_is_unaffected_by_the_thread(cached_runtime: Any) -> None:
    """Control. This passed throughout the incident and must keep passing.

    It is what made the defect look like a BW connection problem: the DB-backed calls were fine, so
    attention went to hdbcli instead of to the local cache.
    """
    _require_ok(server.bw_list_chains("qa", limit=1), "list_chains on thread A")
    _require_ok(_on_thread(lambda: server.bw_list_chains("qa", limit=1)), "list_chains on thread B")


# --- diagnostics --------------------------------------------------------------------------


def test_an_error_envelope_names_the_exception_module() -> None:
    """``ProgrammingError`` alone cannot distinguish the local cache from the BW session.

    Both sqlite3 and hdbcli raise a class of that name, and the reply naming only the class sent a
    live investigation after the BW connection while the defect was in the cache. The module is a
    library identifier, so it carries no credential, host, statement or object name.
    """
    failure = from_exception(sqlite3.ProgrammingError("boom"), tool="bw_analyze_object")

    assert failure.detail["exception"] == "ProgrammingError"  # unchanged, additive fix
    assert failure.detail["exception_module"] == "sqlite3"
    assert failure.detail["tool"] == "bw_analyze_object"
    # The generic message must stay generic: only scrubbed families forward their own text.
    assert "boom" not in failure.message


def test_exception_module_distinguishes_two_same_named_classes() -> None:
    class ProgrammingError(Exception):
        """A stand-in for hdbcli.dbapi.ProgrammingError, which is not importable offline."""

    sqlite_failure = from_exception(sqlite3.ProgrammingError("a"))
    other_failure = from_exception(ProgrammingError("b"))

    assert sqlite_failure.detail["exception"] == other_failure.detail["exception"]
    assert sqlite_failure.detail["exception_module"] != other_failure.detail["exception_module"]
    assert sqlite_failure.detail["exception_module"] == "sqlite3"
