"""Tests for data-flow diagram layout and rendering (offline, no DB).

Layout is deterministic by design, so these assert real geometric properties (flow direction,
layering, edge styling) rather than pixel output.
"""

from __future__ import annotations

from xml.etree import ElementTree

from mcp_server_sapbw.models.lineage import LineageEdge, LineageGraph, LineageNode
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.services.diagram import (
    build_layout,
    png_available,
    render_png,
    render_svg,
)

P = Provenance(source_table="RSTRAN")


def _node(nid: str, kind: str) -> LineageNode:
    return LineageNode(id=nid, object_type=kind, name=nid, provenance=P)  # type: ignore[arg-type]


def _edge(
    src: str, dst: str, kind: str = "transformation", confidence: str = "exact"
) -> LineageEdge:
    return LineageEdge(
        src=src,
        dst=dst,
        kind=kind,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        provenance=P,
    )


def _chain_graph(truncated: bool = False) -> LineageGraph:
    """DS -> STG -> EDW -> CP -> QUERY, plus an advisory routine lookup into EDW."""
    return LineageGraph(
        root_id="EDW",
        direction="both",
        depth=4,
        nodes=[
            _node("DS", "datasource"),
            _node("STG", "dso"),
            _node("EDW", "adso"),
            _node("CP", "compositeprovider"),
            _node("QUERY", "query"),
            _node("LOOKUP", "dso"),
        ],
        edges=[
            _edge("DS", "STG", "dtp"),
            _edge("STG", "EDW"),
            _edge("EDW", "CP", "composite_part"),
            _edge("CP", "QUERY", "query_provider"),
            _edge("LOOKUP", "EDW", "routine_lookup", "advisory"),
        ],
        node_count=6,
        edge_count=5,
        truncated=truncated,
    )


def test_layering_follows_dependency_depth() -> None:
    layout = build_layout(_chain_graph())
    layer = {n.id: n.layer for n in layout.nodes}
    assert layer["DS"] == 0
    assert layer["STG"] == 1
    assert layer["EDW"] == 2
    assert layer["CP"] == 3
    assert layer["QUERY"] == 4
    assert layout.layer_count == 5


def test_every_declared_edge_flows_left_to_right() -> None:
    layout = build_layout(_chain_graph())
    pos = {n.id: n.x for n in layout.nodes}
    for edge in layout.edges:
        if edge.src != edge.dst:
            assert pos[edge.src] < pos[edge.dst], f"{edge.src}->{edge.dst} does not flow forward"


def test_root_is_emphasised_and_labels_are_object_names() -> None:
    layout = build_layout(_chain_graph())
    roots = [n for n in layout.nodes if n.is_root]
    assert [n.id for n in roots] == ["EDW"]
    assert {n.label for n in layout.nodes} >= {"DS", "STG", "EDW", "CP", "QUERY"}


def test_layout_is_deterministic() -> None:
    first = build_layout(_chain_graph())
    second = build_layout(_chain_graph())
    assert [(n.id, n.x, n.y) for n in first.nodes] == [(n.id, n.x, n.y) for n in second.nodes]


def test_cycle_does_not_hang_layout() -> None:
    """A circular dependency is legal in BW; layering must terminate anyway."""
    graph = LineageGraph(
        root_id="A",
        direction="both",
        depth=2,
        nodes=[_node("A", "dso"), _node("B", "adso")],
        edges=[_edge("A", "B"), _edge("B", "A")],
        node_count=2,
        edge_count=2,
    )
    layout = build_layout(graph)
    assert len(layout.nodes) == 2


def test_self_loop_is_rendered() -> None:
    graph = LineageGraph(
        root_id="A",
        direction="both",
        depth=1,
        nodes=[_node("A", "dso")],
        edges=[_edge("A", "A")],
        node_count=1,
        edge_count=1,
    )
    svg = render_svg(build_layout(graph))
    assert ">self<" in svg


# --- SVG ----------------------------------------------------------------------------------


def test_svg_is_self_contained_and_styled() -> None:
    svg = render_svg(build_layout(_chain_graph()))
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    # No external references: nothing is fetched when the SVG is displayed.
    assert "http://" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "<image" not in svg
    # Advisory edges are dashed; declared ones are not.
    assert "stroke-dasharray" in svg
    # Legend names the types actually present plus the advisory convention.
    assert "DataSource" in svg and "CompositeProvider" in svg
    assert "advisory (heuristic)" in svg


def test_svg_escapes_object_names() -> None:
    graph = LineageGraph(
        root_id="A&<B>",
        direction="both",
        depth=1,
        nodes=[_node("A&<B>", "dso")],
        edges=[],
        node_count=1,
    )
    svg = render_svg(build_layout(graph))
    assert "A&amp;&lt;B&gt;" in svg
    assert "<B>" not in svg


def test_truncation_is_visible_on_the_canvas() -> None:
    assert "truncated" in render_svg(build_layout(_chain_graph(truncated=True)))
    assert "truncated" not in render_svg(build_layout(_chain_graph(truncated=False)))


# --- PNG ----------------------------------------------------------------------------------


