"""Read-only connection layer — the load-bearing security piece.

Defense in depth, with the hard guarantee at the lowest layer (mission Rule 1):

1. **Statement-level guard (primary).** ``assert_read_only`` rejects anything whose first
   significant token is not ``SELECT`` or ``WITH``, and rejects multi-statement text, *before* the
   SQL ever reaches the driver. This is the guarantee the server relies on.
2. **Grant check (secondary, fail-closed).** When a profile sets ``read_only_user: true``, the pool
   verifies the connected user's effective privileges at connect time. It refuses the connection if
   any write privilege is present *or* if the privileges cannot be read at all — a mis-provisioned
   user or an unreadable privileges view must fail closed, never fall through.
3. **Secret scrubbing.** Host, user, and password are stripped from any error text before it leaves
   this layer (mission Rule 5).

The layer never exposes a way to run a non-``SELECT`` statement.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from typing import Any, Protocol

from .budget import charge_query
from .logging import get_logger, log_query, table_of
from .profiles import Profile

_LOG = get_logger("connection")

# A statement slower than this is logged at WARNING even when the level would hide it: on someone
# else's landscape the slow query is the thing you need to see without turning on debug logging.
DEFAULT_SLOW_QUERY_MS = 5_000.0

# --- Statement-level guard ---------------------------------------------------------------

_ALLOWED_LEADING_VERBS = frozenset({"SELECT", "WITH"})
_LEADING_VERB_RE = re.compile(r"([A-Za-z]+)")

# Effective-privilege query used by the grant check. This is itself a SELECT.
_PRIVILEGE_QUERY = "SELECT PRIVILEGE FROM SYS.EFFECTIVE_PRIVILEGES WHERE USER_NAME = CURRENT_USER"

# Whole-word markers that indicate a write/DDL/admin privilege (checked per privilege token).
_WRITE_PRIVILEGE_MARKERS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "EXECUTE",
        "CREATE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "IMPORT",
        "REPLACE",
        "MERGE",
        "GRANT",
        "REVOKE",
        "LOAD",
        "UNLOAD",
        "ADMIN",
        "WRITE",
        "MODIFY",
    }
)


class ReadOnlyViolation(Exception):
    """A non-SELECT statement was attempted, or the read-only grant check failed closed."""


class ConnectionFailure(Exception):
    """A connection could not be established (message is scrubbed of secrets)."""


class QueryError(Exception):
    """A query failed at the driver (message is scrubbed of secrets)."""


# --- Connection-loss detection -----------------------------------------------------------
#
# A long analysis run does not fit in one database session: the server closed the session ~34
# minutes into a whole-system documentation generation, which loses the run rather than slowing it.
# Retrying is only correct for a *transport* failure, though. A rejected column name or a missing
# table is deterministic, and retrying it would burn the same time again to reach the same answer,
# so only these signatures are treated as retryable.
_CONNECTION_LOST_MARKERS = frozenset(
    {
        "forcibly closed",  # observed live: HANA closed an idle/long-running session
        "connection reset",
        "connection closed",
        "connection to the server was lost",
        "connection lost",
        "lost connection",
        "broken pipe",
        "not connected",
        "no connection",
        "socket closed",
        "session not connected",
        "cannot send data",
        "receive failed",
        "communication link failure",
        "-10709",  # hdbcli: connection failed
        "-10807",  # hdbcli: connection down / sqldbc
        "-10108",  # hdbcli: session has been terminated
    }
)
# Attempts after the first failure. Two is enough to cross a server-side session recycle without
# masking a host that is genuinely down.
_MAX_RECONNECT_ATTEMPTS = 2


def is_connection_lost(error: BaseException) -> bool:
    """True when an error looks like the transport dropped rather than the query being wrong."""
    text = str(error).lower()
    return any(marker in text for marker in _CONNECTION_LOST_MARKERS)


def _strip_leading_noise(sql: str) -> str:
    """Remove leading whitespace and leading SQL comments (line and block)."""
    current = sql
    while True:
        stripped = current.lstrip()
        if stripped.startswith("--"):
            newline = stripped.find("\n")
            current = "" if newline == -1 else stripped[newline + 1 :]
            continue
        if stripped.startswith("/*"):
            end = stripped.find("*/")
            current = "" if end == -1 else stripped[end + 2 :]
            continue
        return stripped


def assert_read_only(sql: str) -> None:
    """Raise :class:`ReadOnlyViolation` unless ``sql`` is a single SELECT/WITH statement.

    This is the primary, always-on guarantee. It runs before the driver sees the SQL.
    """
    body = sql.strip()
    if not body:
        raise ReadOnlyViolation("empty statement is not allowed")

    # Reject multiple statements. A single optional trailing ';' is tolerated.
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        raise ReadOnlyViolation("multiple statements are not allowed")

    cleaned = _strip_leading_noise(body)
    match = _LEADING_VERB_RE.match(cleaned)
    if match is None:
        raise ReadOnlyViolation("statement has no recognizable leading keyword")
    verb = match.group(1).upper()
    if verb not in _ALLOWED_LEADING_VERBS:
        raise ReadOnlyViolation(
            f"only SELECT/WITH statements are permitted; refused a '{verb}' statement"
        )


# --- Secret scrubbing --------------------------------------------------------------------


class SecretScrubber:
    """Replaces known sensitive strings (host, user, password) with ``<redacted>``."""

    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first so overlapping secrets are fully masked.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    @classmethod
    def for_profile(cls, profile: Profile) -> SecretScrubber:
        return cls([profile.host, profile.user, profile.password.get_secret_value()])


# --- Driver protocols (structural; hdbcli and test fakes both satisfy these) --------------


class RawCursor(Protocol):
    def execute(self, operation: str, parameters: Sequence[Any] | None = ...) -> Any: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...

    def close(self) -> None: ...


class RawConnection(Protocol):
    def cursor(self) -> RawCursor: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[Profile], RawConnection]


# --- Grant check (secondary, fail-closed) ------------------------------------------------


def verify_read_only_grants(connection: RawConnection, *, profile_name: str) -> None:
    """Fail closed unless the connected user is confirmed to hold only read privileges.

    Raises :class:`ReadOnlyViolation` if a write privilege is found, or if the effective
    privileges cannot be read (unreadable view, query error, or empty result). The statement-level
    guard remains the primary protection; this is an additional connect-time gate.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(_PRIVILEGE_QUERY)
        rows = cursor.fetchall()
    except Exception as exc:
        raise ReadOnlyViolation(
            f"cannot verify read-only status for profile '{profile_name}' "
            "(effective-privileges query failed); failing closed"
        ) from exc
    finally:
        with suppress(Exception):
            cursor.close()

    if not rows:
        raise ReadOnlyViolation(
            f"cannot verify read-only status for profile '{profile_name}' "
            "(no effective privileges returned); failing closed"
        )

    write_privileges: set[str] = set()
    for row in rows:
        privilege = str(row[0]).upper()
        words = {word for word in re.split(r"[^A-Z]+", privilege) if word}
        if words & _WRITE_PRIVILEGE_MARKERS:
            write_privileges.add(privilege.strip())

    if write_privileges:
        listed = ", ".join(sorted(write_privileges))
        raise ReadOnlyViolation(
            f"profile '{profile_name}' user holds write privileges ({listed}); "
            "refusing connection (read_only_user=true)"
        )


