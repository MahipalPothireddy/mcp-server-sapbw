"""Per-profile SQLite metadata cache.

Metadata changes slowly and some extracts (``RSTRAN`` / ``RSZ*`` / ``RSAABAP``) are expensive on
large systems, so structural extracts are cached with a long TTL. Runtime statistics are never
cached beyond one hour (mission Section 3). Entries are invalidated automatically when the
capability fingerprint changes (e.g. after ``bw_refresh_capabilities``) and can be purged by scope
via ``bw_refresh_cache``.

The cache stores opaque string values (callers serialize their pydantic models to JSON). The cache
file lives under a git-ignored directory and never leaves the machine.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

CacheTier = Literal["structural", "runtime"]

_DEFAULT_STRUCTURAL_TTL = 86_400  # 24h
_RUNTIME_TTL_CAP = 3_600  # 1h hard cap for runtime statistics

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    system       TEXT NOT NULL,
    object_type  TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    tier         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    value        TEXT NOT NULL,
    extracted_at REAL NOT NULL,
    PRIMARY KEY (system, object_type, object_id, tier)
)
"""


class SqliteCache:
    """A local SQLite cache for one profile's extracted metadata.

    ``fingerprint`` ties entries to a capability record; when it changes, stale entries miss and are
    evicted. ``clock`` is injectable for testing TTL behavior.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        system: str,
        fingerprint: str,
        structural_ttl: int = _DEFAULT_STRUCTURAL_TTL,
        runtime_ttl: int = _RUNTIME_TTL_CAP,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._system = system
        self._fingerprint = fingerprint
        self._structural_ttl = int(structural_ttl)
        # Runtime statistics are hard-capped at one hour regardless of the configured value.
        self._runtime_ttl = min(int(runtime_ttl), _RUNTIME_TTL_CAP)
        self._clock = clock

        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @property
    def structural_ttl(self) -> int:
        """Seconds a structural extract is served for before it is re-read."""
        return self._structural_ttl

    @property
    def runtime_ttl(self) -> int:
        """Seconds a runtime statistic is served for (hard-capped at one hour)."""
        return self._runtime_ttl

    def entry_counts(self) -> dict[str, int]:
        """Entries per object type, for reporting what is at rest without reading any value."""
        rows = self._conn.execute(
            "SELECT object_type, COUNT(*) FROM cache_entries WHERE system = ? GROUP BY object_type",
            (self._system,),
        ).fetchall()
        return {str(object_type): int(count) for object_type, count in rows}

    def _ttl(self, tier: CacheTier) -> int:
        return self._structural_ttl if tier == "structural" else self._runtime_ttl

    def get(
        self, object_type: str, object_id: str, *, tier: CacheTier = "structural"
    ) -> str | None:
        """Return the cached value, or ``None`` on miss / expiry / fingerprint change."""
        row = self._conn.execute(
            "SELECT value, extracted_at, fingerprint FROM cache_entries "
            "WHERE system = ? AND object_type = ? AND object_id = ? AND tier = ?",
            (self._system, object_type, object_id, tier),
        ).fetchone()
        if row is None:
            return None
        value, extracted_at, fingerprint = row
        if fingerprint != self._fingerprint:
            self._delete(object_type, object_id, tier)
            return None
        if self._clock() - float(extracted_at) > self._ttl(tier):
            self._delete(object_type, object_id, tier)
            return None
        return str(value)

    def put(
        self, object_type: str, object_id: str, value: str, *, tier: CacheTier = "structural"
    ) -> None:
        """Store a value under the given key with the current fingerprint and timestamp."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cache_entries "
            "(system, object_type, object_id, tier, fingerprint, value, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._system,
                object_type,
                object_id,
                tier,
                self._fingerprint,
                value,
                self._clock(),
            ),
        )
        self._conn.commit()

    def refresh(self, scope: str) -> int:
        """Invalidate cached entries by scope. Returns the number of rows removed.

        ``scope`` of ``"all"`` clears everything; otherwise it matches an ``object_type`` (e.g.
        ``"chains"``) or a specific ``object_id``.
        """
        if scope == "all":
            cursor = self._conn.execute(
                "DELETE FROM cache_entries WHERE system = ?", (self._system,)
            )
        else:
            cursor = self._conn.execute(
                "DELETE FROM cache_entries WHERE system = ? AND (object_type = ? OR object_id = ?)",
                (self._system, scope, scope),
            )
        self._conn.commit()
        return cursor.rowcount

    def _delete(self, object_type: str, object_id: str, tier: CacheTier) -> None:
        self._conn.execute(
            "DELETE FROM cache_entries "
            "WHERE system = ? AND object_type = ? AND object_id = ? AND tier = ?",
            (self._system, object_type, object_id, tier),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
