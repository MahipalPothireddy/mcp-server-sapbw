"""D7 regression: CompositeProvider consumer discovery must resolve before it caps.

Synthetic names only. The scripted connection deliberately behaves like a real database on the two
points that made D7 invisible in testing:

* it applies ``LIKE``/``NOT LIKE`` predicates itself, so SQL-side pruning is actually exercised
  rather than assumed;
* without ``ORDER BY`` it returns rows in an arbitrary, *rotating* order, because an uncapped
  ``LIMIT`` over an unordered result set is precisely what produced a different answer per call.

The population mirrors the measured shape on a real system: hierarchy runtime views outnumber
provider views by orders of magnitude, and they sort *before* the provider views. That ordering is
deliberate - it means ``ORDER BY`` alone does not rescue a raw-row cap, so these tests fail against
the old implementation for the right reason.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from mcp_server_sapbw.core.budget import charge_query, query_budget
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.evidence import evidence_for
from mcp_server_sapbw.models.lineage import LineageGraph
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services import lineage as lineage_mod
from mcp_server_sapbw.services.lineage import LineageService
from mcp_server_sapbw.services.table_resolver import (
    CALC_VIEW_PACKAGE,
    CALC_VIEW_QUERY_SEGMENT,
    is_hierarchy_view,
    provider_from_calc_view,
    query_from_calc_view,
)

SCHEMA = "TESTSCHEMA"
PKG = CALC_VIEW_PACKAGE

_TABLES = {
    "transformation": "RSTRAN",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "dtp": "RSBKDTP",
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "composite_header": "RSOHCPR",
    # Present but empty. The declared-query branch (D12) reports itself unsupported when RSZCOMPIC
    # cannot be read, which is correct on a release that lacks it - but it would make every
    # assertion about `completeness` in this file a statement about the missing tables rather than
    # about consumer paging. Declaring them present, with the connection returning no rows, keeps
    # these tests about what they are about and additionally pins that a provider with no queries
    # reads as complete rather than as bounded.
    "query_provider": "RSZCOMPIC",
    "query_dir": "RSZCOMPDIR",
    "element_dir": "RSZELTDIR",
}

ROOT = "PART_DSO"
#: A provider that is genuinely its own transformation source and target. BW allows this and S01
#: contains one, so the self-loop rule must not reach it.
SELF_LOOP_OBJECT = "SELFLOAD_DSO"

#: Consuming CompositeProviders. Named to sort *after* the hierarchy views on purpose.
CONSUMERS = [f"ZCP_{i:03d}" for i in range(1, 13)]

#: Hierarchy runtime views: the bulk of the population, and never a provider.
_HIER_VIEWS = [f"{PKG}/HCPR_{i:04d}/hier" for i in range(600)] + [
    f"{PKG}/HCPR_{i:04d}/hier/NODE" for i in range(600)
]
#: Provider views, plus the root's own generated view (a self-reference artefact).
_PROVIDER_VIEWS = [f"{PKG}/{name}" for name in CONSUMERS]
_SELF_VIEW = f"{PKG}/{ROOT}"
#: Outside the generated package: rejected by the resolver, so also prunable in SQL.
_FOREIGN_VIEWS = [f"other.package.thing/{name}" for name in ("V_ONE", "V_TWO")]

POPULATION = [*_HIER_VIEWS, *_PROVIDER_VIEWS, _SELF_VIEW, *_FOREIGN_VIEWS]

_TRANS_BY_SOURCE: dict[str, list[tuple[str, str, str]]] = {
    SELF_LOOP_OBJECT: [(SELF_LOOP_OBJECT, "ODSO", "TR_SELF")],
}


def _like(value: str, pattern: str) -> bool:
    """SQL ``LIKE`` with ``%`` wildcards, case-sensitive as HANA is for these names."""
    return (
        re.match("^" + ".*".join(re.escape(p) for p in pattern.split("%")) + "$", value) is not None
    )


class ScriptedConnection:
    """Emulates the dependency catalogue, including predicates, ordering and pagination."""

    def __init__(self) -> None:
        self.dependency_queries: list[tuple[int, int, bool]] = []
        self._rotation = 0

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        charge_query()  # the real connection charges per statement; budget tests need that here
        params = list(parameters or [])
        if "OBJECT_DEPENDENCIES" in sql:
            return self._dependencies(sql, params)
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    def _dependencies(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        limit, offset = int(params[-2]), int(params[-1])
        ordered = "ORDER BY" in sql
        rows = list(POPULATION)
        # The fixed shape is three LIKE placeholders: one package prefix, two hierarchy exclusions.
        if sql.count("LIKE ?") == 3:
            prefix, hier_mid, hier_end = (str(params[-5]), str(params[-4]), str(params[-3]))
            rows = [
                v
                for v in rows
                if _like(v, prefix) and not _like(v, hier_mid) and not _like(v, hier_end)
            ]
        self.dependency_queries.append((limit, offset, ordered))
        if ordered:
            rows.sort()
        else:
            # An unordered LIMIT may return any qualifying rows, and a different set each time. The
            # stride is wide enough that successive windows genuinely differ in *which kind* of row
            # they contain; a narrow stride would keep landing in the same run of hierarchy views,
            # and a consistently wrong answer would read as a stable one.
            self._rotation += 400
            rows = rows[self._rotation % len(rows) :] + rows[: self._rotation % len(rows)]
        return [(v,) for v in rows[offset : offset + limit]]

    def _rstran(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if sql.startswith(("SELECT SOURCETYPE", "SELECT TARGETTYPE")):
            return [("ODSO",)]  # the root's own type
        if "SOURCENAME = ?" in sql:  # downstream declared edges
            return list(_TRANS_BY_SOURCE.get(str(params[0]), []))
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


def _service(connection: ScriptedConnection | None = None) -> LineageService:
    return LineageService(connection or ScriptedConnection(), _capability())


def _consumer_names(graph: Any) -> set[str]:
    return {e.dst for e in graph.edges if e.kind == "composite_part"}


def _graph(connection: ScriptedConnection | None = None, root: str = ROOT) -> Any:
    graph = _service(connection).get_lineage(root, direction="downstream", depth=1)
    assert not isinstance(graph, UnsupportedResult)
    return graph


# --- A. the raw-row cap hid valid providers ---------------------------------------------------


def test_providers_beyond_the_raw_row_cap_are_found() -> None:
    """Every consumer is discovered although ~1200 non-provider rows qualify ahead of them.

    Against the old implementation this returns almost nothing: the cap took 50 raw rows, and the
    rows that resolve to a provider sort last.
    """
    graph = _graph()
    assert _consumer_names(graph) == set(CONSUMERS)
    assert graph.completeness == "complete"
    assert graph.truncated is False


def test_discovery_orders_and_prunes_in_sql() -> None:
    connection = ScriptedConnection()
    _graph(connection)
    assert connection.dependency_queries, "no dependency read was issued"
    assert all(ordered for _, _, ordered in connection.dependency_queries), (
        "every dependency page must be ordered, or the subset returned is arbitrary"
    )


# --- B. the cap counts resolved providers, not rows -------------------------------------------


def test_cap_applies_to_distinct_providers_not_raw_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a cap of 5, exactly 5 *providers* come back - not 5 rows out of thousands."""
    monkeypatch.setattr(lineage_mod, "_MAX_COMPOSITE_CONSUMERS", 5)
    graph = _graph()
    found = _consumer_names(graph)
    assert len(found) == 5
    assert found <= set(CONSUMERS)
    assert graph.completeness == "semantic_limit"


