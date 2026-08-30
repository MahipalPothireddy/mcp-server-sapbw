"""Batching the CompositeProvider-consumer read must not change the graph it produces.

The read is the costliest branch of a deep walk - measured at 667ms against a ~110ms per-statement
floor on a production system, 34% of a depth-3 both-directions walk - so it is now issued once per
BFS level instead of once per node. That is a pure cost change, and these tests exist to hold it to
that: every assertion here compares the batched answer against the same answer read node by node,
rather than checking the batched path against expectations written alongside it.

Synthetic names only. The fixture derives its generated table names by calling the same resolver the
service calls, so a change to BW's naming convention cannot make the fixture agree with a stale
expectation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from mcp_server_sapbw.core.budget import charge_query, query_budget
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services import lineage as lineage_mod
from mcp_server_sapbw.services.lineage import LineageService
from mcp_server_sapbw.services.table_resolver import CALC_VIEW_PACKAGE

SCHEMA = "TESTSCHEMA"
PKG = CALC_VIEW_PACKAGE

_TABLES = {
    "transformation": "RSTRAN",
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "composite_header": "RSOHCPR",
    # Present but returning no rows: see the note in test_lineage_composite_consumers.py. It keeps
    # `completeness` in this file a statement about consumer discovery.
    "query_provider": "RSZCOMPIC",
    "query_dir": "RSZCOMPDIR",
    "element_dir": "RSZELTDIR",
}

ROOT = "ROOT_DSO"
#: Level-1 nodes. Two of them, so the level-1 frontier crosses _PREFETCH_MIN_FRONTIER and the
#: batched read is actually exercised; a single-node frontier would silently test the old path.
MID_ONE = "MID_ONE_DSO"
MID_TWO = "MID_TWO_DSO"

#: Consumers per node. Deliberately different in number, so a batch that mixed up attribution
#: produces a visibly wrong answer rather than a plausible one.
CONSUMERS: dict[str, list[str]] = {
    ROOT: ["CP_ROOT_A"],
    MID_ONE: ["CP_ONE_A", "CP_ONE_B", "CP_ONE_C"],
    MID_TWO: ["CP_TWO_A", "CP_TWO_B"],
}

#: Hierarchy runtime views: prunable noise, and they sort before the provider views.
_HIER_VIEWS = [f"{PKG}/HCPR_{i:04d}/hier" for i in range(40)]

_TRANS_BY_SOURCE: dict[str, list[tuple[str, str, str]]] = {
    ROOT: [(MID_ONE, "ODSO", "TR_ONE"), (MID_TWO, "ODSO", "TR_TWO")],
}


def _tables_for(name: str) -> list[str]:
    """The generated tables the service will ask about, from the service's own resolver."""
    return lineage_mod._consumer_tables(name)


def _population() -> dict[str, list[str]]:
    """base table -> dependent view names, as the catalogue would hold them.

    A node's consumers are spread across its candidate tables in **reverse** name order, which is
    the case that separates a correct batch from a plausible one. A node owns several base tables,
    so a batched read ordered by ``(base table, dependent)`` yields that node's consumers in a
    different order from its own read ordered by dependent alone - and where the consumer cap
    binds, a different order is a different subset. Spreading them evenly, or giving every table
    the same list, hides that: the two orders coincide and the batch looks right for the wrong
    reason.
    """
    rows: dict[str, list[str]] = {}
    for owner, consumers in CONSUMERS.items():
        tables = sorted(_tables_for(owner))
        descending = [f"{PKG}/{cp}" for cp in sorted(consumers, reverse=True)]
        for index, table in enumerate(tables):
            assigned = descending[index :: len(tables)]
            # The node's own generated view is a self-reference artefact; include it so the
            # batched path has to discard it too.
            rows[table] = [*_HIER_VIEWS, *assigned, f"{PKG}/{owner}"]
    return rows


POPULATION = _population()


def _like(value: str, pattern: str) -> bool:
    return (
        re.match("^" + ".*".join(re.escape(p) for p in pattern.split("%")) + "$", value) is not None
    )


