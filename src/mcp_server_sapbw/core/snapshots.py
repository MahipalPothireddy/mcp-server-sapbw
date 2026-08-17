"""Where snapshots live between calls.

"What changed since last week" needs last week's snapshot to still exist, so this is a small SQLite
store beside the extract cache, one file per profile. It is separate from the cache on purpose: the
cache is disposable and expires, while a snapshot is a deliberate record a caller asked for and must
not vanish on a TTL or a capability refresh.

**This is customer metadata at rest**, and more of it than the cache holds - a snapshot names every
provider, transformation and chain in a system. Two consequences, both enforced rather than
documented and hoped for: the file lives under the same per-user cache root as everything else,
never inside a repository; and a profile with ``cache_enabled: false`` gets no store at all, so an
organisation that will not accept metadata at rest can still capture two snapshots in memory for an
immediate comparison and keep nothing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..models.snapshot import Snapshot, SnapshotSummary
from .identity import storage_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  TEXT NOT NULL PRIMARY KEY,
    system       TEXT NOT NULL,
    taken_at     TEXT NOT NULL,
    bw_release   TEXT NOT NULL,
    object_count INTEGER NOT NULL,
    edge_count   INTEGER NOT NULL,
    families     TEXT NOT NULL,
    truncated    INTEGER NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_by_time ON snapshots (system, taken_at DESC);
"""


#: Pass this instead of a file to keep a store entirely in memory, writing nothing. Used by the
#: offline suite so no test leaves customer-shaped metadata on disk.
IN_MEMORY = Path(":memory:")


class SnapshotStore:
    """Snapshots for one profile, in one SQLite file.

    **Thread safety.** ``check_same_thread=False`` was already set here, so unlike the extract cache
    this store never raised across threads - and that is precisely why its remaining hazard stayed
    invisible. One instance is shared for the life of the process
    (``ServerRuntime._snapshot_stores``) and reached from whichever worker thread serves a call, and
    ``commit()`` is scoped to the connection rather than the statement: one thread committing inside
    :meth:`put` would also commit another's in-flight ``DELETE`` from :meth:`delete`, whose
    ``rowcount`` is then read after that commit. Neither shows up as an error, so the same
    :class:`~threading.RLock` discipline applies as in :mod:`~mcp_server_sapbw.core.cache`.

    Re-entrant for the same reason the cache's is: it costs nothing and it removes the class of
    deadlock that appears the first time one guarded method calls another.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._in_memory = path == IN_MEMORY
        self._lock = threading.RLock()
        if not self._in_memory:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def put(self, snapshot: Snapshot) -> None:
        """Store a snapshot, replacing one with the same id."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(snapshot_id, system, taken_at, bw_release, object_count, edge_count, families, "
                "truncated, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_id,
                    snapshot.system,
                    snapshot.taken_at.isoformat(),
                    snapshot.bw_release,
                    snapshot.object_count,
                    snapshot.edge_count,
                    json.dumps(snapshot.scope.families),
                    int(snapshot.scope.truncated),
                    snapshot.model_dump_json(),
                ),
            )
            self._conn.commit()

    def get(self, snapshot_id: str) -> Snapshot | None:
        """One snapshot by id, or ``None``.

        A stored payload that no longer validates is treated as absent rather than raised: a model
        change shipped in a new server version must not turn an old snapshot into an error, and the
        caller's remedy is the same either way - take a fresh one.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return Snapshot.model_validate_json(row[0])
        except ValueError:
            return None

    def latest(self, system: str, *, before: datetime | None = None) -> Snapshot | None:
        """The most recent snapshot for a system, optionally the most recent before a moment.

        ``before`` is what makes "compare the current state against the last one" work without the
        caller tracking ids: capture now, then ask for the latest taken before that.
        """
        with self._lock:
            if before is None:
                row = self._conn.execute(
                    "SELECT payload FROM snapshots WHERE system = ? ORDER BY taken_at DESC LIMIT 1",
                    (system,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT payload FROM snapshots WHERE system = ? AND taken_at < ? "
                    "ORDER BY taken_at DESC LIMIT 1",
                    (system, before.isoformat()),
                ).fetchone()
        if row is None:
            return None
        try:
            return Snapshot.model_validate_json(row[0])
        except ValueError:
            return None

    def list(self, *, system: str | None = None, limit: int = 50) -> list[SnapshotSummary]:
        """Stored snapshots, newest first. Summaries only - the payloads are large."""
        sql = (
            "SELECT snapshot_id, system, taken_at, bw_release, object_count, edge_count, "
            "families, truncated FROM snapshots"
        )
        params: tuple[object, ...] = ()
        if system:
            sql += " WHERE system = ?"
            params = (system,)
        sql += " ORDER BY taken_at DESC LIMIT ?"
        params = (*params, int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            SnapshotSummary(
                snapshot_id=row[0],
                system=row[1],
                taken_at=datetime.fromisoformat(row[2]),
                bw_release=row[3],
                object_count=int(row[4]),
                edge_count=int(row[5]),
                families=json.loads(row[6]),
                truncated=bool(row[7]),
            )
            for row in rows
        ]

    def delete(self, snapshot_id: str) -> bool:
        # rowcount is read after the commit, so both must happen under one acquisition.
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def count(self, *, system: str | None = None) -> int:
        with self._lock:
            if system:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE system = ?", (system,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
        return int(row[0]) if row else 0

    def size_bytes(self) -> int:
        """On-disk size, so an operator can see how much metadata is at rest without opening it.

        Zero for an in-memory store, which is the truthful answer: nothing is at rest.
        """
        if self._in_memory:
            return 0
        try:
            return self._path.stat().st_size
        except OSError:  # pragma: no cover - the file was removed underneath us
            return 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def snapshot_file(system: str, cache_directory: Path, *, tenant: str | None = None) -> Path:
    """The snapshot file for one profile. Named from the profile identity, never from a host.

    Shares :func:`~mcp_server_sapbw.core.identity.storage_key` with the extract cache rather than
    repeating the sanitising rule. It repeated it before, and the copy had the same defect: five
    distinct aliases (``prd/eu``, ``prd_eu``, ``prd.eu``, ``prd eu``, ``prd:eu``) all produced one
    file name, so two profiles shared one snapshot store without anything failing.
    """
    return cache_directory / f"{storage_key(system, tenant)}.snapshots.sqlite"