# --- C. determinism ---------------------------------------------------------------------------


def test_repeated_calls_return_identical_identity_sets() -> None:
    """Three consecutive calls, compared by identity sets rather than by counts.

    One connection is shared on purpose. The scripted database advances its arbitrary row order per
    statement, so a fresh connection per call would reset it and the test would pass even against an
    unordered read - which is exactly how this defect survived the existing suite.
    """
    connection = ScriptedConnection()
    node_sets: list[frozenset[str]] = []
    edge_sets: list[frozenset[tuple[str, str, str]]] = []
    for _ in range(3):
        graph = _graph(connection)
        node_sets.append(frozenset(n.id for n in graph.nodes))
        edge_sets.append(frozenset((e.src, e.dst, e.kind) for e in graph.edges))
    assert len(set(node_sets)) == 1, "node identity set changed between calls"
    assert len(set(edge_sets)) == 1, "edge identity set changed between calls"


# --- D. pagination ----------------------------------------------------------------------------


def test_providers_on_the_final_page_are_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage_mod, "_CONSUMER_PAGE_ROWS", 5)
    connection = ScriptedConnection()
    graph = _graph(connection)
    assert _consumer_names(graph) == set(CONSUMERS)
    assert len(connection.dependency_queries) >= 3, "discovery did not page"
    assert graph.completeness == "complete"