class ScriptedConnection:
    """Serves RSTRAN and the dependency catalogue, honouring predicates, order and pagination."""

    def __init__(self) -> None:
        #: (batched?, limit, offset, ordered?) per dependency statement.
        self.dependency_reads: list[tuple[bool, int, int, bool]] = []
        self._rotation = 0

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        charge_query()
        params = list(parameters or [])
        if "OBJECT_DEPENDENCIES" in sql:
            return self._dependencies(sql, params)
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    def _dependencies(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        batched = "DISTINCT BASE_OBJECT_NAME" in sql
        limit, offset = int(params[-2]), int(params[-1])
        ordered = "ORDER BY" in sql
        prefix, hier_mid, hier_end = (str(params[-5]), str(params[-4]), str(params[-3]))
        requested = [str(t) for t in params[1:-6]]
        self.dependency_reads.append((batched, limit, offset, ordered))

        pairs: list[tuple[str, str]] = []
        for table in requested:
            for view in POPULATION.get(table, []):
                if _like(view, prefix) and not _like(view, hier_mid) and not _like(view, hier_end):
                    pairs.append((table, view))
        pairs = sorted(set(pairs))
        if ordered and not batched:
            # The single-node read states ORDER BY DEPENDENT_OBJECT_NAME only, so it must be
            # emulated that way. Sorting both shapes by (table, dependent) would make the two paths
            # agree here and nowhere else, and the re-sort the batch depends on would look
            # unnecessary.
            pairs.sort(key=lambda pair: pair[1])
        if not ordered:
            # An unordered LIMIT may return any qualifying rows, and a different set each time.
            self._rotation += 7
            cut = self._rotation % max(len(pairs), 1)
            pairs = pairs[cut:] + pairs[:cut]
        window = pairs[offset : offset + limit]
        if batched:
            return [(table, view) for table, view in window]
        return [(view,) for _, view in window]

    def _rstran(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if sql.startswith(("SELECT SOURCETYPE", "SELECT TARGETTYPE")):
            return [("ODSO",)]
        if "SOURCENAME IN (" in sql:  # the batched declared-edge prefetch
            out: list[tuple[Any, ...]] = []
            for source in [str(p) for p in params]:
                for target, target_type, tran in _TRANS_BY_SOURCE.get(source, []):
                    out.append((source, target, target_type, tran))
            return sorted(out)
        if "SOURCENAME = ?" in sql:
            return [
                (target, target_type, tran)
                for target, target_type, tran in _TRANS_BY_SOURCE.get(str(params[0]), [])
            ]
        return []


def _capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical,
                present=True,
                schema_name=SCHEMA,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _graph(connection: ScriptedConnection, depth: int = 2) -> Any:
    graph = LineageService(connection, _capability()).get_lineage(
        ROOT, direction="downstream", depth=depth
    )
    assert not isinstance(graph, UnsupportedResult)
    return graph


def _identity(graph: Any) -> tuple[frozenset[str], frozenset[tuple[str, str, str]]]:
    return (
        frozenset(f"{n.name}:{n.object_type}" for n in graph.nodes),
        frozenset((e.src, e.dst, e.kind) for e in graph.edges),
    )


def _unbatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the frontier prefetch, leaving the node-by-node reads in place."""
    monkeypatch.setattr(lineage_mod, "_PREFETCH_MIN_FRONTIER", 10**6)


# --- A. the batch is actually issued ----------------------------------------------------------


def test_a_multi_node_level_is_read_in_one_batched_statement() -> None:
    connection = ScriptedConnection()
    _graph(connection)
    batched = [r for r in connection.dependency_reads if r[0]]
    assert batched, "the level-1 frontier was not read in a batch"
    # Level 0 is a single node, so it stays below the batching threshold and reads on its own.
    assert [r for r in connection.dependency_reads if not r[0]], "expected the root's own read"


def test_batching_reduces_the_statement_count(monkeypatch: pytest.MonkeyPatch) -> None:
    batched_conn = ScriptedConnection()
    _graph(batched_conn)
    with monkeypatch.context() as patch:
        _unbatched(patch)
        single_conn = ScriptedConnection()
        _graph(single_conn)
    assert len(batched_conn.dependency_reads) < len(single_conn.dependency_reads), (
        "batching must issue fewer dependency statements than reading node by node"
    )


def test_every_batched_page_is_ordered() -> None:
    connection = ScriptedConnection()
    _graph(connection)
    assert all(ordered for *_, ordered in connection.dependency_reads), (
        "an unordered page makes the subset returned arbitrary once any bound binds"
    )


# --- B. the graph is the same either way ------------------------------------------------------


def test_batched_and_node_by_node_graphs_are_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole claim of the change, stated as a comparison rather than as an expectation."""
    batched = _identity(_graph(ScriptedConnection()))
    with monkeypatch.context() as patch:
        _unbatched(patch)
        single = _identity(_graph(ScriptedConnection()))
    assert batched == single


def test_consumers_are_attributed_to_the_right_node() -> None:
    graph = _graph(ScriptedConnection())
    by_source: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.kind == "composite_part":
            by_source.setdefault(edge.src, set()).add(edge.dst)
    for owner, consumers in CONSUMERS.items():
        assert by_source.get(owner, set()) == set(consumers), (
            f"{owner} was given the wrong consumers - a batch attributed rows to the wrong node"
        )


def test_a_nodes_own_generated_view_is_still_discarded_when_batched() -> None:
    graph = _graph(ScriptedConnection())
    assert not [e for e in graph.edges if e.src == e.dst and e.kind == "composite_part"], (
        "a provider is not its own consumer, batched or not"
    )
    assert not [n for n in graph.nodes if n.name.startswith(PKG)], (
        "a raw calc-view path reached the graph"
    )


def test_batched_discovery_reports_a_complete_reading() -> None:
    graph = _graph(ScriptedConnection())
    assert graph.completeness == "complete"
    assert graph.truncated is False


# --- C. the batch is abandoned rather than trusted --------------------------------------------


def test_batch_abandoned_on_its_row_bound_still_returns_the_full_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound binding must cost speed, not rows: nodes fall back to their own reads.

    Both numbers matter. The page size has to be small enough that pages come back *full* when the
    row bound is reached - a bound set below a single short page is never actually tested, because
    the batch exhausts itself on the first read and is legitimately cached.
    """
    monkeypatch.setattr(lineage_mod, "_CONSUMER_PAGE_ROWS", 2)
    monkeypatch.setattr(lineage_mod, "_PREFETCH_CONSUMER_MAX_ROWS", 4)
    connection = ScriptedConnection()
    graph = _graph(connection)
    with monkeypatch.context() as patch:
        _unbatched(patch)
        patch.setattr(lineage_mod, "_CONSUMER_PAGE_ROWS", 2)
        expected = _identity(_graph(ScriptedConnection()))
    assert _identity(graph) == expected
    assert graph.completeness == "complete", (
        "an abandoned prefetch is not a bound on the answer, so it must not be reported as one"
    )
    assert [r for r in connection.dependency_reads if not r[0]], "the fallback read never happened"


def test_batch_abandoned_when_a_table_cannot_be_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose base table belongs to no node in the batch invalidates that batch."""

    class ForeignRowConnection(ScriptedConnection):
        def _dependencies(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
            rows = super()._dependencies(sql, params)
            if "DISTINCT BASE_OBJECT_NAME" in sql and rows:
                return [("A_TABLE_NOBODY_ASKED_FOR", rows[0][1]), *rows]
            return rows

    connection = ForeignRowConnection()
    graph = _graph(connection)
    with monkeypatch.context() as patch:
        _unbatched(patch)
        expected = _identity(_graph(ScriptedConnection()))
    assert _identity(graph) == expected, "an unattributable batch must not reach the graph"


def test_colliding_candidate_tables_abandon_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two nodes claiming one generated table is not resolved by picking one."""
    monkeypatch.setattr(lineage_mod, "_consumer_tables", lambda name: ["SHARED_TABLE"])
    connection = ScriptedConnection()
    _graph(connection)
    assert not [r for r in connection.dependency_reads if r[0]], (
        "a colliding batch must be abandoned before it is read, not attributed by guesswork"
    )


def test_the_batch_is_not_issued_when_the_budget_has_no_headroom() -> None:
    """With the allowance nearly spent, the batch defers to the expansions rather than spending it.

    Asserted on the prefetch directly. The end-to-end version of this cannot be written against
    this fixture: the whole walk costs about ten statements, so every budget that trips the guard
    also stops the walk before it reaches a second level, and the assertion would be about the
    fixture's size rather than about the guard. The single-node path's budget reporting is covered
    in test_lineage_composite_consumers.py, where the population is large enough to mean it.
    """
    connection = ScriptedConnection()
    service = LineageService(connection, _capability())
    with query_budget(max_queries=1, max_seconds=0):
        service._prefetch_consumers([MID_ONE, MID_TWO])
    assert not connection.dependency_reads, "the batch spent an allowance it was asked to leave"
    assert not service._pf_consumers, "nothing may be cached from a batch that was never read"


def test_prefetch_never_runs_for_a_single_node_level() -> None:
    connection = ScriptedConnection()
    _graph(connection, depth=1)
    assert not [r for r in connection.dependency_reads if r[0]], (
        "a one-node frontier gains nothing from a batch and must not pay for one"
    )


# --- D. bounds still behave when the answer came from a batch ---------------------------------


def test_the_consumer_cap_binds_identically_on_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap must select the same subset and report the same bound, batched or not."""
    monkeypatch.setattr(lineage_mod, "_MAX_COMPOSITE_CONSUMERS", 2)
    batched = _graph(ScriptedConnection())
    with monkeypatch.context() as patch:
        _unbatched(patch)
        single = _graph(ScriptedConnection())
    assert _identity(batched) == _identity(single)
    assert batched.completeness == single.completeness == "semantic_limit"
    assert batched.truncated is True


def test_a_capped_batched_result_is_reproducible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage_mod, "_MAX_COMPOSITE_CONSUMERS", 2)
    connection = ScriptedConnection()
    first = _identity(_graph(connection))
    second = _identity(_graph(connection))
    assert first == second, "a capped result must still be the same capped result"


def test_batched_pages_are_followed_to_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage_mod, "_CONSUMER_PAGE_ROWS", 3)
    connection = ScriptedConnection()
    graph = _graph(connection)
    with monkeypatch.context() as patch:
        _unbatched(patch)
        patch.setattr(lineage_mod, "_CONSUMER_PAGE_ROWS", 3)
        expected = _identity(_graph(ScriptedConnection()))
    assert _identity(graph) == expected
    assert len([r for r in connection.dependency_reads if r[0]]) >= 2, "the batch did not page"
