"""SQL dialect builder for read-only HANA SELECTs.

Responsibilities (mission Section 3, Rule 6):

* build parameterized SELECTs — values are always bound, never string-interpolated;
* auto-inject ``OBJVERS = 'A'`` on ``RSD*`` / ``RSO*`` / ``RSTRAN*`` / ``RSZ*`` tables unless a
  version comparison is explicitly requested;
* qualify tables with the schema resolved by the capability record (never hardcoded);
* provide pagination and count helpers so list endpoints return a ``total_count``;
* build ``LIKE`` terms for caller-supplied name filters (:func:`like_term`) with one consistent
  wildcard rule across every list endpoint.

Everything produced here begins with ``SELECT`` and therefore passes the connection-layer guard.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..models.capability import CapabilityRecord

# Tables in these families carry an OBJVERS column; active version is 'A'.
_ACTIVE_VERSION_PREFIX = re.compile(r"^(RSD|RSO|RSZ|RSTRAN)", re.IGNORECASE)

# Escape character for LIKE patterns. Backslash has no special meaning in a HANA string literal,
# so ``ESCAPE '\'`` is a plain single-character literal.
LIKE_ESCAPE = "\\"

# Member tables that match the versioned prefixes but carry NO OBJVERS column (discovered live in
# B4). Active-version injection must be skipped for these, or the generated SQL references a
# non-existent column.
_NO_OBJVERS_TABLES = frozenset(
    {
        "RSOADSOKEYFIELDS",
        "RSOADSOPART",
        "RSOADSO_DTELNM",
    }
)


class DialectError(Exception):
    """A query could not be built (e.g. a table is unavailable on the connected release)."""


@dataclass(frozen=True)
class SelectQuery:
    """A ready-to-execute SELECT and its bound parameters."""

    sql: str
    parameters: list[Any] = field(default_factory=list)


# --- capability-read recording (off unless explicitly switched on) -----------------------------
#
# The capability contract needs to know which declared logical tables the code actually reads. A
# grep for `from_logical="..."` undercounts, because eight call sites pass the name as a variable
# fed from a module-level spec table - the text tables, the search sources, the provider catalogue
# and the declared-lookup stores are all reached that way, and a grep reports them as dead.
#
# Recording at this chokepoint measures the answer instead of inferring it. It is off by default and
# costs one `is None` check per query when off, so it is never a production cost.
# Single-element holder rather than a module-level rebind, matching the pattern used for the server
# runtime so no `global` statement is needed.
_recorder: dict[str, set[str]] = {}


def _record_read(logical: str) -> None:
    observed = _recorder.get("reads")
    if observed is not None:
        observed.add(logical)


@contextmanager
def record_reads() -> Iterator[set[str]]:
    """Collect the logical tables read inside the block.

    Used by the capability-contract check, which exercises the server and compares what was really
    read against what the resolver declares. Nested blocks are supported; it is not thread-safe by
    design, being a build-time measurement tool rather than a runtime feature.
    """
    observed: set[str] = set()
    previous = _recorder.get("reads")
    _recorder["reads"] = observed
    try:
        yield observed
    finally:
        if previous is None:
            _recorder.pop("reads", None)
        else:
            _recorder["reads"] = previous
            previous |= observed  # an outer block also saw whatever the inner one read


# --- per-tool attribution (off unless explicitly switched on) ----------------------------------
#
# The contract answers "is this capability read anywhere". The support matrix has to answer a
# different question - "which capabilities does *this tool* need" - because that is the unit a
# customer thinks in: nobody asks whether RSPCPROCESSLOG is present, they ask whether
# bw_get_chain_runtimes works on their release.
#
# Hand-maintaining that mapping across 55 tools would drift the first time a tool gained a reader,
# and drift silently, since nothing would contradict it. So it is measured through the same
# chokepoint: each tool call opens a nested recording scope, and what it read is attributed to it.
# Off by default and gated on a single bool, so a production call pays one attribute lookup.

_attribution: dict[str, set[str]] = {}
_attribution_on = False


def enable_tool_attribution() -> None:
    """Switch per-tool read attribution on. Called by the test session, never in production."""
    global _attribution_on  # noqa: PLW0603 - one process-wide build-time switch
    _attribution_on = True


def tool_attribution() -> dict[str, set[str]]:
    """What each tool was observed to read, so far. Keys are tool names."""
    return {tool: set(reads) for tool, reads in _attribution.items()}


def reset_tool_attribution() -> None:
    _attribution.clear()


@contextmanager
def record_tool_reads(tool: str) -> Iterator[None]:
    """Attribute the reads inside the block to one tool.

    A no-op unless :func:`enable_tool_attribution` has been called. The scope nests, so an outer
    session-wide recorder still sees everything the block read - the attribution is additional
    bookkeeping, not a redirection.
    """
    if not _attribution_on:
        yield
        return
    with record_reads() as observed:
        try:
            yield
        finally:
            _attribution.setdefault(tool, set()).update(observed)


def needs_active_version(physical_table: str) -> bool:
    """True when a physical table name belongs to an OBJVERS-versioned family.

    Member tables in ``_NO_OBJVERS_TABLES`` match the prefix but have no OBJVERS column, so they
    are excluded (injecting ``OBJVERS = 'A'`` there would reference a non-existent column).
    """
    if physical_table.upper() in _NO_OBJVERS_TABLES:
        return False
    return _ACTIVE_VERSION_PREFIX.match(physical_table) is not None


def quote_ident(name: str) -> str:
    """Double-quote a HANA identifier, escaping embedded quotes."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


@dataclass(frozen=True)
class LikeTerm:
    """A bound ``LIKE`` value plus the SQL fragment that applies it.

    Use :meth:`clause` to build the condition and pass :attr:`value` as the bound parameter, so the
    ``ESCAPE`` clause is only emitted when the value actually contains escapes.
    """

    value: str
    escaped: bool

    def clause(self, expression: str) -> str:
        """The ``<expression> LIKE ?`` condition, with ``ESCAPE`` when the value needs it."""
        suffix = f" ESCAPE '{LIKE_ESCAPE}'" if self.escaped else ""
        return f"{expression} LIKE ?{suffix}"


def like_term(pattern: str) -> LikeTerm:
    """Turn a caller-supplied name filter into an upper-cased ``LIKE`` term.

    One rule, used by every list endpoint that filters on an object name:

    * a pattern containing ``%`` is treated as caller-authored and passed through verbatim, so
      ``%SALES\\_O3%``-style patterns and their wildcards stay under the caller's control;
    * any other pattern is a **substring** search — wrapped in ``%``, with SQL ``LIKE``
      metacharacters (``_`` and the escape character) escaped so they match literally.

    The second rule is what makes BW naming usable: virtually every BW technical name contains an
    underscore, and DataSource endpoints are stored space-padded (``<DS><pad><LOGSYS>``), so an
    unescaped or unwrapped pattern silently matches nothing.
    """
    text = pattern.strip().upper()
    if "%" in text:
        return LikeTerm(text, escaped=False)
    escaped = text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("_", f"{LIKE_ESCAPE}_")
    return LikeTerm(f"%{escaped}%", escaped=escaped != text)


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
        _record_read(from_logical)
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