def test_paging_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page bound stops discovery and is reported, rather than crawling the catalogue."""
    monkeypatch.setattr(lineage_mod, "_CONSUMER_PAGE_ROWS", 1)
    monkeypatch.setattr(lineage_mod, "_MAX_CONSUMER_PAGES", 3)
    connection = ScriptedConnection()
    graph = _graph(connection)
    assert len(connection.dependency_queries) == 3
    assert graph.completeness == "semantic_limit"
    assert graph.truncated is True


# --- E. resolver artefacts --------------------------------------------------------------------


def test_no_self_loop_or_pseudo_object_consumers() -> None:
    graph = _graph()
    composite = [e for e in graph.edges if e.kind == "composite_part"]
    assert composite, "expected composite_part edges"
    assert not [e for e in composite if e.src == e.dst], "a provider is not its own consumer"
    names = {n.name for n in graph.nodes}
    assert "hier" not in names, "a hierarchy view resolved to a provider named 'hier'"
    assert not [n for n in names if "/hier" in n], "hierarchy pseudo-object reached the graph"
    assert not [n for n in names if n.startswith(PKG)], "a raw calc-view path reached the graph"


def test_declared_transformation_self_loop_is_preserved() -> None:
    """The artefact rule is scoped to composite_part: a real declared self-loop must survive."""
    graph = _graph(root=SELF_LOOP_OBJECT)
    self_edges = [e for e in graph.edges if e.src == e.dst]
    assert [e for e in self_edges if e.kind == "transformation"], (
        "a declared transformation whose source and target are one object must stay represented"
    )


# --- F. semantic limit is a deterministic subset ----------------------------------------------


def test_semantic_limit_subset_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage_mod, "_MAX_COMPOSITE_CONSUMERS", 4)
    first, second = _consumer_names(_graph()), _consumer_names(_graph())
    assert first == second, "a capped result must still be the same capped result"
    assert len(first) == 4


def test_semantic_limit_is_reported_not_implied_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage_mod, "_MAX_COMPOSITE_CONSUMERS", 4)
    graph = _graph()
    assert graph.truncated is True
    assert graph.completeness == "semantic_limit"
    assert any("cap" in c for c in graph.caveats)


# --- G. budget stop ---------------------------------------------------------------------------


def test_budget_stop_is_reported_honestly() -> None:
    """A budget that cannot cover another page stops discovery and says so."""
    with query_budget(max_queries=7, max_seconds=0):
        graph = _graph()
    assert graph.completeness == "query_budget"
    assert graph.truncated is True
    assert any("statement allowance" in c for c in graph.caveats)


def test_time_budget_stop_is_reported_honestly() -> None:
    # Inside the allowance (6s of 10s) but within the headroom, so discovery stops and reports
    # rather than spending the remainder and failing the whole call.
    calls = iter([0.0])
    with query_budget(max_queries=0, max_seconds=10, clock=lambda: next(calls, 6.0)):
        graph = _graph()
    assert graph.completeness == "time_budget"
    assert graph.truncated is True


# --- the completeness contract itself ---------------------------------------------------------


def test_truncated_and_completeness_cannot_disagree() -> None:
    # Older callers set only the bool; completeness must not claim the graph is whole.
    legacy = LineageGraph(root_id="X", direction="both", depth=1, truncated=True)
    assert legacy.completeness == "node_limit"
    # And a stated bound must always show up in the bool.
    bounded = LineageGraph(root_id="X", direction="both", depth=1, completeness="query_budget")
    assert bounded.truncated is True


# --- D9: a BEx query's generated view is a query, not a CompositeProvider ----------------------
#
# BW generates one calc view per query under a "query.<provider>" package suffix. That suffix was
# read as a provider namespace, so a query arrived typed `compositeprovider` under a synthesized
# name like '/QUERY.<PROVIDER>/<QUERY>'. The relationship is real; only the label was wrong.

# Deliberately outside the customer (Z*/Y*) namespace: these are synthetic fixture names living in a
# test file rather than under tests/fixtures/, and the leak check rightly refuses customer-shaped
# literals there. Working around it by splitting the string would defeat the check.
QUERY_PROVIDER = "HOST_CP"
QUERY_NAMES = ["Q_REVENUE", "Q_MARGIN"]
_QUERY_VIEWS = [
    f"{PKG}.{CALC_VIEW_QUERY_SEGMENT}.{QUERY_PROVIDER.lower()}/{q}" for q in QUERY_NAMES
]


class QueryViewConnection(ScriptedConnection):
    """Serves provider views, query views, and hierarchy noise from one population."""

    def _dependencies(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        global POPULATION  # noqa: PLW0603
        original = POPULATION
        POPULATION = [*original, *_QUERY_VIEWS]
        try:
            return super()._dependencies(sql, params)
        finally:
            POPULATION = original


def _query_graph() -> Any:
    graph = _service(QueryViewConnection()).get_lineage(ROOT, direction="downstream", depth=1)
    assert not isinstance(graph, UnsupportedResult)
    return graph


def test_query_calc_view_becomes_a_query_node_not_a_composite_provider() -> None:
    graph = _query_graph()
    by_name = {n.name: n for n in graph.nodes}
    for query in QUERY_NAMES:
        assert query in by_name, f"{query} should reach the graph under its own technical name"
        assert by_name[query].object_type == "query", (
            "a BEx query's generated view must not be typed as a CompositeProvider"
        )


def test_query_consumers_do_not_masquerade_as_composite_providers() -> None:
    graph = _query_graph()
    composite = {e.dst for e in graph.edges if e.kind == "composite_part"}
    assert composite == set(CONSUMERS), "only genuine CompositeProviders may be composite_part"
    assert not [n for n in graph.nodes if n.name.startswith("/QUERY.")], (
        "a synthesized '/QUERY.<PROVIDER>/<QUERY>' name must not reach the caller"
    )


def test_query_consumer_edge_uses_the_query_provider_kind() -> None:
    graph = _query_graph()
    query_edges = [e for e in graph.edges if e.dst in QUERY_NAMES]
    assert len(query_edges) == len(QUERY_NAMES)
    for edge in query_edges:
        assert edge.kind == "query_provider"
        assert edge.src == ROOT
        # The owning provider is worth keeping: it is how a reader finds the query in BW.
        assert QUERY_PROVIDER in (edge.note or ""), "the query's provider should be named"


def test_a_provider_namespace_called_query_is_not_mistaken_for_a_query_view() -> None:
    """The rule is the two-segment 'query.<provider>' suffix, not the word 'query'."""
    namespaced = f"{PKG}.query/V_THING"  # a provider in a '/QUERY/' namespace
    assert query_from_calc_view(namespaced) is None
    assert provider_from_calc_view(namespaced) == "/QUERY/V_THING"

    generated = f"{PKG}.query.some_cp/Q_ONE"
    assert provider_from_calc_view(generated) is None
    assert query_from_calc_view(generated) == ("Q_ONE", "SOME_CP")


def test_hierarchy_view_with_a_trailing_segment_is_still_rejected() -> None:
    assert is_hierarchy_view(f"{PKG}/CP_X/hier")
    assert provider_from_calc_view(f"{PKG}/CP_X/hier") is None
    # A provider genuinely named with a 'hier' prefix is not a hierarchy view.
    assert not is_hierarchy_view(f"{PKG}/HIERARCHY_X")


# --- D10: the evidence narrative must match how the fact was obtained -------------------------


def test_calc_view_consumer_edges_do_not_claim_to_come_from_abap() -> None:
    graph = _query_graph()
    derived = [e for e in graph.edges if e.kind in ("composite_part", "query_provider")]
    assert derived, "expected calc-view-derived edges"
    for edge in derived:
        assert edge.evidence is not None
        assert edge.evidence.method != "routine_select_parse", (
            "an edge read from OBJECT_DEPENDENCIES must not say it was parsed out of routine ABAP"
        )
        assert edge.evidence.method == "generated_view_naming"
        assert "OBJECT_DEPENDENCIES" in (edge.evidence.detail or "")


def test_routine_edges_still_say_they_came_from_abap() -> None:
    """The fix must not blunt the distinction it exists to preserve."""
    assert evidence_for("lineage_edge", "advisory").method == "routine_select_parse"
    assert evidence_for("calc_view_consumer", "provider").method == "generated_view_naming"
    assert evidence_for("calc_view_consumer", "provider").basis == "derived"


# --- H. one relationship, one confidence, whichever end the walk starts from -------------------
#
# A part provider and its CompositeProvider are a single declared fact. Walking upstream *from* the
# CompositeProvider it was read out of the stored model and reported ``exact``; walking downstream
# from the part, the same fact was resolved through the generated calc view and reported
# ``advisory``. The strength of the evidence therefore depended on which end the walk started from,
# and a figure centred on the part drew its last hop dashed beside an identical sibling edge drawn
# solid. Found in a production diagram: one part-to-CompositeProvider edge advisory and a sibling
# part's edge to the same CompositeProvider exact, both
# declared inputs of the same model.
#
# The confirmation is not free - it reads the model - so it is only worth doing because the model is
# the same extract the other direction already pays for, and it is memoised per CompositeProvider.

MODELLED_CONSUMER = CONSUMERS[0]
_PART_ALIAS = "U1.ADSO.1"
_STORED_MODEL = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Composite:compositeView xmlns:Composite="urn:composite" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" schemaVersion="1.12" '
    f'name="{MODELLED_CONSUMER}" withHanaModel="true" defaultNode="#///U1">\n'
    '  <viewNode xsi:type="View:Union" name="U1">\n'
    '    <element xsi:type="BwCore:BwElement" name="KEYFIELD" infoObjectName="KEYFIELD"/>\n'
    f'    <input xsi:type="Composite:CompositeInput" name="" alias="{_PART_ALIAS}" '
    'selectAll="true">\n'
    f"      <entity>{ROOT}.composite#//</entity>\n"
    "    </input>\n"
    "  </viewNode>\n"
    "</Composite:compositeView>\n"
)


class ModelledConnection(ScriptedConnection):
    """Also serves ``RSOHCPR``, so one consumer's stored model can actually be read.

    Only one, deliberately. The other eleven have no model here, which is what keeps the advisory
    branch under test in the same fixture: a relationship the model cannot confirm must stay
    advisory rather than being promoted along with the ones it can.
    """

    def __init__(self) -> None:
        super().__init__()
        #: Every RSOHCPR read, by the provider asked about, so the memo can be asserted on.
        self.model_reads: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if "RSOHCPR" in sql:
            charge_query()
            params = list(parameters or [])
            self.model_reads.append(str(params[0]) if params else "")
            return self._model(sql, params)
        return super().execute_select(sql, parameters)

    @staticmethod
    def _model(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if not params or str(params[0]) != MODELLED_CONSUMER:
            return []
        if "LENGTH(XML_UI)" in sql:
            return [(len(_STORED_MODEL),)]
        if "LENGTH(XML_DEF)" in sql:
            return [(0,)]
        if "XML_UI" in sql:
            return [(_STORED_MODEL,)]
        return []


def _part_edge(graph: Any, part: str, provider: str) -> Any:
    def is_wanted(edge: Any) -> bool:
        return bool(edge.kind == "composite_part" and edge.src == part and edge.dst == provider)

    found = [edge for edge in graph.edges if is_wanted(edge)]
    assert len(found) == 1, f"expected one {part}->{provider} part edge, got {len(found)}"
    return found[0]


def test_a_part_edge_the_model_confirms_reads_the_same_from_either_end() -> None:
    connection = ModelledConnection()
    service = _service(connection)

    downstream = service.get_lineage(ROOT, direction="downstream", depth=1)
    upstream = service.get_lineage(MODELLED_CONSUMER, direction="upstream", depth=1)
    assert not isinstance(downstream, UnsupportedResult)
    assert not isinstance(upstream, UnsupportedResult)

    from_part = _part_edge(downstream, ROOT, MODELLED_CONSUMER)
    from_provider = _part_edge(upstream, ROOT, MODELLED_CONSUMER)

    assert from_part.confidence == "exact", (
        "the stored model declares this part, so reaching it from the part end does not make the "
        "fact weaker"
    )
    assert _PART_ALIAS in (from_part.note or ""), "a confirmed edge should name the model's alias"
    assert (from_part.confidence, from_part.note) == (
        from_provider.confidence,
        from_provider.note,
    ), "the same relationship described differently depending on the direction of travel"
    assert from_part.evidence is not None and from_provider.evidence is not None
    assert from_part.evidence.method == from_provider.evidence.method
    assert from_part.evidence.method == evidence_for("composite_part", "declared_model").method


def test_a_part_edge_the_model_cannot_confirm_stays_advisory() -> None:
    """Promotion is earned per relationship, not applied to the kind.

    Eleven of these twelve CompositeProviders have no readable model in this fixture. Their edges
    rest on the generated view's name alone, which is genuinely weaker evidence, and saying so is
    the entire purpose of the field.
    """
    graph = _service(ModelledConnection()).get_lineage(ROOT, direction="downstream", depth=1)
    assert not isinstance(graph, UnsupportedResult)
    unconfirmed = [
        e for e in graph.edges if e.kind == "composite_part" and e.dst != MODELLED_CONSUMER
    ]
    assert len(unconfirmed) == len(CONSUMERS) - 1
    for edge in unconfirmed:
        assert edge.confidence == "advisory"
        assert edge.evidence is not None
        assert edge.evidence.method == "generated_view_naming"


def test_the_model_is_read_once_per_composite_provider() -> None:
    """Twelve consumers must not become twelve model reads per consumer.

    The confirmation runs inside consumer resolution, which a walk reaches once per part - so
    without the memo a wide CompositeProvider would pay for the same LOB repeatedly.
    """
    connection = ModelledConnection()
    service = _service(connection)
    service.get_lineage(ROOT, direction="downstream", depth=1)
    before = len(connection.model_reads)
    service.get_lineage(ROOT, direction="downstream", depth=1)
    assert len(connection.model_reads) == before, "the second walk re-read the stored model"
    assert connection.model_reads.count(MODELLED_CONSUMER) <= 3, (
        "one model is a size probe per column plus one body fetch, not more"
    )
    # And a provider whose model is absent is remembered as absent: 12 consumers, each probed once,
    # rather than once per edge that reaches them.
    assert len(connection.model_reads) == 3 + 2 * (len(CONSUMERS) - 1)
