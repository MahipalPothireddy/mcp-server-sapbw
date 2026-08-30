"""The Mermaid flow diagram: connected, grouped, escaped, and honest about its bounds.

The previous emitter was tested by three substring checks on a two-node fixture, which passed on the
node declarations alone - a malformed or entirely absent edge line satisfied all three. These tests
parse the emitted block instead, so an assertion about an edge is an assertion about an edge.

Synthetic names only, with one deliberate exception: the escaping tests feed names containing the
characters that break Mermaid's label grammar, because that is the behaviour under test.
"""

from __future__ import annotations

import re
from typing import Literal, get_args

from mcp_server_sapbw.models.lineage import (
    LineageEdge,
    LineageEdgeKind,
    LineageGraph,
    LineageNode,
    LineageNodeType,
)
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.services.diagram import (
    _MERMAID_MAX_NODES,
    _NODE_STYLE,
    _TYPE_LABEL,
    build_layout,
    render_mermaid,
)

PROV = Provenance(source_table="RSTRAN")

#: A DataSource endpoint as BW stores it: `<DATASOURCE><padding><LOGSYS>`.
PADDED_SOURCE = "2LIS_13_VDITM" + " " * 18 + "SRCCLNT100"


def _node(name: str, node_type: LineageNodeType = "dso") -> LineageNode:
    return LineageNode(id=f"{node_type}:{name}", object_type=node_type, name=name, provenance=PROV)


def _edge(
    src: str,
    dst: str,
    *,
    kind: LineageEdgeKind = "transformation",
    advisory: bool = False,
    src_type: LineageNodeType = "dso",
    dst_type: LineageNodeType = "dso",
    update_modes: list[Literal["full", "delta", "init"]] | None = None,
) -> LineageEdge:
    return LineageEdge(
        src=f"{src_type}:{src}",
        dst=f"{dst_type}:{dst}",
        kind=kind,
        confidence="advisory" if advisory else "exact",
        update_modes=update_modes or [],
        provenance=PROV,
    )


def _graph(
    nodes: list[LineageNode], edges: list[LineageEdge], *, root: str | None = None
) -> LineageGraph:
    return LineageGraph(
        root_id=root or nodes[0].id,
        direction="both",
        depth=2,
        nodes=nodes,
        edges=edges,
    )


def _render(graph: LineageGraph, *, max_nodes: int = _MERMAID_MAX_NODES) -> str:
    return render_mermaid(build_layout(graph), max_nodes=max_nodes)


def _body(mermaid: str) -> list[str]:
    """The diagram lines, with the markdown fence stripped."""
    lines = mermaid.splitlines()
    assert lines[0] == "```mermaid"
    assert lines[-1] == "```"
    assert lines[1] == "flowchart LR"
    return lines[2:-1]


_EDGE = re.compile(r"^\s*(\w+)\s+(-->|-\.->)\|\"(.*)\"\|\s+(\w+)$")
_DECL = re.compile(r"^\s*(\w+)\[\"(.*)\"\]$")


def _edges(mermaid: str) -> list[tuple[str, str, str, str]]:
    out = []
    for line in _body(mermaid):
        match = _EDGE.match(line)
        if match:
            out.append((match.group(1), match.group(2), match.group(3), match.group(4)))
    return out


