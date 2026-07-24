"""Repository base: the only layer that turns capability + dialect + connection into records.

Every repository method checks the capability record before building SQL (returning a structured
``UnsupportedResult`` when a table is absent), executes through the read-only connection, and stamps
each record with provenance. Repositories hold no tool registration and no cross-domain logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.cache import CacheTier, SqliteCache
from ..core.capabilities import SupportsSelect, unsupported_result
from ..core.dialect import SelectQuery, SqlDialect
from ..models.capability import CapabilityRecord
from ..models.provenance import Provenance, UnsupportedResult


class Repository:
    """Base for domain repositories."""

    def __init__(
        self,
        connection: SupportsSelect,
        capability: CapabilityRecord,
        cache: SqliteCache | None = None,
    ) -> None:
        self._connection = connection
        self._capability = capability
        self._dialect = SqlDialect(capability)
        self._cache = cache

    @property
    def capability(self) -> CapabilityRecord:
        return self._capability

    @property
    def dialect(self) -> SqlDialect:
        return self._dialect

    def require(
        self, *logical_names: str, alternative: str | None = None
    ) -> UnsupportedResult | None:
        """Return an ``UnsupportedResult`` if any logical table is absent, else ``None``."""
        absent = [name for name in logical_names if not self._capability.is_available(name)]
        if not absent:
            return None
        return unsupported_result(
            self._capability,
            [self.physical(name) for name in absent],
            alternative=alternative,
        )

    def physical(self, logical_name: str) -> str:
        """Resolved physical table name for a logical name (falls back to the logical name)."""
        status = self._capability.table(logical_name)
        if status is not None and status.resolved_name is not None:
            return status.resolved_name
        return logical_name

    def select(self, query: SelectQuery) -> list[tuple[Any, ...]]:
        """Execute a read-only SELECT through the connection (statement guard applies)."""
        return self._connection.execute_select(query.sql, query.parameters)

    def provenance(self, logical_name: str, key: Mapping[str, Any]) -> Provenance:
        """Build a Provenance citing the resolved physical table and a stringified key."""
        return Provenance(
            source_table=self.physical(logical_name),
            source_key={k: str(v) for k, v in key.items()},
        )

    # --- cache passthrough (no-ops when no cache is configured) ---

    def cache_get(
        self, object_type: str, object_id: str, *, tier: CacheTier = "structural"
    ) -> str | None:
        return self._cache.get(object_type, object_id, tier=tier) if self._cache else None

    def cache_put(
        self, object_type: str, object_id: str, value: str, *, tier: CacheTier = "structural"
    ) -> None:
        if self._cache is not None:
            self._cache.put(object_type, object_id, value, tier=tier)
