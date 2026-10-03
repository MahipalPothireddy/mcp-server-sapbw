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


def record_capability_read(logical: str) -> None:
    """Record a read of ``logical`` that did not go through :meth:`SqlDialect.build_select`.

    The escape hatch for the handful of statements the builder cannot express - an aliased join, for
    instance. Without it the capability contract reports such a table as *declared but never read*,
    which is the opposite of the truth, and would push someone to delete a load-bearing table.

    Call it with the same logical name the statement resolves, and take the physical name from the
    capability record rather than writing a literal - otherwise the contract measures one thing
    while the SQL does another.
    """
    _record_read(logical)


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
    """True when a physical table name belongs to an OBJVERS-versioned family, **by name**.

    The fallback for when the column list was never measured. It is a naming convention and it was
    wrong in both directions (D49): ``RSPC*``, ``RSEC*``, ``RSA*`` and the 3.x ``RSUPD*``/``RSTS*``
    families all carry ``OBJVERS`` and match none of the four prefixes, so **30 declared tables got
    no filter at all**; and three tables match a prefix without having the column, which is why
    :data:`_NO_OBJVERS_TABLES` exists. :meth:`SqlDialect.active_version_column` prefers the measured
    column set and only falls back here.
    """
    if physical_table.upper() in _NO_OBJVERS_TABLES:
        return False
    return _ACTIVE_VERSION_PREFIX.match(physical_table) is not None


#: The version column. :meth:`SqlDialect._active_version_needed` skips injection when a ``where``
#: already constrains it. Matching on the column name rather than a full condition is deliberate: a
#: reader filtering ``OBJVERS IN ('A','M')`` on purpose must not get ``= 'A'`` bolted underneath.
_OBJVERS = "OBJVERS"


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

    def _active_version_needed(
        self, logical: str, physical: str, conditions: Sequence[str]
    ) -> bool:
        """Whether to inject ``OBJVERS = 'A'``, deciding from the measured column set (D49).

        Mission Rule 6 is "active version only unless explicitly comparing", and it was enforced by
        a **name prefix** - ``RSD``/``RSO``/``RSZ``/``RSTRAN``. That got it wrong both ways. Thirty
        declared tables carry ``OBJVERS`` and match no prefix, so the filter was simply absent:
        process chains, analysis authorisations, the ABAP source table and the BW 3.x stack.
        Measured on the reference system this is not cosmetic - ``RSPCCHAIN`` holds 3,616 active
        rows out of 15,960, and **129 chain steps exist in the modified version and not the active
        one, 111 of them load steps**, so a load closure could attribute loads a chain never
        performs. The symptom that exposed it was a step-category total of exactly double the step
        count. In the other direction, three tables match a prefix and lack the column, which is
        what :data:`_NO_OBJVERS_TABLES` was invented to patch.

        Reading the column set instead makes the decision a measurement. Twenty-five call sites
        already added the condition by hand and stay correct; the condition is skipped when the
        ``where`` already constrains the column, so those do not get it twice - and a reader that
        deliberately asks for several versions is not overruled.

        Falls back to the name prefix when the columns were never measured, so a release whose
        ``DD03L`` could not be read behaves exactly as before.
        """
        if any(_OBJVERS in condition.upper() for condition in conditions):
            return False
        status = self._capability.table(logical) if self._capability else None
        if status is not None and status.columns_known:
            return status.has_column(_OBJVERS)
        return needs_active_version(physical)

    def _check_columns(self, logical: str, physical: str, columns: Sequence[str] | None) -> None:
        """Refuse a SELECT naming a column this release does not have (D45).

        Only **bare upper-case identifiers** are checked. A ``columns`` list legitimately contains
        expressions the dialect passes through verbatim - measured across the source: ``COUNT(*)``,
        ``MAX(DATUM)``, ``SUM(MEMORY_SIZE_IN_TOTAL)``, ``DISTINCT CHAIN_ID``, ``LENGTH(CDATA)`` and
        table-qualified names like ``V.DOMNAME``, 59 of them in all. Validating those would break
        every aggregate read in the server, so anything that is not a plain identifier is left alone
        and stays the caller's responsibility.

        Silent when the column list was never measured, which is what makes this safe to add to a
        path every read goes through: an unreadable ``DD03L`` degrades to today's behaviour rather
        than to a server that refuses everything.

        The error names the table, the column and the release, because the failure this replaces was
        ``invalid column name: PARENT_AREA: line 1 col 18`` - true, and useless for deciding whether
        the release is different or the server is wrong.
        """
        if self._capability is None or not columns:
            return
        status = self._capability.table(logical)
        if status is None or not status.columns_known:
            return
        missing = [
            column
            for column in columns
            if column.isidentifier() and column == column.upper() and not status.has_column(column)
        ]
        if missing:
            raise DialectError(
                f"{physical} on {self._capability.bw_release} has no column(s) "
                f"{', '.join(missing)} (logical table '{logical}'). This is a release difference, "
                "not a malformed query: check the dictionary for the equivalent column on this "
                "release rather than assuming the name used on another one."
            )

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
        self._check_columns(from_logical, physical, columns)
        column_list = ", ".join(columns) if columns else "*"
        conditions: list[str] = list(where or [])
        out_params: list[Any] = list(params or [])

        if not compare_versions and self._active_version_needed(from_logical, physical, conditions):
            conditions.append(f"{_OBJVERS} = 'A'")

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
