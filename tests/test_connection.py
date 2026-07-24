"""Tests for the read-only connection layer (B1) — the load-bearing security piece.

Covers the primary statement-level guard (refuses anything not SELECT/WITH, before the driver),
the secondary fail-closed grant check, and secret scrubbing.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from mcp_server_sapbw.core.connection import (
    ConnectionFailure,
    QueryError,
    ReadOnlyConnection,
    ReadOnlyConnectionPool,
    ReadOnlyViolation,
    SecretScrubber,
    assert_read_only,
    verify_read_only_grants,
)
from mcp_server_sapbw.core.profiles import Profile

# --- Test doubles -------------------------------------------------------------------------


class FakeCursor:
    def __init__(
        self, rows: list[tuple[Any, ...]] | None = None, raise_on_execute: Exception | None = None
    ) -> None:
        self.rows = rows if rows is not None else []
        self.raise_on_execute = raise_on_execute
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    def execute(self, operation: str, parameters: Any = None) -> None:
        self.executed.append((operation, parameters))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor | None = None) -> None:
        self._cursor = cursor if cursor is not None else FakeCursor()
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def make_profile(*, read_only_user: bool = True) -> Profile:
    return Profile(
        name="qa",
        host="secret-host.example.invalid",
        port=30015,
        user="ro_user",
        password=SecretStr("hunter2-secret"),
        abap_schema="auto",
        read_only_user=read_only_user,
    )


# --- Statement-level guard ----------------------------------------------------------------

ALLOWED = [
    "SELECT * FROM RSTRAN",
    "select 1 from dummy",
    "   SELECT 1",
    "-- leading comment\nSELECT 1",
    "/* block */ SELECT 1",
    "WITH cte AS (SELECT 1 FROM dummy) SELECT * FROM cte",
    "SELECT 1;",
]

REJECTED = [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "CREATE TABLE t (a INT)",
    "ALTER TABLE t ADD c INT",
    "TRUNCATE TABLE t",
    "MERGE INTO t USING s ON (1=1)",
    "CALL some_proc()",
    "GRANT SELECT ON t TO u",
    "EXEC something",
    "SELECT 1; DROP TABLE t",
    "",
    "   ",
    "-- only a comment",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_assert_read_only_allows(sql: str) -> None:
    assert_read_only(sql)  # must not raise


@pytest.mark.parametrize("sql", REJECTED)
def test_assert_read_only_rejects(sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(sql)


# --- ReadOnlyConnection.execute_select ----------------------------------------------------


def test_execute_select_returns_rows() -> None:
    cursor = FakeCursor(rows=[("a", 1), ("b", 2)])
    conn = ReadOnlyConnection(FakeConnection(cursor), scrubber=SecretScrubber([]))
    rows = conn.execute_select("SELECT k, v FROM t WHERE k = ?", ["a"])
    assert rows == [("a", 1), ("b", 2)]
    assert cursor.executed == [("SELECT k, v FROM t WHERE k = ?", ["a"])]


def test_write_attempt_refused_before_driver() -> None:
    """A write must be rejected by the guard without the cursor ever executing it."""
    cursor = FakeCursor()
    conn = ReadOnlyConnection(FakeConnection(cursor), scrubber=SecretScrubber([]))
    with pytest.raises(ReadOnlyViolation):
        conn.execute_select("DELETE FROM t")
    assert cursor.executed == []  # the driver was never reached


def test_driver_error_is_scrubbed() -> None:
    profile = make_profile()
    leaky = RuntimeError(
        "connect to secret-host.example.invalid failed for ro_user pw=hunter2-secret"
    )
    cursor = FakeCursor(raise_on_execute=leaky)
    conn = ReadOnlyConnection(FakeConnection(cursor), scrubber=SecretScrubber.for_profile(profile))
    with pytest.raises(QueryError) as excinfo:
        conn.execute_select("SELECT 1 FROM dummy")
    message = str(excinfo.value)
    assert "hunter2-secret" not in message
    assert "secret-host.example.invalid" not in message
    assert "ro_user" not in message
    assert "<redacted>" in message


# --- Grant check --------------------------------------------------------------------------


def test_grant_check_passes_for_read_only() -> None:
    conn = FakeConnection(FakeCursor(rows=[("SELECT",), ("CATALOG READ",)]))
    verify_read_only_grants(conn, profile_name="qa")  # must not raise


@pytest.mark.parametrize(
    "privilege",
    ["INSERT", "UPDATE", "DELETE", "EXECUTE", "CREATE ANY", "DROP", "DATA ADMIN"],
)
def test_grant_check_refuses_write_privilege(privilege: str) -> None:
    conn = FakeConnection(FakeCursor(rows=[("SELECT",), (privilege,)]))
    with pytest.raises(ReadOnlyViolation):
        verify_read_only_grants(conn, profile_name="qa")


def test_grant_check_fails_closed_on_empty() -> None:
    conn = FakeConnection(FakeCursor(rows=[]))
    with pytest.raises(ReadOnlyViolation):
        verify_read_only_grants(conn, profile_name="qa")


def test_grant_check_fails_closed_on_query_error() -> None:
    conn = FakeConnection(FakeCursor(raise_on_execute=RuntimeError("view not readable")))
    with pytest.raises(ReadOnlyViolation):
        verify_read_only_grants(conn, profile_name="qa")


# --- Pool ---------------------------------------------------------------------------------


def test_pool_returns_and_caches_connection() -> None:
    calls = 0

    def factory(_profile: Profile) -> FakeConnection:
        nonlocal calls
        calls += 1
        return FakeConnection(FakeCursor(rows=[("SELECT",)]))

    pool = ReadOnlyConnectionPool(factory=factory)
    profile = make_profile()
    first = pool.acquire(profile)
    second = pool.acquire(profile)
    assert first is second
    assert calls == 1


def test_pool_refuses_write_user_and_does_not_cache() -> None:
    raw = FakeConnection(FakeCursor(rows=[("SELECT",), ("INSERT",)]))

    pool = ReadOnlyConnectionPool(factory=lambda _p: raw)
    with pytest.raises(ReadOnlyViolation):
        pool.acquire(make_profile(read_only_user=True))
    assert raw.closed is True  # refused connection was closed
    # Nothing cached: a second attempt tries again (and fails again).
    with pytest.raises(ReadOnlyViolation):
        pool.acquire(make_profile(read_only_user=True))


def test_pool_skips_grant_check_when_flag_false() -> None:
    # Even a user with write grants is accepted when read_only_user is false, because the
    # statement-level guard remains the real protection.
    raw = FakeConnection(FakeCursor(rows=[("INSERT",)]))
    pool = ReadOnlyConnectionPool(factory=lambda _p: raw)
    conn = pool.acquire(make_profile(read_only_user=False))
    assert isinstance(conn, ReadOnlyConnection)
    # The guard still refuses writes on this connection.
    with pytest.raises(ReadOnlyViolation):
        conn.execute_select("DROP TABLE t")


def test_pool_scrubs_connection_error() -> None:
    def factory(_profile: Profile) -> FakeConnection:
        raise RuntimeError("cannot reach secret-host.example.invalid as ro_user")

    pool = ReadOnlyConnectionPool(factory=factory)
    with pytest.raises(ConnectionFailure) as excinfo:
        pool.acquire(make_profile())
    message = str(excinfo.value)
    assert "secret-host.example.invalid" not in message
    assert "ro_user" not in message
    assert "<redacted>" in message


def test_secret_scrubber_longest_first() -> None:
    scrubber = SecretScrubber(["abc", "abcdef"])
    assert scrubber.scrub("abcdef and abc") == "<redacted> and <redacted>"