def _declarations(mermaid: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _body(mermaid):
        match = _DECL.match(line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


# --- A. connected: every edge is drawn, between the right two nodes ---------------------------


def test_every_edge_is_emitted_between_the_correct_nodes() -> None:
    graph = _graph(
        [_node("SRC", "datasource"), _node("MID"), _node("TGT", "adso")],
        [
            _edge("SRC", "MID", src_type="datasource"),
            _edge("MID", "TGT", dst_type="adso"),
        ],
    )
    mermaid = _render(graph)
    declarations = _declarations(mermaid)
    edges = _edges(mermaid)
    assert len(edges) == 2, "an edge in the graph must be an edge in the diagram"

    drawn = {
        (declarations[src].split("<br/>")[0], declarations[dst].split("<br/>")[0])
        for src, _, _, dst in edges
    }
    assert drawn == {("SRC", "MID"), ("MID", "TGT")}


def test_no_node_is_left_isolated_when_the_graph_is_connected() -> None:
    graph = _graph(
        [_node("A"), _node("B"), _node("C")],
        [_edge("A", "B"), _edge("B", "C")],
    )
    mermaid = _render(graph)
    declared = set(_declarations(mermaid))
    touched = {end for src, _, _, dst in _edges(mermaid) for end in (src, dst)}
    assert declared == touched, "a connected graph must not render an unreachable box"


def test_a_self_loop_is_drawn_rather_than_dropped() -> None:
    """BW allows a transformation whose source and target are one object; S01 contains one."""
    graph = _graph([_node("SELFLOAD")], [_edge("SELFLOAD", "SELFLOAD")])
    edges = _edges(_render(graph))
    assert len(edges) == 1
    assert edges[0][0] == edges[0][3]


# --- B. readable: stages, colour, root, clipped labels ----------------------------------------


def test_nodes_are_grouped_into_dependency_stages() -> None:
    graph = _graph(
        [_node("SRC", "datasource"), _node("MID"), _node("TGT", "adso")],
        [_edge("SRC", "MID", src_type="datasource"), _edge("MID", "TGT", dst_type="adso")],
    )
    body = "\n".join(_body(_render(graph)))
    assert 'subgraph stage0["Stage 1"]' in body
    assert 'subgraph stage2["Stage 3"]' in body, "a three-hop flow should read as three stages"
    assert body.count("  end") == 3


def test_each_object_type_present_gets_its_own_colour_class() -> None:
    graph = _graph(
        [_node("SRC", "datasource"), _node("Q", "query")],
        [_edge("SRC", "Q", src_type="datasource", dst_type="query", kind="query_provider")],
    )
    body = "\n".join(_body(_render(graph)))
    assert "classDef t_datasource fill:" in body
    assert "classDef t_query fill:" in body
    assert "classDef t_dso" not in body, "only the types actually drawn should be defined"
    assert body.count("class n") >= 2, "every node must be assigned to its type class"


def test_the_root_is_emphasised() -> None:
    graph = _graph([_node("ROOT"), _node("OTHER")], [_edge("ROOT", "OTHER")], root="dso:ROOT")
    assert re.search(r"style n\d+ stroke-width:3px;", _render(graph)) is not None


def test_a_space_padded_datasource_name_is_collapsed_and_clipped() -> None:
    """Pasted raw, the padding makes one box wider than the rest of the diagram together."""
    graph = _graph(
        [_node(PADDED_SOURCE, "datasource"), _node("TGT")],
        [_edge(PADDED_SOURCE, "TGT", src_type="datasource")],
    )
    labels = list(_declarations(_render(graph)).values())
    padded = [text for text in labels if "2LIS_13_VDITM" in text]
    assert padded, "the DataSource should still be present"
    assert "  " not in padded[0], "the stored padding must be collapsed"
    assert len(padded[0].split("<br/>")[0]) <= 26


def test_the_object_type_is_shown_in_words_not_as_an_enum_token() -> None:
    graph = _graph([_node("CP", "compositeprovider"), _node("T")], [_edge("CP", "T")])
    labels = "\n".join(_declarations(_render(graph)).values())
    assert "CompositeProvider" in labels
    assert "(compositeprovider)" not in labels


# --- C. honest: advisory edges, update modes, stated bounds -----------------------------------


def test_an_advisory_edge_is_dashed_and_a_declared_one_is_not() -> None:
    """Both halves asserted. Checking only the dash lets a solid arrow regress unnoticed."""
    graph = _graph(
        [_node("A"), _node("B"), _node("LOOKUP")],
        [
            _edge("A", "B"),
            _edge("LOOKUP", "B", kind="routine_lookup", advisory=True),
        ],
    )
    arrows = {label: arrow for _, arrow, label, _ in _edges(_render(graph))}
    assert arrows["routine"] == "-.->"
    assert any(arrow == "-->" for arrow in arrows.values()), "a declared edge must render solid"


def test_edge_labels_carry_every_update_mode() -> None:
    """A pair with both a full and a delta DTP must not read as only one of them."""
    graph = _graph(
        [_node("A"), _node("B")],
        [_edge("A", "B", kind="dtp", update_modes=["full", "delta"])],
    )
    labels = [label for *_, label, _ in [(e[0], e[1], e[2], e[3]) for e in _edges(_render(graph))]]
    assert any("full" in text and "delta" in text for text in labels)


def test_the_legibility_bound_is_stated_in_the_diagram() -> None:
    nodes = [_node(f"OBJ{i:03d}") for i in range(12)]
    edges = [_edge(f"OBJ{i:03d}", f"OBJ{i + 1:03d}") for i in range(11)]
    mermaid = _render(_graph(nodes, edges), max_nodes=5)
    body = "\n".join(_body(mermaid))
    assert "showing 5 of 12 objects" in body
    assert "narrow the direction or depth" in body, "a bound must name its own remedy"
    assert len(_declarations(mermaid)) == 6, "five nodes plus the note node"


def test_an_edge_to_a_clipped_node_is_not_drawn_dangling() -> None:
    nodes = [_node(f"OBJ{i:03d}") for i in range(6)]
    edges = [_edge(f"OBJ{i:03d}", f"OBJ{i + 1:03d}") for i in range(5)]
    mermaid = _render(_graph(nodes, edges), max_nodes=3)
    declared = set(_declarations(mermaid))
    for src, _, _, dst in _edges(mermaid):
        assert src in declared and dst in declared


def test_a_truncated_graph_says_so_on_the_diagram() -> None:
    graph = LineageGraph(
        root_id="dso:A",
        direction="both",
        depth=2,
        nodes=[_node("A"), _node("B")],
        edges=[_edge("A", "B")],
        completeness="node_limit",
    )
    rendered = _render(graph)
    assert "graph truncated upstream" in rendered
    assert "lower the depth" in rendered


def test_the_two_bounds_are_reported_separately() -> None:
    """A legibility clip and an upstream truncation have different fixes, so they read apart."""
    nodes = [_node(f"OBJ{i:03d}") for i in range(9)]
    graph = LineageGraph(
        root_id=nodes[0].id,
        direction="both",
        depth=2,
        nodes=nodes,
        edges=[_edge(f"OBJ{i:03d}", f"OBJ{i + 1:03d}") for i in range(8)],
        completeness="node_limit",
    )
    body = "\n".join(_body(_render(graph, max_nodes=4)))
    assert "showing 4 of 9 objects" in body
    assert "graph truncated upstream" in body


def test_a_complete_graph_carries_no_bound_note() -> None:
    graph = _graph([_node("A"), _node("B")], [_edge("A", "B")])
    body = "\n".join(_body(_render(graph)))
    assert "bound[" not in body
    assert "truncated" not in body


# --- D. escaping: one odd name must not cost the whole diagram --------------------------------


def test_a_quote_in_a_name_does_not_break_the_label_grammar() -> None:
    """An unescaped quote closes the label early and Mermaid then fails the entire block."""
    graph = _graph([_node('ODD"NAME'), _node("B")], [_edge('ODD"NAME', "B")])
    mermaid = _render(graph)
    labels = _declarations(mermaid)
    assert labels, "the declaration line must still parse"
    assert '"' not in "".join(labels.values())
    assert "#quot;" in "".join(labels.values())


def test_hash_and_angle_brackets_are_escaped() -> None:
    graph = _graph([_node("A#1<x>"), _node("B")], [_edge("A#1<x>", "B")])
    rendered = "".join(_declarations(_render(graph)).values())
    assert "#35;" in rendered
    assert "#lt;" in rendered and "#gt;" in rendered


def test_a_slashed_bw_name_survives_unchanged_in_the_label() -> None:
    """Slashes are legal in a quoted label and are part of the name, so they must not be mangled."""
    namespaced = "/IMO/D_MMIM10"
    graph = _graph([_node(namespaced), _node("B")], [_edge(namespaced, "B")])
    assert namespaced in "".join(_declarations(_render(graph)).values())


def test_node_ids_are_synthetic_so_a_name_never_reaches_id_position() -> None:
    graph = _graph([_node("/IMO/D_MMIM10"), _node("B")], [_edge("/IMO/D_MMIM10", "B")])
    for node_id in _declarations(_render(graph)):
        assert re.fullmatch(r"n\d+|bound", node_id), f"{node_id} is not a safe Mermaid id"


# --- E. the visual vocabulary must cover every node type --------------------------------------
#
# Found on a real diagram: DTP nodes rendered as grey boxes labelled "object", because `dtp` is a
# LineageNodeType that neither table carried and both fall through to a default. Eleven of the
# twenty-three types were in that state, in the PNG and SVG as well as here. Reflection over the
# Literal rather than a hand-written list, since a hand-written list is how the gap opened.


def test_every_node_type_has_a_colour_and_a_label() -> None:
    declared = set(get_args(LineageNodeType))
    assert declared - set(_NODE_STYLE) == set(), "a node type with no colour renders as 'unknown'"
    assert declared - set(_TYPE_LABEL) == set(), "a node type with no label renders as 'object'"


def test_the_vocabulary_carries_nothing_that_is_not_a_node_type() -> None:
    declared = set(get_args(LineageNodeType))
    assert set(_NODE_STYLE) - declared == set()
    assert set(_TYPE_LABEL) - declared == set()


def test_a_dtp_node_is_labelled_and_coloured_as_itself() -> None:
    graph = _graph(
        [_node("DTP_0ABC", "dtp"), _node("TGT")],
        [_edge("DTP_0ABC", "TGT", src_type="dtp", kind="dtp")],
    )
    mermaid = _render(graph)
    assert "DTP" in "".join(_declarations(mermaid).values())
    assert "object" not in "".join(_declarations(mermaid).values())
    assert "classDef t_dtp fill:#eef2f7" in mermaid


# --- F. the two renderers must agree about the graph ------------------------------------------


def test_mermaid_and_image_layouts_describe_the_same_graph() -> None:
    graph = _graph(
        [_node("SRC", "datasource"), _node("MID"), _node("TGT", "adso")],
        [_edge("SRC", "MID", src_type="datasource"), _edge("MID", "TGT", dst_type="adso")],
    )
    layout = build_layout(graph)
    mermaid = render_mermaid(layout)
    assert len(_declarations(mermaid)) == len(layout.nodes)
    assert len(_edges(mermaid)) == len(layout.edges)