# --- Connection wrapper and pool ---------------------------------------------------------


class _Lease:
    """One checked-out raw connection. ``dead`` marks it for disposal instead of reuse."""

    __slots__ = ("dead", "raw")

    def __init__(self, raw: RawConnection) -> None:
        self.raw = raw
        self.dead = False


class _RawConnectionPool:
    """A bounded set of interchangeable raw connections for one profile.

    Why this exists: a single raw connection cannot be shared by concurrent callers. MCP tool
    functions are synchronous, so the server runs them in a worker threadpool — two tool calls
    against the same system genuinely execute at the same time, and driver connections are not
    safe for simultaneous use from several threads. Interleaved cursors on one connection produce
    wrong rows or protocol errors rather than an honest failure, which is the worst outcome for a
    server whose whole value is trustworthy answers.

    Every connection here is created by the same vetted factory as the first one, so each has
    passed the fail-closed grant check. Growth is lazy and bounded: a single analyst issuing one
    call at a time keeps using exactly one connection, and a HANA session is only ever spent when
    concurrency actually demands it.
    """

    def __init__(
        self,
        initial: RawConnection,
        *,
        create: Callable[[], RawConnection] | None,
        max_size: int,
    ) -> None:
        # Without a factory the pool cannot grow, so it degrades to serialising the one connection
        # it was given (which is correct, just not concurrent).
        self._create = create
        self._max_size = max(1, int(max_size)) if create is not None else 1
        self._capacity = threading.Semaphore(self._max_size)
        self._lock = threading.Lock()
        self._idle: list[RawConnection] = [initial]
        self._created: list[RawConnection] = [initial]

    @property
    def max_size(self) -> int:
        return self._max_size

    def checkout(self) -> _Lease:
        """Take an idle connection, or open one if the pool is below its ceiling."""
        self._capacity.acquire()
        try:
            with self._lock:
                if self._idle:
                    return _Lease(self._idle.pop())
            if self._create is None:  # pragma: no cover - max_size is 1 without a factory
                raise ConnectionFailure("connection pool is exhausted and cannot grow")
            raw = self._create()
            with self._lock:
                self._created.append(raw)
            return _Lease(raw)
        except BaseException:
            self._capacity.release()  # never strand a permit
            raise

    def checkin(self, lease: _Lease) -> None:
        """Return a connection for reuse, or dispose of it when the lease is marked dead."""
        try:
            if lease.dead:
                self._dispose(lease.raw)
                return
            with self._lock:
                self._idle.append(lease.raw)
        finally:
            self._capacity.release()

    def replace(self, lease: _Lease) -> bool:
        """Swap a dropped connection for a fresh, fully vetted one. False when impossible."""
        if self._create is None:
            return False
        self._dispose(lease.raw)
        try:
            raw = self._create()
        except Exception:
            # Includes ReadOnlyViolation from the re-run grant check. The caller reports the
            # original transport failure instead, so the reason the query failed stays visible.
            lease.dead = True
            return False
        with self._lock:
            self._created.append(raw)
        lease.raw = raw
        lease.dead = False
        return True

    def _dispose(self, raw: RawConnection) -> None:
        with suppress(Exception):
            raw.close()
        with self._lock:
            for index, existing in enumerate(self._created):
                if existing is raw:
                    del self._created[index]
                    break

    def close(self) -> None:
        with self._lock:
            connections = list(self._created)
            self._created.clear()
            self._idle.clear()
        for raw in connections:
            with suppress(Exception):
                raw.close()


