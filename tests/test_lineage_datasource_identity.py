"""A DataSource's identity, in and out: D19, D72, and the edge lookup that sat between them.

BW stores a DataSource endpoint as ``<DATASOURCE><padding><LOGSYS>`` - one column holding two facts.
Three separate defects live in that sentence and they had to be fixed together:

* **D19.** The padded endpoint was reported as the object's *name*, so the same DataSource had a
  different name in every environment (BDLS rewrites the logical system) and nothing keyed by
  DataSource name matched it.
* **D72.** Stripping the name made every name the server reports for a DataSource unusable as an
  *input*: measured on the reference system, downstream lineage from a bare name returned **1 node
  and 0 edges** where the stored endpoint returned **44 and 49**. An empty graph reads as "this
  DataSource feeds nothing".
* **The lookup between them.** ``ObjectGraph.from_lineage`` resolved edge endpoints through
  ``node.name`` while the edges reference ``node.id``. Equal for every node ever built, so invisible
  - until D19 made one node's name differ from its key, at which point **19 DataSources lost all 54
  of their edges** on production, silently, with no caveat.

Offline against a scripted landscape. Synthetic names only.

    DS_ORDERS<pad>SRC100  --TR1-->  STAGE_DSO  --TR2-->  MART_CUBE
    DS_PLAIN              --TR3-->  STAGE_DSO          (stored with no padding at all)
    DS_ORDERS_EXT<pad>SRC100 --TR4--> OTHER_DSO        (a prefix near-miss, must not be confused)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.lineage import LineageNode
from mcp_server_sapbw.models.provenance import Provenance, UnsupportedResult
from mcp_server_sapbw.services.graph import ObjectGraph
from mcp_server_sapbw.services.lineage import LineageService

SCHEMA = "TESTSCHEMA"
_TABLES = {"transformation": "RSTRAN", "dtp": "RSBKDTP"}

_SYSTEM_ID = "SRC100"
#: Padded exactly as BW pads it: the name to 30 characters, then the logical system. Written as a
#: computed value rather than a literal so the padding cannot drift from the name's length.
def _endpoint(name: str) -> str:
    return name.ljust(30) + _SYSTEM_ID


DS_ORDERS = "DS_ORDERS"
DS_ORDERS_EP = _endpoint(DS_ORDERS)
DS_NEAR = "DS_ORDERS_EXT"  # shares DS_ORDERS as a prefix
DS_NEAR_EP = _endpoint(DS_NEAR)
DS_PLAIN = "DS_PLAIN"  # a DataSource stored with no logical system, which is legitimate

# source -> [(target, target TLOGO, TRANID)]
_BY_SOURCE: dict[str, list[tuple[str, str, str]]] = {
    DS_ORDERS_EP: [("STAGE_DSO", "ODSO", "TR1")],
    DS_PLAIN: [("STAGE_DSO", "ODSO", "TR3")],
    DS_NEAR_EP: [("OTHER_DSO", "ODSO", "TR4")],
    "STAGE_DSO": [("MART_CUBE", "CUBE", "TR2")],
}
# target -> [(source, source TLOGO, TRANID)]
_BY_TARGET: dict[str, list[tuple[str, str, str]]] = {
    "STAGE_DSO": [(DS_ORDERS_EP, "RSDS", "TR1"), (DS_PLAIN, "RSDS", "TR3")],
    "MART_CUBE": [("STAGE_DSO", "ODSO", "TR2")],
    "OTHER_DSO": [(DS_NEAR_EP, "RSDS", "TR4")],
}
#: The object's own TLOGO from each side, which is how a *root* learns its type - it has no inbound
#: hop to learn from. Keyed by the stored value, so a bare name is genuinely unknown here, which is
#: the condition D72's resolution triggers on.
_OWN_TYPE_AS_SOURCE = {
    DS_ORDERS_EP: "RSDS",
    DS_NEAR_EP: "RSDS",
    DS_PLAIN: "RSDS",
    "STAGE_DSO": "ODSO",
}
_OWN_TYPE_AS_TARGET = {"STAGE_DSO": "ODSO", "MART_CUBE": "CUBE", "OTHER_DSO": "ODSO"}


class ScriptedConnection:
    """Answers RSTRAN in the four shapes this walk uses, discriminated by its column list.

    The type probe and the declared-edge read both filter on ``SOURCENAME = ?`` and differ only in
    what they select, so matching on the ``WHERE`` alone would hand one the other's rows. Keyed on
    the projection for that reason.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        params = [str(p).strip() for p in (parameters or [])]
        if "RSTRAN" not in sql:
            return []  # no DTP rows in this landscape; transformations carry every edge
        if sql.startswith("SELECT SOURCETYPE"):
            code = _OWN_TYPE_AS_SOURCE.get(params[0] if params else "")
            return [(code,)] if code else []
        if sql.startswith("SELECT TARGETTYPE"):
            code = _OWN_TYPE_AS_TARGET.get(params[0] if params else "")
            return [(code,)] if code else []
        if "DISTINCT SOURCENAME" in sql and "LIKE" in sql:
            # A prefix search, like the database: the pattern is `<name>%` with `_` escaped.
            prefix = (params[0] if params else "").replace("\\_", "_").rstrip("%")
            return [(name,) for name in sorted(_BY_SOURCE) if name.startswith(prefix)]
        if "SOURCENAME IN (" in sql:
            return [
                (name, target, code, tran)
                for name in params
                for target, code, tran in _BY_SOURCE.get(name, [])
            ]
        if "TARGETNAME IN (" in sql:
            return [
                (name, source, code, tran)
                for name in params
                for source, code, tran in _BY_TARGET.get(name, [])
            ]
        name = params[0] if params else ""
        if "SOURCENAME = ?" in sql:
            return list(_BY_SOURCE.get(name, []))
        if "TARGETNAME = ?" in sql:
            return list(_BY_TARGET.get(name, []))
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


