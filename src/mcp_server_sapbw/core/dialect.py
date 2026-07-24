"""SQL dialect builder for read-only HANA SELECTs.

Responsibilities (mission Section 3, Rule 6):

* build parameterized SELECTs — values are always bound, never string-interpolated;
* auto-inject ``OBJVERS = 'A'`` on ``RSD*`` / ``RSO*`` / ``RSTRAN*`` / ``RSZ*`` tables unless a
  version comparison is explicitly requested;
* qualify tables with the schema resolved by the capability record (never hardcoded);
* provide pagination and count helpers so list endpoints return a ``total_count``.

Everything produced here begins with ``SELECT`` and therefore passes the connection-layer guard.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models.capability import CapabilityRecord

# Tables in these families carry an OBJVERS column; active version is 'A'.
_ACTIVE_VERSION_PREFIX = re.compile(r"^(RSD|RSO|RSZ|RSTRAN)", re.IGNORECASE)


class DialectError(Exception):
    """A query could not be built (e.g. a table is unavailable on the connected release)."""


@dataclass(frozen=True)
class SelectQuery:
    """A ready-to-execute SELECT and its bound parameters."""

    sql: str
    parameters: list[Any] = field(default_factory=list)


def needs_active_version(physical_table: str) -> bool:
    """True when a physical table name belongs to an OBJVERS-versioned family."""
    return _ACTIVE_VERSION_PREFIX.match(physical_table) is not None


def quote_ident(name: str) -> str:
    """Double-quote a HANA identifier, escaping embedded quotes."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


class SqlDialect:
    """Builds SELECT statements, resolving/qualifying tables via a capability record."""

    def __init__(self, capability: CapabilityRecord | None = None) -> None:
        self._capability = capability

    def _resolve(self, logical_name: str) -> tuple[str, str]:
        """Return ``(physical_name, qualified_reference)`` for a logical table name."""
        if self._capability is None:
            return logical_name, quote_ident(logical_name)
        status = self._capability.table(logical_name)
        if status is None or not status.present or status.resolved_name is None:
            raise DialectError(
                f"table '{logical_name}' is not available on this release; "
                "check capability.require() before building SQL"
            )
        physical = status.resolved_name
        if status.schema_name:
            return physical, f"{quote_ident(status.schema_name)}.{quote_ident(physical)}"
        return physical, quote_ident(physical)

    def build_select(
        self,
        *,
        columns: Sequence[str] | None = None,
        from_logical: str,
        where: Sequence[str] | None = None,
        params: Sequence[Any] | None = None,
        group_by: Sequence[str] | None = None,
        order_by: Sequence[str] | None = None,
        compare_versions: bool = False,
    ) -> SelectQuery:
        """Assemble a base SELECT (no pagination).

        ``where`` conditions use ``?`` placeholders whose values are supplied in ``params``.
        Unless ``compare_versions`` is set, ``OBJVERS = 'A'`` is appended for versioned tables.
        """
        physical, qualified = self._resolve(from_logical)
        column_list = ", ".join(columns) if columns else "*"
        conditions: list[str] = list(where or [])
        out_params: list[Any] = list(params or [])

        if not compare_versions and needs_active_version(physical):
            conditions.append("OBJVERS = 'A'")

        parts = [f"SELECT {column_list} FROM {qualified}"]
        if conditions:
            parts.append("WHERE " + " AND ".join(conditions))
        if group_by:
            parts.append("GROUP BY " + ", ".join(group_by))
        if order_by:
            parts.append("ORDER BY " + ", ".join(order_by))
        return SelectQuery(sql=" ".join(parts), parameters=out_params)

    def paginate(self, query: SelectQuery, *, limit: int, offset: int = 0) -> SelectQuery:
        """Append ``LIMIT ? OFFSET ?`` with bound parameters."""
        return SelectQuery(
            sql=f"{query.sql} LIMIT ? OFFSET ?",
            parameters=[*query.parameters, int(limit), int(offset)],
        )

    def count_query(self, query: SelectQuery) -> SelectQuery:
        """Wrap a base query as ``SELECT COUNT(*) ... `` for a ``total_count``.

        Pass the unpaginated base query (do not paginate before counting).
        """
        return SelectQuery(
            sql=f"SELECT COUNT(*) AS TOTAL_COUNT FROM ({query.sql})",
            parameters=list(query.parameters),
        )