def test_png_renders_when_the_viz_extra_is_installed() -> None:
    layout = build_layout(_chain_graph())
    png = render_png(layout)
    if not png_available():  # pragma: no cover - depends on the optional extra
        assert png is None
        return
    assert png is not None
    assert png[:4] == bytes([137, 80, 78, 71])  # PNG magic number
    assert len(png) > 1000


# --- the SVG carries object identity, not only a picture -----------------------------------


def test_each_node_group_carries_the_full_object_name_and_type() -> None:
    """The visible label is clipped, so the box alone does not say which object it is.

    Without identity in the markup the SVG is a picture and nothing more: a consumer wanting to
    attach a click, a tooltip or a link has to re-derive a layout of its own, and the two then
    disagree about everything except which objects exist.
    """
    svg = render_svg(build_layout(_chain_graph()))
    root = ElementTree.fromstring(svg)  # well-formedness is part of the assertion
    groups = [g for g in root.iter() if g.get("class") == "bw-node"]
    assert len(groups) == len(_chain_graph().nodes)
    identified = {g.get("data-node-id"): g.get("data-node-type") for g in groups}
    assert identified == {
        "DS": "datasource",
        "STG": "dso",
        "EDW": "adso",
        "CP": "compositeprovider",
        "QUERY": "query",
        "LOOKUP": "dso",
    }


def test_a_long_name_survives_in_the_data_attribute_even_when_the_label_is_clipped() -> None:
    long_name = "VERY_LONG_OBJECT_NAME_" + ("X" * 60)
    graph = LineageGraph(
        root_id=long_name,
        direction="upstream",
        depth=1,
        nodes=[
            LineageNode(
                id=long_name,
                object_type="adso",
                name=long_name,
                provenance=Provenance(source_table="RSOADSO", source_key={"ADSONM": long_name}),
            )
        ],
        node_count=1,
    )
    svg = render_svg(build_layout(graph))
    root = ElementTree.fromstring(svg)
    group = next(g for g in root.iter() if g.get("class") == "bw-node")
    assert group.get("data-node-id") == long_name, "the clipped label must not become the identity"
    drawn = "".join(t.text or "" for t in group.iter() if t.tag.endswith("text"))
    assert long_name not in drawn, "the fixture must actually exercise clipping"


def test_the_root_is_marked_so_a_consumer_can_find_it() -> None:
    svg = render_svg(build_layout(_chain_graph()))
    root = ElementTree.fromstring(svg)
    marked = [
        g.get("data-node-id")
        for g in root.iter()
        if g.get("class") == "bw-node" and g.get("data-node-root")
    ]
    assert marked == ["EDW"], "the root is what the caller asked about, not the leftmost node"


def test_each_edge_names_its_endpoints_and_kind() -> None:
    svg = render_svg(build_layout(_chain_graph()))
    root = ElementTree.fromstring(svg)
    edges = {
        (p.get("data-edge-src"), p.get("data-edge-dst"), p.get("data-edge-kind"))
        for p in root.iter()
        if p.get("class") == "bw-edge"
    }
    assert ("STG", "EDW", "transformation") in edges
    assert ("EDW", "CP", "composite_part") in edges
    assert ("LOOKUP", "EDW", "routine_lookup") in edges, "an advisory edge is identified too"


def test_a_self_loop_is_identified_like_any_other_edge() -> None:
    """The edge a reader most wants to interrogate: source and target are the same object.

    It is drawn by a separate branch, which is how it came to be the one edge with no identity.
    """
    graph = LineageGraph(
        root_id="LOOP_DSO",
        direction="both",
        depth=1,
        nodes=[_node("LOOP_DSO", "dso")],
        edges=[_edge("LOOP_DSO", "LOOP_DSO")],
        node_count=1,
        edge_count=1,
    )
    svg = render_svg(build_layout(graph))
    root = ElementTree.fromstring(svg)
    edges = [p for p in root.iter() if p.get("class") == "bw-edge"]
    assert len(edges) == 1
    assert edges[0].get("data-edge-src") == "LOOP_DSO"
    assert edges[0].get("data-edge-dst") == "LOOP_DSO"


def test_the_hover_title_gives_the_untruncated_name() -> None:
    svg = render_svg(build_layout(_chain_graph()))
    root = ElementTree.fromstring(svg)
    group = next(
        g for g in root.iter() if g.get("class") == "bw-node" and g.get("data-node-id") == "DS"
    )
    title = next(t for t in group if t.tag.endswith("title"))
    assert title.text is not None
    assert "DS" in title.text and "DataSource" in title.text


def test_identity_attributes_are_escaped_so_a_hostile_name_cannot_break_the_document() -> None:
    hostile = 'A"><script>x</script>'
    graph = LineageGraph(
        root_id=hostile,
        direction="upstream",
        depth=1,
        nodes=[
            LineageNode(
                id=hostile,
                object_type="dso",
                name=hostile,
                provenance=Provenance(source_table="RSDODSO", source_key={"ODSOBJECT": hostile}),
            )
        ],
        node_count=1,
    )
    svg = render_svg(build_layout(graph))
    assert "<script>" not in svg
    root = ElementTree.fromstring(svg)  # would raise if the attribute broke the markup
    group = next(g for g in root.iter() if g.get("class") == "bw-node")
    assert group.get("data-node-id") == hostile