def _datasource_node(name: str, *, direction: str = "upstream", root: str = "MART_CUBE") -> Any:
    graph = _service().get_lineage(root, direction=direction, depth=4)  # type: ignore[arg-type]
    assert not isinstance(graph, UnsupportedResult)
    return next((n for n in graph.nodes if n.name == name), None)


# --- D19: the name is the DataSource, the logical system is a fact ------------------------------


def test_a_datasource_is_named_without_its_logical_system() -> None:
    node = _datasource_node(DS_ORDERS)
    assert node is not None, "the upstream walk must reach the DataSource for this to mean anything"
    assert node.name == DS_ORDERS
    assert " " not in node.name


def test_the_logical_system_is_kept_as_a_fact_rather_than_dropped() -> None:
    """Stripped from the identity, not discarded. Which system it extracts from is worth knowing."""
    node = _datasource_node(DS_ORDERS)
    assert node.source_system is not None
    assert node.source_system.system_id == _SYSTEM_ID
    # Named, not classified: deciding what *kind* of system this is belongs to the source registry.
    assert node.source_system.system_type == "unknown"


def test_the_graph_key_keeps_the_raw_endpoint() -> None:
    """``id`` is what the edges reference, so it cannot be rewritten to suit presentation."""
    node = _datasource_node(DS_ORDERS)
    assert node.id == DS_ORDERS_EP
    assert node.id != node.name


def test_the_canonical_ref_is_type_qualified_and_environment_independent() -> None:
    """``ref.id`` is the value other tools join on, and BDLS must not be able to change it."""
    node = _datasource_node(DS_ORDERS)
    assert node.ref is not None
    assert node.ref.id == f"datasource:{DS_ORDERS}"


def test_a_datasource_stored_without_a_logical_system_is_left_alone() -> None:
    """The unpadded form is legitimate, and inventing a system for it is worse than nothing."""
    node = _datasource_node(DS_PLAIN)
    assert node is not None
    assert node.name == DS_PLAIN
    assert node.source_system is None


def test_trace_to_source_reports_the_bare_name() -> None:
    trace = _service().trace_to_source("MART_CUBE", depth=6)
    assert not isinstance(trace, UnsupportedResult)
    assert DS_ORDERS in trace.datasources_reached
    assert DS_ORDERS_EP not in trace.datasources_reached


def test_a_node_typed_later_is_stripped_on_that_path_too() -> None:
    """The upgrade path needs its own cover, and cannot be reached through the public walk.

    A DataSource is routinely named by a neighbour before its own type is known, so the node is
    created untyped with the padded name intact and only *becomes* a DataSource when a later hop
    says so. Rebuilding the ref from the raw parameter there would put the logical system straight
    back into the name - one branch fixed and one not, which is how D19 would have half-shipped.
    """
    service = _service()
    nodes: dict[str, LineageNode] = {}
    provenance = Provenance(source_table="RSTRAN", source_key={})
    service._ensure_node(nodes, DS_ORDERS_EP, "unknown", provenance)
    assert nodes[DS_ORDERS_EP].name == DS_ORDERS_EP  # untyped: nothing to strip yet

    service._ensure_node(nodes, DS_ORDERS_EP, "datasource", provenance)
    upgraded = nodes[DS_ORDERS_EP]
    assert upgraded.name == DS_ORDERS
    assert upgraded.ref is not None and upgraded.ref.id == f"datasource:{DS_ORDERS}"
    assert upgraded.source_system is not None
    assert upgraded.source_system.system_id == _SYSTEM_ID


# --- the edge lookup D19 would have broken -----------------------------------------------------


