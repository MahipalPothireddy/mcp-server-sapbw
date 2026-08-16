"""The object graph, and the cycle it exists to find.

The layer analyzer detected self-loops and two-object cycles, and said so in a caveat:
longer cycles were not searched. A three-object loop has no correct load order either. The tests
below pin that, plus the operations the traversals used to each reimplement.
"""

from __future__ import annotations

from mcp_server_sapbw.models.evidence import evidence_for
from mcp_server_sapbw.models.lineage import LineageEdge, LineageGraph, LineageNode
from mcp_server_sapbw.models.objects import BwObjectRef
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.services.graph import ObjectGraph

_PROV = Provenance(source_table="RSTRAN")


def _ref(object_type: str, name: str) -> BwObjectRef:
    return BwObjectRef(object_type=object_type, name=name)  # type: ignore[arg-type]


def _chain(*pairs: tuple[tuple[str, str], tuple[str, str]]) -> ObjectGraph:
    graph = ObjectGraph()
    for (src_type, src_name), (dst_type, dst_name) in pairs:
        graph.add_edge(_ref(src_type, src_name), _ref(dst_type, dst_name))
    return graph


# --- construction and identity ------------------------------------------------------------


def test_nodes_are_keyed_by_canonical_id() -> None:
    graph = _chain((("datasource", "DS_A"), ("dso", "STAGE")))
    assert graph.node_ids == ["datasource:DS_A", "dso:STAGE"]
    assert "dso:STAGE" in graph
    assert len(graph) == 2


def test_a_dso_and_an_infoobject_of_the_same_name_are_different_nodes() -> None:
    """The reason keys are type-qualified: BW names are only near-unique."""
    graph = _chain(
        (("dso", "SALES"), ("infocube", "SALES_CUBE")),
        (("infoobject", "SALES"), ("infocube", "SALES_CUBE")),
    )
    assert len(graph) == 3
    assert graph.neighbours("infocube:SALES_CUBE", "upstream") == ["dso:SALES", "infoobject:SALES"]


def test_duplicate_edges_are_collapsed() -> None:
    graph = ObjectGraph()
    for _ in range(3):
        graph.add_edge(_ref("dso", "A"), _ref("infocube", "B"))
    assert len(graph.edges()) == 1


def test_a_better_typed_ref_upgrades_an_unknown_node() -> None:
    graph = ObjectGraph()
    graph.add_node(_ref("unknown", "THING"))
    graph.add_node(_ref("unknown", "THING"))
    assert graph.node("unknown:THING") is not None
    # A typed ref is a different id, so both exist - which is correct: an untyped node is a
    # different statement from a typed one, and silently merging them would invent a fact.
    graph.add_node(_ref("adso", "THING"))
    assert set(graph.node_ids) == {"unknown:THING", "adso:THING"}


def test_built_from_a_lineage_graph() -> None:
    nodes = [
        LineageNode(id="DS_A", object_type="datasource", name="DS_A", provenance=_PROV),
        LineageNode(id="STAGE", object_type="dso", name="STAGE", provenance=_PROV),
    ]
    edges = [LineageEdge(src="DS_A", dst="STAGE", kind="transformation", provenance=_PROV)]
    graph = ObjectGraph.from_lineage(
        LineageGraph(root_id="STAGE", direction="both", depth=2, nodes=nodes, edges=edges)
    )
    assert graph.node_ids == ["datasource:DS_A", "dso:STAGE"]
    assert graph.neighbours("datasource:DS_A") == ["dso:STAGE"]
    # The evidence rides along, so the graph can still say why an edge is believed.
    assert graph.edges()[0].evidence is not None


def test_built_from_name_pairs_normalises_the_tlogo_code() -> None:
    graph = ObjectGraph.from_pairs([("STAGE", "SALES_CUBE", "CUBE")])
    assert "infocube:SALES_CUBE" in graph


def test_lineage_edges_naming_absent_nodes_are_skipped_not_invented() -> None:
    graph = ObjectGraph.from_lineage(
        LineageGraph(
            root_id="A",
            direction="both",
            depth=1,
            nodes=[LineageNode(id="A", object_type="dso", name="A", provenance=_PROV)],
            edges=[LineageEdge(src="A", dst="GHOST", kind="transformation", provenance=_PROV)],
        )
    )
    assert graph.node_ids == ["dso:A"]
    assert graph.edges() == []


# --- traversal ----------------------------------------------------------------------------


def test_reachable_walks_transitively() -> None:
    graph = _chain(
        (("datasource", "DS"), ("dso", "STAGE")),
        (("dso", "STAGE"), ("adso", "EDW")),
        (("adso", "EDW"), ("infocube", "MART")),
    )
    assert graph.reachable("datasource:DS") == {"dso:STAGE", "adso:EDW", "infocube:MART"}
    assert graph.reachable("infocube:MART", "upstream") == {
        "adso:EDW",
        "dso:STAGE",
        "datasource:DS",
    }


def test_reachable_respects_a_depth_bound() -> None:
    graph = _chain(
        (("datasource", "DS"), ("dso", "STAGE")),
        (("dso", "STAGE"), ("adso", "EDW")),
        (("adso", "EDW"), ("infocube", "MART")),
    )
    assert graph.reachable("datasource:DS", depth=1) == {"dso:STAGE"}
    assert graph.reachable("datasource:DS", depth=2) == {"dso:STAGE", "adso:EDW"}


