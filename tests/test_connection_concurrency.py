"""Tests that concurrent callers never share a raw connection.

MCP tool functions are synchronous, so the server runs them in a worker threadpool: two tool calls
against the same system genuinely execute at once. A driver connection is not safe for simultaneous
use from several threads, and the failure mode is silent — interleaved cursors return wrong rows
rather than raising — so this is asserted directly rather than assumed.

The key test drives real threads through one ``ReadOnlyConnection`` and fails if any single raw
connection is ever inside ``execute`` twice at the same moment.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_server_sapbw.core.connection import (
    QueryError,
    ReadOnlyConnection,
    ReadOnlyConnectionPool,
    ReadOnlyViolation,
    SecretScrubber,
)
from mcp_server_sapbw.core.profiles import Profile

_WORKERS = 8
_CALLS_PER_WORKER = 12


class OverlapDetectingCursor:
    """Records an error if two cursors of the same connection are ever executing together."""

    def __init__(self, owner: OverlapDetectingConnection) -> None:
        self._owner = owner

    def execute(self, operation: str, parameters: Any = None) -> None:
        with self._owner.lock:
            self._owner.in_flight += 1
            if self._owner.in_flight > 1:
                self._owner.overlaps += 1
        # Hold the "query" open long enough that any real overlap is observed rather than missed.
        threading.Event().wait(0.001)
        with self._owner.lock:
            self._owner.in_flight -= 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [("ok",)]

    def close(self) -> None:
        return None


class OverlapDetectingConnection:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.overlaps = 0
        self.closed = False

    def cursor(self) -> OverlapDetectingCursor:
        return OverlapDetectingCursor(self)

    def close(self) -> None:
        self.closed = True


def _profile(**kw: Any) -> Profile:
    base: dict[str, Any] = {
        "name": "qa",
        "host": "host.invalid",
        "port": 30015,
        "user": "TESTER",
        "password": SecretStr("unused"),
        "read_only_user": False,
    }
    base.update(kw)
    return Profile(**base)


def _hammer(connection: ReadOnlyConnection) -> list[Any]:
    def worker() -> list[Any]:
        out = []
        for _ in range(_CALLS_PER_WORKER):
            out.append(connection.execute_select("SELECT 1 FROM DUMMY"))
        return out

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return [future.result() for future in [pool.submit(worker) for _ in range(_WORKERS)]]


def test_concurrent_queries_never_share_a_connection() -> None:
    """The regression: one shared connection with no serialisation around cursor execution."""
    created: list[OverlapDetectingConnection] = [OverlapDetectingConnection()]

    def reopen() -> OverlapDetectingConnection:
        created.append(OverlapDetectingConnection())
        return created[-1]

    connection = ReadOnlyConnection(
        created[0],
        scrubber=SecretScrubber([]),
        reopen=reopen,
        pool_size=4,
    )
    results = _hammer(connection)

    assert sum(len(r) for r in results) == _WORKERS * _CALLS_PER_WORKER
    assert all(row == [("ok",)] for batch in results for row in batch)
    assert [c.overlaps for c in created] == [0] * len(created), (
        "a raw connection was used by two threads at once"
    )


def test_pool_never_exceeds_its_ceiling() -> None:
    created: list[OverlapDetectingConnection] = [OverlapDetectingConnection()]

    def reopen() -> OverlapDetectingConnection:
        created.append(OverlapDetectingConnection())
        return created[-1]

    connection = ReadOnlyConnection(
        created[0], scrubber=SecretScrubber([]), reopen=reopen, pool_size=3
    )
    _hammer(connection)
    assert connection.pool_size == 3
    assert len(created) <= 3, "opened more sessions than the profile allows"


def test_single_caller_opens_exactly_one_connection() -> None:
    """Lazy growth: sequential use must not spend extra database sessions."""
    created: list[OverlapDetectingConnection] = [OverlapDetectingConnection()]

    def reopen() -> OverlapDetectingConnection:  # pragma: no cover - must not be called
        created.append(OverlapDetectingConnection())
        return created[-1]

    connection = ReadOnlyConnection(
        created[0], scrubber=SecretScrubber([]), reopen=reopen, pool_size=8
    )
    for _ in range(20):
        connection.execute_select("SELECT 1 FROM DUMMY")
    assert len(created) == 1


def test_without_a_reopen_factory_the_pool_serialises_one_connection() -> None:
    """Test doubles and read-only wrappers that cannot grow must still be safe, just serial."""
    raw = OverlapDetectingConnection()
    connection = ReadOnlyConnection(raw, scrubber=SecretScrubber([]))
    assert connection.pool_size == 1
    _hammer(connection)
    assert raw.overlaps == 0


def test_close_closes_every_connection_the_pool_opened() -> None:
    created: list[OverlapDetectingConnection] = [OverlapDetectingConnection()]

    def reopen() -> OverlapDetectingConnection:
        created.append(OverlapDetectingConnection())
        return created[-1]

    connection = ReadOnlyConnection(
        created[0], scrubber=SecretScrubber([]), reopen=reopen, pool_size=4
    )
    _hammer(connection)
    connection.close()
    assert all(c.closed for c in created), "a pooled session was left open"


def test_statement_guard_still_runs_before_checkout() -> None:
    """A refused statement must not even take a connection from the pool."""

    class Exploding:
        def cursor(self) -> Any:  # pragma: no cover - must never be reached
            raise AssertionError("the guard let a write through to the driver")

        def close(self) -> None:
            return None

    connection = ReadOnlyConnection(Exploding(), scrubber=SecretScrubber([]))
    with pytest.raises(ReadOnlyViolation):
        connection.execute_select("DELETE FROM T")


def test_query_error_returns_the_connection_to_the_pool() -> None:
    """A bad query is not a bad connection: the session must be reused, not leaked."""

    class FailingCursor:
        def execute(self, operation: str, parameters: Any = None) -> None:
            raise RuntimeError("invalid column name NOPE")

        def fetchall(self) -> list[tuple[Any, ...]]:  # pragma: no cover
            return []

        def close(self) -> None:
            return None

    class FailingConnection:
        def __init__(self) -> None:
            self.cursors = 0

        def cursor(self) -> FailingCursor:
            self.cursors += 1
            return FailingCursor()

        def close(self) -> None:
            return None

    raw = FailingConnection()
    connection = ReadOnlyConnection(raw, scrubber=SecretScrubber([]), pool_size=1)
    for _ in range(3):
        with pytest.raises(QueryError):
            connection.execute_select("SELECT NOPE FROM T")
    # Three attempts all found a connection available, so none was stranded by the failures.
    assert raw.cursors == 3


def test_profile_pool_size_is_honoured_by_the_pool() -> None:
    opened: list[OverlapDetectingConnection] = []

    def factory(profile: Profile) -> OverlapDetectingConnection:
        opened.append(OverlapDetectingConnection())
        return opened[-1]

    pool = ReadOnlyConnectionPool(factory)
    connection = pool.acquire(_profile(pool_size=2))
    assert connection.pool_size == 2
    _hammer(connection)
    assert all(c.overlaps == 0 for c in opened)
    assert len(opened) <= 2
    pool.close_all()


def test_pool_size_defaults_and_bounds() -> None:
    assert _profile().pool_size == 4  # sensible default: concurrency without session sprawl
    with pytest.raises(ValueError):
        _profile(pool_size=0)
    with pytest.raises(ValueError):
        _profile(pool_size=33)