def test_an_object_graph_keeps_edges_when_a_name_differs_from_its_key() -> None:
    """The trap. Endpoints resolve through ``id``, because that is what the edges carry.

    Watched failing against the previous `by_name` lookup, on production: the DataSource nodes lost
    every edge, 19 of them, and the graph went from 122 edges to 68 - exactly the 54 that touch a
    DataSource. Nothing raised and nothing was caveated; the diagrams simply came out wrong.
    """
    graph = _service().get_lineage("MART_CUBE", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    built = ObjectGraph.from_lineage(graph)
    edges = [e for out in built._out.values() for e in out]

    assert len(edges) == len(graph.edges), "an edge was dropped in translation"
    assert any(e.src == f"datasource:{DS_ORDERS}" for e in edges), (
        "the DataSource kept its node but lost its edges, which is the silent form of this defect"
    )


# --- D72: the name the server reports has to work as an input ----------------------------------


def test_lineage_from_a_bare_datasource_name_finds_the_stored_endpoint() -> None:
    bare = _service().get_lineage(DS_ORDERS, direction="downstream", depth=3)
    padded = _service().get_lineage(DS_ORDERS_EP, direction="downstream", depth=3)
    assert not isinstance(bare, UnsupportedResult)
    assert not isinstance(padded, UnsupportedResult)

    assert {n.id for n in bare.nodes} == {n.id for n in padded.nodes}
    assert len(bare.edges) == len(padded.edges)
    assert "STAGE_DSO" in {n.name for n in bare.nodes}, "the bare name must reach the same flow"


def test_an_empty_graph_is_no_longer_the_answer_for_a_bare_name() -> None:
    """The measured symptom, stated as an assertion: 1 node and 0 edges reads as 'feeds nothing'."""
    graph = _service().get_lineage(DS_ORDERS, direction="downstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    assert len(graph.nodes) > 1
    assert graph.edges


def test_a_name_that_is_only_a_prefix_of_a_real_datasource_resolves_to_nothing() -> None:
    """A prefix search is a candidate generator, not the answer.

    ``DS_ORDER`` is not a DataSource; it is a prefix of two that are. Without the exact-head check
    it resolves to whichever candidate comes first and returns **another object's entire downstream
    flow** under the name the caller asked about - a confident, complete, wrong answer.

    This is the case the check is actually for. Asking about a real name cannot expose it: a padded
    endpoint pads with spaces, and a space sorts below every character a longer name could put
    there, so under ``ORDER BY`` the exact match always arrives first. A test written on
    ``DS_ORDERS`` therefore passes whether the check exists or not - it was, and it did.
    """
    graph = _service().get_lineage("DS_ORDER", direction="downstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)

    assert [n.name for n in graph.nodes] == ["DS_ORDER"], (
        "a prefix resolved to a real DataSource's endpoint and returned its flow"
    )
    assert graph.edges == []


def test_a_real_name_resolves_to_its_own_endpoint_and_not_a_longer_neighbour() -> None:
    graph = _service().get_lineage(DS_ORDERS, direction="downstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "STAGE_DSO" in names
    assert "OTHER_DSO" not in names, "resolved to the near-miss DataSource's flow"
    assert DS_NEAR not in names


def test_an_endpoint_passed_in_directly_is_not_re_resolved() -> None:
    """Already an endpoint, so the extra read is pointless and must not be issued."""
    conn = ScriptedConnection()
    _service(conn).get_lineage(DS_ORDERS_EP, direction="downstream", depth=2)
    assert not [s for s in conn.statements if "LIKE" in s]


def test_a_name_that_resolves_as_itself_costs_no_endpoint_read() -> None:
    """Only a name that resolves as nothing is worth a second look."""
    conn = ScriptedConnection()
    _service(conn).get_lineage("STAGE_DSO", direction="downstream", depth=2)
    assert not [s for s in conn.statements if "LIKE" in s]


def test_an_unknown_name_resolves_to_nothing_rather_than_to_something_else() -> None:
    graph = _service().get_lineage("NOSUCH_THING", direction="downstream", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    assert [n.name for n in graph.nodes] == ["NOSUCH_THING"]
    assert graph.edges == []


def test_impact_from_a_bare_datasource_name_does_not_count_the_root_as_affected() -> None:
    """The root is excluded by key, not by display name, or it counts as its own consumer."""
    impact = _service().impact_analysis(DS_ORDERS, depth=3)
    assert not isinstance(impact, UnsupportedResult)

    assert impact.root_id == DS_ORDERS_EP
    affected = {n.name for n in impact.graph.nodes if n.id != impact.root_id}
    assert DS_ORDERS not in affected
    assert impact.affected_object_count == len(affected)
    assert "STAGE_DSO" in affected