class ReadOnlyConnection:
    """Wraps raw connections so every query passes the statement guard before the driver.

    Reconnects transparently when the transport drops. ``reopen`` must reproduce the *full* connect
    path including the read-only grant check — otherwise a reconnect would be a way to end up on a
    connection that never passed the gate, which is the one thing this layer exists to prevent.

    Concurrent callers are served from a bounded pool (:class:`_RawConnectionPool`) rather than a
    shared connection, so simultaneous tool calls never interleave cursors on one session.
    """

    def __init__(
        self,
        raw: RawConnection,
        *,
        scrubber: SecretScrubber,
        reopen: Callable[[], RawConnection] | None = None,
        pool_size: int = 1,
        slow_query_ms: float = DEFAULT_SLOW_QUERY_MS,
    ) -> None:
        self._scrubber = scrubber
        self._reopen = reopen
        self._pool = _RawConnectionPool(raw, create=reopen, max_size=pool_size)
        self._counter_lock = threading.Lock()
        self._slow_query_ms = slow_query_ms
        self.reconnect_count = 0

    @property
    def pool_size(self) -> int:
        """Maximum concurrent connections this wrapper will open for its profile."""
        return self._pool.max_size

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        """Run a read-only query. Raises :class:`ReadOnlyViolation` for anything not SELECT/WITH.

        Charged against the active query budget (if any) before the statement runs, so a runaway
        tool call stops at a documented bound instead of holding a session indefinitely.
        """
        assert_read_only(sql)  # BEFORE the driver — the load-bearing check
        charge_query()  # bound the call before spending a session on it
        bound = list(parameters) if parameters is not None else []
        attempts = _MAX_RECONNECT_ATTEMPTS if self._reopen is not None else 0
        lease = self._pool.checkout()
        started = time.monotonic()
        try:
            for attempt in range(attempts + 1):
                try:
                    rows = self._run(lease.raw, sql, bound)
                except QueryError as exc:
                    retryable = is_connection_lost(exc) and attempt < attempts
                    if not retryable:
                        raise
                    if not self._pool.replace(lease):
                        raise
                    with self._counter_lock:
                        self.reconnect_count += 1
                    _LOG.warning("reconnected after transport loss attempt=%d", attempt + 1)
                else:
                    log_query(
                        _LOG,
                        table=table_of(sql),
                        elapsed_ms=(time.monotonic() - started) * 1000.0,
                        rows=len(rows),
                        slow_ms=self._slow_query_ms,
                    )
                    return rows
            raise AssertionError("unreachable")  # pragma: no cover - loop returns or raises
        finally:
            self._pool.checkin(lease)

    def _run(self, raw: RawConnection, sql: str, bound: list[Any]) -> list[tuple[Any, ...]]:
        cursor = raw.cursor()
        try:
            cursor.execute(sql, bound)
            return cursor.fetchall()
        except Exception as exc:
            raise QueryError(self._scrubber.scrub(str(exc))) from None
        finally:
            with suppress(Exception):
                cursor.close()

    def close(self) -> None:
        self._pool.close()