def test_paths_enumerates_every_route() -> None:
    """'How does data get from this DataSource to this report' is not a neighbour question."""
    graph = _chain(
        (("datasource", "DS"), ("dso", "STAGE")),
        (("dso", "STAGE"), ("adso", "EDW_A")),
        (("dso", "STAGE"), ("adso", "EDW_B")),
        (("adso", "EDW_A"), ("infocube", "MART")),
        (("adso", "EDW_B"), ("infocube", "MART")),
    )
    result = graph.paths("datasource:DS", "infocube:MART")
    assert len(result.paths) == 2
    assert result.truncated is False
    assert all(p[0] == "datasource:DS" and p[-1] == "infocube:MART" for p in result.paths)


def test_paths_reports_its_cap_rather_than_silently_truncating() -> None:
    graph = _chain((("dso", "A"), ("dso", "B")), (("dso", "B"), ("dso", "C")))
    result = graph.paths("dso:A", "dso:C", max_paths=0)
    assert result.truncated is True
    assert result.reason and "cap" in result.reason


def test_paths_between_absent_endpoints_says_so() -> None:
    graph = _chain((("dso", "A"), ("dso", "B")))
    result = graph.paths("dso:A", "dso:NOPE")
    assert result.paths == []
    assert result.reason and "not in this graph" in result.reason


def test_a_cycle_does_not_hang_path_enumeration() -> None:
    graph = _chain(
        (("dso", "A"), ("dso", "B")),
        (("dso", "B"), ("dso", "A")),
        (("dso", "B"), ("infocube", "C")),
    )
    result = graph.paths("dso:A", "infocube:C")
    assert result.paths == [["dso:A", "dso:B", "infocube:C"]]


# --- cycles: the gap this component closes ------------------------------------------------


def test_a_three_object_circular_dependency_is_found() -> None:
    """A->B->C->A. The previous detector searched only self-loops and two-object cycles."""
    graph = _chain(
        (("dso", "A"), ("dso", "B")),
        (("dso", "B"), ("dso", "C")),
        (("dso", "C"), ("dso", "A")),
    )
    cycles = graph.cycles()
    assert len(cycles) == 1
    assert cycles[0].members == ["dso:A", "dso:B", "dso:C"]
    assert cycles[0].is_self_loop is False
    assert len(cycles[0].edges) == 3


def test_a_two_object_cycle_is_still_found() -> None:
    graph = _chain((("dso", "A"), ("dso", "B")), (("dso", "B"), ("dso", "A")))
    assert [c.members for c in graph.cycles()] == [["dso:A", "dso:B"]]


def test_a_self_loop_is_reported_and_marked_as_one() -> None:
    graph = _chain((("dso", "A"), ("dso", "A")))
    cycles = graph.cycles()
    assert [c.members for c in cycles] == [["dso:A"]]
    assert cycles[0].is_self_loop is True


def test_an_acyclic_graph_reports_no_cycles() -> None:
    graph = _chain(
        (("datasource", "DS"), ("dso", "STAGE")),
        (("dso", "STAGE"), ("adso", "EDW")),
        (("adso", "EDW"), ("infocube", "MART")),
    )
    assert graph.cycles() == []


def test_a_diamond_is_not_a_cycle() -> None:
    """Two paths to the same target is normal BW modelling, not a loop."""
    graph = _chain(
        (("dso", "A"), ("adso", "B")),
        (("dso", "A"), ("adso", "C")),
        (("adso", "B"), ("infocube", "D")),
        (("adso", "C"), ("infocube", "D")),
    )
    assert graph.cycles() == []


def test_several_independent_cycles_are_reported_separately() -> None:
    graph = _chain(
        (("dso", "A"), ("dso", "B")),
        (("dso", "B"), ("dso", "A")),
        (("adso", "X"), ("adso", "Y")),
        (("adso", "Y"), ("adso", "Z")),
        (("adso", "Z"), ("adso", "X")),
    )
    cycles = graph.cycles()
    # Sorted largest first, because the bigger tangle is the bigger problem.
    assert [len(c.members) for c in cycles] == [3, 2]


def test_cycle_detection_survives_a_deep_chain() -> None:
    """Iterative Tarjan on purpose: a recursive one would blow the stack on a real landscape."""
    graph = ObjectGraph()
    depth = 2000
    for i in range(depth):
        graph.add_edge(_ref("dso", f"N{i}"), _ref("dso", f"N{i + 1}"))
    graph.add_edge(_ref("dso", f"N{depth}"), _ref("dso", "N0"))  # close the loop
    cycles = graph.cycles()
    assert len(cycles) == 1
    assert len(cycles[0].members) == depth + 1


# --- stats --------------------------------------------------------------------------------


def test_stats_describe_the_shape() -> None:
    graph = _chain(
        (("datasource", "DS"), ("dso", "STAGE")),
        (("dso", "STAGE"), ("adso", "EDW")),
        (("dso", "STAGE"), ("infocube", "MART")),
    )
    graph.add_node(_ref("dso", "ORPHAN"))
    stats = graph.stats()
    assert stats.node_count == 5
    assert stats.edge_count == 3
    assert stats.source_count == 1  # the DataSource
    assert stats.sink_count == 2  # EDW and MART
    assert stats.isolated_count == 1  # ORPHAN
    assert stats.max_fan_out == 2  # STAGE feeds two
    assert stats.nodes_by_type["dso"] == 2


def test_stats_summarise_the_evidence_mix() -> None:
    graph = ObjectGraph()
    graph.add_edge(
        _ref("dso", "A"), _ref("adso", "B"), evidence=evidence_for("lineage_edge", "exact")
    )
    graph.add_edge(
        _ref("adso", "B"),
        _ref("infocube", "C"),
        kind="routine_lookup",
        evidence=evidence_for("lineage_edge", "advisory"),
    )
    stats = graph.stats()
    assert stats.evidence is not None
    assert (stats.evidence.observed, stats.evidence.inferred) == (1, 1)
    assert stats.edges_by_kind == {"routine_lookup": 1, "transformation": 1}
