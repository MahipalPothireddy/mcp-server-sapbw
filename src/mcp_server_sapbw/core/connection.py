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
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from typing import Any, Protocol

from .profiles import Profile

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


class ReadOnlyConnection:
    """Wraps a raw connection so every query passes the statement guard before the driver."""

    def __init__(self, raw: RawConnection, *, scrubber: SecretScrubber) -> None:
        self._raw = raw
        self._scrubber = scrubber

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        """Run a read-only query. Raises :class:`ReadOnlyViolation` for anything not SELECT/WITH."""
        assert_read_only(sql)  # BEFORE the driver — the load-bearing check
        cursor = self._raw.cursor()
        try:
            cursor.execute(sql, list(parameters) if parameters is not None else [])
            return cursor.fetchall()
        except Exception as exc:
            raise QueryError(self._scrubber.scrub(str(exc))) from None
        finally:
            with suppress(Exception):
                cursor.close()

    def close(self) -> None:
        with suppress(Exception):
            self._raw.close()


def _default_factory(profile: Profile) -> RawConnection:
    """Open a real hdbcli connection. Imported lazily so tests need no driver/server."""
    import hdbcli.dbapi  # noqa: PLC0415 - lazy import keeps the module import-safe offline

    connection: RawConnection = hdbcli.dbapi.connect(
        address=profile.host,
        port=profile.port,
        user=profile.user,
        password=profile.password.get_secret_value(),
        encrypt=profile.encrypt,
    )
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
            try:
                raw = self._factory(profile)
            except Exception as exc:
                raise ConnectionFailure(
                    f"could not connect to profile '{profile.name}': " + scrubber.scrub(str(exc))
                ) from None

            if profile.read_only_user:
                try:
                    verify_read_only_grants(raw, profile_name=profile.name)
                except ReadOnlyViolation:
                    with suppress(Exception):
                        raw.close()
                    raise

            connection = ReadOnlyConnection(raw, scrubber=scrubber)
            self._connections[profile.name] = connection
            return connection

    def close_all(self) -> None:
        with self._lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()