def _default_factory(profile: Profile) -> RawConnection:
    """Open a real hdbcli connection. Imported lazily so tests need no driver/server."""
    import hdbcli.dbapi  # noqa: PLC0415 - lazy import keeps the module import-safe offline

    kwargs: dict[str, Any] = {
        "address": profile.host,
        "port": profile.port,
        "user": profile.user,
        "password": profile.password.get_secret_value(),
        "encrypt": profile.encrypt,
    }
    if profile.connect_timeout_seconds > 0:
        # Bounds the connect attempt so an unreachable host fails fast instead of hanging the tool
        # call. hdbcli expects milliseconds.
        kwargs["connectTimeout"] = int(profile.connect_timeout_seconds * 1000)
    if profile.communication_timeout_seconds > 0:
        # Driver-level inactivity bound. Best-effort and complementary to the query budget, which
        # is the guarantee: this depends on driver/server behaviour, the budget does not.
        kwargs["communicationTimeout"] = int(profile.communication_timeout_seconds * 1000)
    if profile.encrypt:
        # Certificate validation is on by default; a trust store or explicit opt-out (for
        # internal/self-signed hosts) is set per profile. The channel stays encrypted either way.
        kwargs["sslValidateCertificate"] = profile.ssl_validate_certificate
        if profile.ssl_trust_store:
            kwargs["sslTrustStore"] = profile.ssl_trust_store
    connection: RawConnection = hdbcli.dbapi.connect(**kwargs)
    return connection


class ReadOnlyConnectionPool:
    """Lazily creates and reuses one read-only connection per profile.

    The connection factory is injectable for offline testing. On first connect for a profile with
    ``read_only_user: true``, the grant check runs and the connection is refused (fail closed) if it
    cannot be confirmed read-only.
    """

    def __init__(self, factory: ConnectionFactory | None = None) -> None:
        self._factory = factory or _default_factory
        self._connections: dict[str, ReadOnlyConnection] = {}
        self._lock = threading.Lock()

    def acquire(self, profile: Profile) -> ReadOnlyConnection:
        with self._lock:
            existing = self._connections.get(profile.name)
            if existing is not None:
                return existing

            scrubber = SecretScrubber.for_profile(profile)

            def _connect(profile: Profile = profile) -> RawConnection:
                """The whole connect path, including the fail-closed grant check.

                Shared by the first connect and every reconnect so the two can never diverge: a
                reconnect that skipped the grant check would be a hole in the read-only guarantee.
                """
                raw = self._factory(profile)
                if profile.read_only_user:
                    try:
                        verify_read_only_grants(raw, profile_name=profile.name)
                    except ReadOnlyViolation:
                        with suppress(Exception):
                            raw.close()
                        raise
                return raw

            try:
                raw = _connect()
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                raise ConnectionFailure(
                    f"could not connect to profile '{profile.name}': " + scrubber.scrub(str(exc))
                ) from None

            connection = ReadOnlyConnection(
                raw,
                scrubber=scrubber,
                reopen=_connect,
                pool_size=profile.pool_size,
                slow_query_ms=profile.slow_query_ms,
            )
            _LOG.info(
                "connected profile=%s pool_size=%d read_only_asserted=%s",
                profile.name,
                profile.pool_size,
                profile.read_only_user,
            )
            self._connections[profile.name] = connection
            return connection

    def close_all(self) -> None:
        with self._lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()
