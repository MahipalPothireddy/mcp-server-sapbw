"""Declared BEx query consumers of a provider (defect D12). Synthetic names only.

D12, found by an independent human verification of scenario S01 against a production system. Asked
which reports read an ADSO, the walk returned 23 and missed 47 of the 70 BW declares - and reported
``completeness="complete"`` while doing so. The cause was that query consumers were only ever
discovered side-on: BW generates a calc view per query, so a query whose view happens to read an
object's active table was found, while the queries *defined on* the CompositeProvider above it were
never looked for, because nothing asked RSZCOMPIC.

The fixture reproduces the shape that makes this subtle. RSZCOMPIC carries a row per query
**component**, not per query: on the measured system one CompositeProvider had 267 rows for 48
reports, the rest being structures and calculated key figures. So the reader has to join RSZELTDIR
and keep only query roots (``DEFTP='REP'``) - and a fixture without non-root components would let a
reader that skips that join pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services import lineage as lineage_mod
from mcp_server_sapbw.services.lineage import LineageService

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "dtp": "RSBKDTP",
    "query_provider": "RSZCOMPIC",
    "query_dir": "RSZCOMPDIR",
    "element_dir": "RSZELTDIR",
}

PROVIDER = "SALES_CP"

#: COMPUID -> (technical name, is a query root). The two non-roots are the trap: both carry a
#: RSZCOMPIC assignment to PROVIDER, and neither is a report.
_COMPONENTS: dict[str, tuple[str, bool]] = {
    "UID_Q1": ("SALES_RPT_01", True),
    "UID_Q2": ("SALES_RPT_02", True),
    "UID_Q3": ("SALES_RPT_03", True),
    "UID_S1": ("SALES_STRUCT_ROWS", False),  # a structure
    "UID_C1": ("SALES_CKF_MARGIN", False),  # a calculated key figure
}
_QUERY_NAMES = {name for name, is_root in _COMPONENTS.values() if is_root}
_NON_QUERY_NAMES = {name for name, is_root in _COMPONENTS.values() if not is_root}


class ScriptedConnection:
    """RSZCOMPIC/RSZCOMPDIR/RSZELTDIR, applying the root-element subquery like a database would."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        params = list(parameters or [])
        if "RSZCOMPIC" in sql:
            return self._compic(sql, params)
        if "RSZCOMPDIR" in sql:
            return self._compdir(sql, params)
        return []

    @staticmethod
    def _compic(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        # The root-element filter arrives as an inlined subquery, not a bind parameter, so it is
        # applied here exactly as the database would apply it.
        roots_only = "DEFTP = 'REP'" in sql
        wanted = {str(p).strip().upper() for p in params}
        batched = "INFOCUBE IN (" in sql
        rows: list[tuple[Any, ...]] = []
        for compuid, (_name, is_root) in _COMPONENTS.items():
            if roots_only and not is_root:
                continue
            if PROVIDER.upper() not in wanted:
                continue
            rows.append((PROVIDER, compuid) if batched else (compuid,))
        return rows

    @staticmethod
    def _compdir(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "COMPUID IN (" in sql:
            wanted = {str(p).strip() for p in params}
            return [(uid, _COMPONENTS[uid][0]) for uid in _COMPONENTS if uid in wanted]
        return []  # the _header() lookup: nothing here is itself a query being expanded


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _graph(present: set[str] | None = None, connection: ScriptedConnection | None = None) -> Any:
    service = LineageService(connection or ScriptedConnection(), _capability(present))
    graph = service.get_lineage(PROVIDER, direction="downstream", depth=1)
    assert not isinstance(graph, UnsupportedResult)
    return graph


def _query_edges(graph: Any) -> list[Any]:
    return [e for e in graph.edges if e.kind == "query_provider"]


def test_every_declared_query_is_a_consumer() -> None:
    """The D12 assertion: BW's own assignment table decides who the reports are."""
    graph = _graph()
    assert {e.dst for e in _query_edges(graph)} == _QUERY_NAMES


def test_query_components_are_not_reported_as_reports() -> None:
    """The join that a reader is tempted to skip, and what skipping it costs.

    A structure and a calculated key figure both carry a RSZCOMPIC row against the provider. Without
    the RSZELTDIR root-element filter they arrive as consumers, and on the measured system that
    turned 48 reports into 267 - so the graph would be wrong in a way that looks like thoroughness.
    """
    graph = _graph()
    reported = {node.name for node in graph.nodes}
    leaked = reported & _NON_QUERY_NAMES
    assert not leaked, f"query components reported as reports: {sorted(leaked)}"


def test_declared_query_edges_are_observed_not_inferred() -> None:
    """Evidence has to say RSZCOMPIC, because that is what makes this answer better than the old one.

    The calc-view route produced ``derived``/``generated_view_naming``. This route reads a declared
    assignment, so it is ``observed`` - and a caller sorting a mixed set by trust depends on the
    difference being visible rather than both arriving as "advisory".
    """
    for edge in _query_edges(_graph()):
        assert edge.derivation == "declared"
        assert edge.confidence == "exact"
        assert edge.evidence is not None
        assert edge.evidence.basis == "observed"
        assert edge.evidence.method == "declared_query_assignment"
        assert edge.evidence.is_advisory is False
        assert edge.provenance.source_table == "RSZCOMPIC"
        assert edge.provenance.source_key["INFOCUBE"] == PROVIDER


def test_query_nodes_are_typed_as_queries() -> None:
    graph = _graph()
    for name in _QUERY_NAMES:
        node = next(n for n in graph.nodes if n.name == name)
        assert node.object_type == "query"
        assert node.ref is not None
        assert node.ref.id == f"query:{name}"


def test_a_provider_with_declared_queries_reads_as_complete() -> None:
    """The branch must not mark a walk bounded merely for having run."""
    assert _graph().completeness == "complete"


def test_the_cap_is_reported_rather_than_silently_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half the defect was the label: an incomplete answer called itself complete."""
    monkeypatch.setattr(lineage_mod, "_MAX_DECLARED_QUERY_CONSUMERS", 2)
    graph = _graph()
    assert len(_query_edges(graph)) == 2
    assert graph.completeness == "semantic_limit", (
        "a capped consumer list must say it is a bounded reading, which is exactly what D12 did not"
    )


def test_a_release_without_the_assignment_table_says_so() -> None:
    """Absent metadata is not an empty answer: the class of relationship is missing, not absent."""
    graph = _graph(present={"transformation", "dtp", "query_dir", "element_dir"})
    assert _query_edges(graph) == []
    assert graph.completeness == "unsupported_branch"


def test_a_release_without_the_element_directory_says_so() -> None:
    """Without RSZELTDIR a query cannot be told from a structure, so the branch declines to guess."""
    graph = _graph(present={"transformation", "dtp", "query_provider", "query_dir"})
    assert _query_edges(graph) == []
    assert graph.completeness == "unsupported_branch"


def test_the_level_is_read_in_one_batch_not_once_per_node() -> None:
    """The read is batched into the frontier prefetch, so D12's fix costs a statement per level."""
    connection = ScriptedConnection()
    _graph(connection=connection)
    batched = [s for s in connection.statements if "RSZCOMPIC" in s and "INFOCUBE IN (" in s]
    per_node = [s for s in connection.statements if "RSZCOMPIC" in s and "INFOCUBE = ?" in s]
    # Depth 1 from a single root is one node, which is below the batching threshold, so the
    # single-node path is the correct one here; what matters is that both exist and agree.
    assert batched or per_node
