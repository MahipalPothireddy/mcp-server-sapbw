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
    is_connection_lost,
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


def test_scrubber_masks_endpoints_the_driver_volunteers() -> None:
    """A transport failure names addresses this process never configured.

    Verbatim from a real connection drop during a documentation run, with the addresses changed:
    hdbcli reports the *resolved* server address and the client's own address, neither of which is
    the profile's host string - so literal replacement of known secrets left both in the message
    that was returned to the caller and written to the log. Mission Rule 5 says connection details
    do not reach an error message, which a list of known strings cannot deliver on its own.
    """
    scrubber = SecretScrubber(["bw-host.invalid", "BWUSER", "s3cret"])
    message = (
        "(-10807, \"Connection down: [89006] System call 'recv' failed, rc=10054:"
        "An existing connection was forcibly closed by the remote host "
        '{198.51.100.10:62939 -> 203.0.113.67:30215 TenantName:(none) ConnectionID:329853}")'
    )
    scrubbed = scrubber.scrub(message)
    assert "198.51.100.10" not in scrubbed
    assert "203.0.113.67" not in scrubbed
    assert "62939" not in scrubbed and "30215" not in scrubbed
    assert scrubbed.count("<redacted-endpoint>") == 2
    # The diagnosable part survives: without the codes the message says only "it broke".
    assert "rc=10054" in scrubbed
    assert "-10807" in scrubbed
    assert "[89006]" in scrubbed
    assert "ConnectionID:329853" in scrubbed


def test_scrubber_still_masks_a_configured_host_that_is_not_an_address() -> None:
    """The pattern adds to the list, it does not replace it: a named host still gets masked."""
    scrubber = SecretScrubber(["bw-host.invalid", "BWUSER"])
    assert scrubber.scrub("connect to bw-host.invalid as BWUSER") == (
        "connect to <redacted> as <redacted>"
    )


# --- Reconnect on transport loss -----------------------------------------------------------
#
# A whole-system analysis run does not fit in one database session: the reference system closed
# the session ~34 minutes into a documentation generation. Retrying has to be limited to transport
# failures, and a
# reconnect must not become a way onto a connection that never passed the grant check.

_LOST = "Connection to the server was lost: forcibly closed by the remote host"


class FlakyConnection:
    """Fails the first ``fail_times`` executes with a connection-loss error, then succeeds."""

    def __init__(self, fail_times: int, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.remaining_failures = fail_times
        self.rows = rows if rows is not None else [("ok",)]
        self.closed = False
        self.execute_count = 0

    def cursor(self) -> Any:
        return _FlakyCursor(self)

    def close(self) -> None:
        self.closed = True


class _FlakyCursor:
    def __init__(self, owner: FlakyConnection) -> None:
        self._owner = owner

    def execute(self, operation: str, parameters: Any = None) -> None:
        self._owner.execute_count += 1
        if self._owner.remaining_failures > 0:
            self._owner.remaining_failures -= 1
            raise RuntimeError(_LOST)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._owner.rows)

    def close(self) -> None:
        return None


def test_connection_loss_is_distinguished_from_a_bad_query() -> None:
    assert is_connection_lost(RuntimeError(_LOST))
    assert is_connection_lost(RuntimeError("sql error -10709 connection failed"))
    # A deterministic SQL error must not be retried: the same query would fail the same way.
    assert not is_connection_lost(RuntimeError("invalid column name: OBJVERS"))
    assert not is_connection_lost(RuntimeError("invalid table name: RSNOSUCH"))


def test_dropped_session_reconnects_and_the_query_succeeds() -> None:
    dropped = FlakyConnection(fail_times=1, rows=[("recovered",)])
    replacement = FakeConnection(FakeCursor(rows=[("recovered",)]))

    def reopen() -> Any:
        return replacement

    conn = ReadOnlyConnection(dropped, scrubber=SecretScrubber([]), reopen=reopen)
    assert conn.execute_select("SELECT 1") == [("recovered",)]
    assert conn.reconnect_count == 1
    assert dropped.closed  # the dead connection is not left open


def test_reconnect_gives_up_rather_than_looping_forever() -> None:
    always_dead = FlakyConnection(fail_times=99)

    def reopen() -> Any:
        return FlakyConnection(fail_times=99)

    conn = ReadOnlyConnection(always_dead, scrubber=SecretScrubber([]), reopen=reopen)
    with pytest.raises(QueryError):
        conn.execute_select("SELECT 1")
    # Bounded: the initial attempt plus a fixed number of retries, not an unbounded loop.
    assert conn.reconnect_count == 2


def test_a_bad_query_is_not_retried() -> None:
    cursor = FakeCursor(raise_on_execute=RuntimeError("invalid column name: NOPE"))
    reopened = False

    def reopen() -> Any:
        nonlocal reopened
        reopened = True
        return FakeConnection()

    conn = ReadOnlyConnection(FakeConnection(cursor), scrubber=SecretScrubber([]), reopen=reopen)
    with pytest.raises(QueryError):
        conn.execute_select("SELECT NOPE FROM T")
    assert not reopened
    assert conn.reconnect_count == 0


def test_write_guard_still_runs_before_any_reconnect_path() -> None:
    """The statement guard is the load-bearing check and must not be reachable around."""
    conn = ReadOnlyConnection(
        FlakyConnection(fail_times=1), scrubber=SecretScrubber([]), reopen=FakeConnection
    )
    with pytest.raises(ReadOnlyViolation):
        conn.execute_select("DELETE FROM T")
    assert conn.reconnect_count == 0


def test_reconnect_reruns_the_grant_check_and_refuses_a_write_capable_user() -> None:
    """A reconnect must not be a way onto a connection that never passed the gate."""
    profile = make_profile(read_only_user=True)
    attempts: list[int] = []

    def factory(_: Profile) -> Any:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            # First connect: read-only, and its queries drop the session.
            return _GrantedThenFlaky(privileges=[("SELECT",)])
        # The reconnect lands on a user that now reports a write privilege.
        return FakeConnection(FakeCursor(rows=[("SELECT",), ("INSERT",)]))

    pool = ReadOnlyConnectionPool(factory=factory)
    conn = pool.acquire(profile)
    # The query fails: the transport dropped, and the reconnect was refused by the grant check.
    with pytest.raises(QueryError):
        conn.execute_select("SELECT 1 FROM DUMMY")
    assert len(attempts) == 2  # it did try to reconnect
    assert conn.reconnect_count == 0  # but the gate rejected it


class _GrantedThenFlaky:
    """Passes the grant check, then loses the session on the first real query."""

    def __init__(self, privileges: list[tuple[Any, ...]]) -> None:
        self._privileges = privileges
        self._grant_checked = False
        self.closed = False

    def cursor(self) -> Any:
        return _GrantedThenFlakyCursor(self)

    def close(self) -> None:
        self.closed = True


class _GrantedThenFlakyCursor:
    def __init__(self, owner: _GrantedThenFlaky) -> None:
        self._owner = owner
        self._is_grant_query = False

    def execute(self, operation: str, parameters: Any = None) -> None:
        if "EFFECTIVE_PRIVILEGES" in operation:
            self._is_grant_query = True
            self._owner._grant_checked = True
            return
        raise RuntimeError(_LOST)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._owner._privileges) if self._is_grant_query else []

    def close(self) -> None:
        return None
