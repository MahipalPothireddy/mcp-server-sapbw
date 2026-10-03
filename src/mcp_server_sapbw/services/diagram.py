"""Data-flow diagram rendering: layered layout plus SVG and PNG output.

Turns a :class:`~mcp_server_sapbw.models.lineage.LineageGraph` into a picture an analyst can read
at a glance:

* **Layered layout** — nodes are assigned to columns by longest-path depth from the sources, so data
  flows strictly left-to-right and every edge points forward. Within a column, nodes are ordered by
  their neighbours' positions (a single barycentre pass) to reduce crossings. The layout is
  *deterministic*: the same graph always renders identically, which makes diagrams diffable.
* **Type coding** — each BW object type gets its own fill colour and shape, so the shape of a flow
  (DataSource -> DSO stack -> CompositeProvider -> query) is legible without reading names.
* **Honesty in the picture** — advisory edges (routine-derived, or naming-convention resolutions)
  are dashed and greyed, so a guess never looks like a declared fact. Edge labels carry the hop kind
  and update mode; a truncated graph says so on the canvas, not just in the payload.

Rendering is entirely local: SVG is generated from stdlib only, and PNG uses Pillow if the ``viz``
extra is installed. Nothing is sent to a hosted renderer — diagram content includes customer object
names and must not leave the machine (mission Rule 4).
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any, Literal

from ..models.lineage import LineageEdge, LineageGraph, LineageNodeType

ImageFormat = Literal["svg", "png"]

# --- visual vocabulary --------------------------------------------------------------------

# Fill / border per node type. Chosen for contrast on white and to survive greyscale printing.
# Colour separates *families* rather than individual types - stores, movers, reporting, boundary -
# because the type is always written in the box, so colour's job is to make the shape of a flow
# legible at a glance. Every member of LineageNodeType must appear: a type missing from this table
# used to fall through to grey and render labelled "object", which is how a DTP appeared in a
# production diagram as an unnamed grey box. tests/test_diagram_mermaid.py holds it exhaustive.
_NODE_STYLE: dict[LineageNodeType, tuple[str, str]] = {
    # Boundary - where data enters BW.
    "datasource": ("#fde68a", "#b45309"),  # amber - the source-system boundary
    "infosource": ("#fed7aa", "#c2410c"),
    "transfer_structure": ("#ffedd5", "#ea580c"),  # BW 3.x sibling of the InfoSource
    "source_object": ("#fef3c7", "#92400e"),  # lives in the source system, not in BW
    # Stores - objects that persist or expose data.
    "dso": ("#bfdbfe", "#1d4ed8"),  # blue - persisted staging/EDW
    "adso": ("#c7d2fe", "#4338ca"),
    "infocube": ("#ddd6fe", "#6d28d9"),  # violet - aggregated
    "virtualprovider": ("#ede9fe", "#5b21b6"),
    "multiprovider": ("#e9d5ff", "#7e22ce"),
    "compositeprovider": ("#bbf7d0", "#15803d"),  # green - virtual consumption layer
    "infoobject": ("#fecdd3", "#be123c"),  # rose - master data
    "calcview": ("#d9f99d", "#4d7c0f"),  # lime - HANA
    # Movers - orchestration and logic. One neutral fill on purpose: these are not data, and
    # telling a DTP from a transformation matters far less than telling either from a provider.
    "transformation": ("#eef2f7", "#334155"),
    "dtp": ("#eef2f7", "#475569"),
    "infopackage": ("#eef2f7", "#64748b"),
    "update_rule": ("#eef2f7", "#94a3b8"),  # BW 3.x equivalent of a transformation
    "chain": ("#eef2f7", "#1e293b"),
    # Reporting.
    "query": ("#a5f3fc", "#0e7490"),  # cyan - reporting
    "query_element": ("#cffafe", "#0e7490"),
    "report": ("#bae6fd", "#0369a1"),
    # Gaps. Red because an unresolved dependency is a finding, not a neutral node.
    "unresolved_dependency": ("#fee2e2", "#b91c1c"),
    "unknown": ("#e5e7eb", "#6b7280"),
}
_DEFAULT_STYLE = ("#e5e7eb", "#6b7280")

# Human labels for the legend and the node subtitle (only types actually present are drawn).
_TYPE_LABEL: dict[LineageNodeType, str] = {
    "datasource": "DataSource",
    "infosource": "InfoSource",
    "transfer_structure": "Transfer structure",
    "source_object": "Source object",
    "dso": "DSO",
    "adso": "Advanced DSO",
    "infocube": "InfoCube",
    "virtualprovider": "VirtualProvider",
    "multiprovider": "MultiProvider",
    "compositeprovider": "CompositeProvider",
    "infoobject": "InfoObject",
    "calcview": "Calc view",
    "transformation": "Transformation",
    "dtp": "DTP",
    "infopackage": "InfoPackage",
    "update_rule": "Update rule",
    "chain": "Process chain",
    "query": "Query",
    "query_element": "Query element",
    "report": "Report",
    "unresolved_dependency": "Unresolved",
    "unknown": "Unknown",
}

# Geometry (px).
_NODE_W = 190
_NODE_H = 46
_H_GAP = 96  # horizontal gap between columns (room for edge labels)
_V_GAP = 22
_MARGIN = 28
_HEADER_H = 54
_LEGEND_H = 30
_FONT = "Segoe UI, Helvetica, Arial, sans-serif"
_MAX_LABEL_CHARS = 26


@dataclass
class LayoutNode:
    """A node placed on the canvas."""

    id: str
    label: str
    node_type: LineageNodeType
    layer: int
    x: float = 0.0
    y: float = 0.0
    is_root: bool = False


@dataclass
class DiagramLayout:
    """A positioned graph ready to render."""

    nodes: list[LayoutNode] = field(default_factory=list)
    edges: list[LineageEdge] = field(default_factory=list)
    width: int = 0
    height: int = 0
    title: str = ""
    subtitle: str = ""
    truncated: bool = False
    layer_count: int = 0

    @property
    def by_id(self) -> dict[str, LayoutNode]:
        return {n.id: n for n in self.nodes}


def _shorten(text: str, limit: int = _MAX_LABEL_CHARS) -> str:
    """Normalise whitespace, then clip to a readable width.

    BW stores DataSource endpoints space-padded as ``<DATASOURCE><padding><LOGSYS>``. Collapsing the
    padding keeps the logical system visible (which is useful) instead of pushing it past the clip
    and leaving a bare ellipsis.
    """
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "\u2026"


def _assign_layers(node_ids: list[str], edges: list[LineageEdge]) -> dict[str, int]:
    """Longest-path layering: a node sits one column right of its furthest predecessor.

    Cycles are broken by processing in a stable order and capping iterations, so a circular
    dependency (which BW does allow) cannot hang the layout.
    """
    incoming: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in edges:
        if edge.src in incoming and edge.dst in incoming and edge.src != edge.dst:
            incoming[edge.dst].append(edge.src)

    layer = dict.fromkeys(node_ids, 0)
    for _ in range(len(node_ids)):  # bounded relaxation; converges for a DAG, safe for cycles
        changed = False
        for nid in node_ids:
            if not incoming[nid]:
                continue
            want = max(layer[src] for src in incoming[nid]) + 1
            if want > layer[nid]:
                layer[nid] = want
                changed = True
        if not changed:
            break
    return layer


def _order_within_layers(
    layers: dict[int, list[str]], edges: list[LineageEdge], positions: dict[str, int]
) -> None:
    """One barycentre pass: order each column by the mean position of its neighbours."""
    neighbours: dict[str, list[str]] = {}
    for edge in edges:
        neighbours.setdefault(edge.dst, []).append(edge.src)
        neighbours.setdefault(edge.src, []).append(edge.dst)
    for _, members in sorted(layers.items()):
        members.sort(
            key=lambda nid: (
                sum(positions.get(n, 0) for n in neighbours.get(nid, []))
                / max(len(neighbours.get(nid, [])), 1),
                nid,
            )
        )
        for index, nid in enumerate(members):
            positions[nid] = index


#: Vertical slack added when the graph contains an edge spanning more than one column. Half lands
#: above the columns and half below, each half wide enough to hold a corridor and its clearance.
_ROUTE_BAND = 44


def _needs_route_band(edges: list[LineageEdge], layer_of: dict[str, int]) -> bool:
    """Whether any edge spans more than one column, and so has to be routed around boxes."""
    return any(
        edge.src != edge.dst
        and edge.src in layer_of
        and edge.dst in layer_of
        and abs(layer_of[edge.dst] - layer_of[edge.src]) > 1
        for edge in edges
    )


def build_layout(graph: LineageGraph, *, title: str = "", subtitle: str = "") -> DiagramLayout:
    """Position a lineage graph into deterministic left-to-right layers."""
    node_ids = [n.id for n in graph.nodes]
    types = {n.id: n.object_type for n in graph.nodes}
    names = {n.id: n.name for n in graph.nodes}
    layer_of = _assign_layers(node_ids, graph.edges)

    layers: dict[int, list[str]] = {}
    for nid in node_ids:
        layers.setdefault(layer_of[nid], []).append(nid)
    for members in layers.values():
        members.sort()
    positions = {nid: i for members in layers.values() for i, nid in enumerate(members)}
    _order_within_layers(layers, graph.edges, positions)

    rows = max((len(m) for m in layers.values()), default=1)
    width = _MARGIN * 2 + len(layers) * _NODE_W + max(len(layers) - 1, 0) * _H_GAP
    height = _MARGIN * 2 + _HEADER_H + _LEGEND_H + rows * _NODE_H + max(rows - 1, 0) * _V_GAP
    if _needs_route_band(graph.edges, layer_of):
        # Slack for edges that have to be routed around boxes. Columns are centred in the content
        # area, so this appears as clearance above and below every column - which is where a
        # corridor goes. Without it a graph one row deep has nowhere to route and the edge would be
        # drawn through the boxes between its endpoints. Added only when such an edge exists, so
        # diagrams that never needed routing keep the canvas they had.
        height += _ROUTE_BAND

    nodes: list[LayoutNode] = []
    for layer_index, members in sorted(layers.items()):
        column_h = len(members) * _NODE_H + max(len(members) - 1, 0) * _V_GAP
        top = _MARGIN + _HEADER_H + (height - _MARGIN * 2 - _HEADER_H - _LEGEND_H - column_h) / 2
        for row_index, nid in enumerate(members):
            nodes.append(
                LayoutNode(
                    id=nid,
                    label=_shorten(names.get(nid, nid)),
                    node_type=types.get(nid, "unknown"),
                    layer=layer_index,
                    x=_MARGIN + layer_index * (_NODE_W + _H_GAP),
                    y=top + row_index * (_NODE_H + _V_GAP),
                    is_root=nid == graph.root_id,
                )
            )
    return DiagramLayout(
        nodes=nodes,
        edges=list(graph.edges),
        width=int(width),
        height=int(height),
        title=title or f"Data flow: {names.get(graph.root_id, graph.root_id)}",
        subtitle=subtitle,
        truncated=graph.truncated,
        layer_count=len(layers),
    )


# --- edge routing -------------------------------------------------------------------------
#
# An edge between *adjacent* columns cannot cross a box: the only space between them is the gutter,
# and the gutter holds no boxes. An edge spanning more than one column has boxes in its way, and a
# single bezier drawn between the two endpoints runs flat through them - which is how a production
# figure came to read as if an ADSO fed a DataSource it has no relationship with. Measured on that
# graph: 4 of 11 edges passed through the rectangle of a node they were not connected to, one of
# them through three.
#
# So a multi-column edge is routed through a horizontal corridor that is free of boxes across the
# span it traverses, turning into and out of that corridor inside the gutters either side - where,
# again, there are no boxes. Non-crossing is then a property of the construction rather than
# something to verify afterwards. Adjacent-column edges keep their original single-curve shape, so
# this changes only the diagrams that were wrong.

_Point = tuple[float, float]
_Segment = tuple[_Point, _Point, _Point, _Point]

_DIRECT_POINTS = 2  # start and end: the shape an edge between adjacent columns keeps
_ROUTED_POINTS = 4  # start, two turns, end: an edge taken through a corridor
_BEND = _H_GAP * 0.5  # how far into the gutter an edge turns toward its corridor
_CORRIDOR_PAD = 6.0  # clearance kept between a routed run and the boxes bounding its band
_CORRIDOR_MIN = 14.0  # a band narrower than this cannot hold a line plus that clearance
_LANE_STEP = 9.0  # separation between parallel edges of the same pair
_LABEL_LANE_STEP = 11.0  # and between their labels, which otherwise print on top of each other


def _free_bands(layout: DiagramLayout, x_lo: float, x_hi: float) -> list[_Point]:
    """Horizontal bands spanning ``[x_lo, x_hi]`` that no node box occupies."""
    occupied = sorted(
        (node.y, node.y + _NODE_H)
        for node in layout.nodes
        if node.x < x_hi and node.x + _NODE_W > x_lo
    )
    merged: list[list[float]] = []
    for top, bottom in occupied:
        if merged and top <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bottom)
        else:
            merged.append([top, bottom])
    bands: list[_Point] = []
    cursor = float(_MARGIN + _HEADER_H)
    for top, bottom in merged:
        if top - cursor >= _CORRIDOR_MIN:
            bands.append((cursor, top))
        cursor = max(cursor, bottom)
    content_bottom = float(layout.height - _MARGIN - _LEGEND_H)
    if content_bottom - cursor >= _CORRIDOR_MIN:
        bands.append((cursor, content_bottom))
    return bands


def _corridor_y(layout: DiagramLayout, x_lo: float, x_hi: float, y_target: float) -> float | None:
    """The free band closest to ``y_target``, or ``None`` when every band is too narrow."""
    bands = _free_bands(layout, x_lo, x_hi)
    if not bands:
        return None

    def within(band: _Point) -> float:
        top, bottom = band
        return min(max(y_target, top + _CORRIDOR_PAD), bottom - _CORRIDOR_PAD)

    return within(min(bands, key=lambda band: abs(within(band) - y_target)))


def route_edge(
    layout: DiagramLayout, src: LayoutNode, dst: LayoutNode, *, lane: int = 0
) -> list[_Point]:
    """Waypoints for one edge: two for a direct hop, four when it is routed via a corridor.

    ``lane`` separates parallel edges of the same pair - a transformation and the routine lookup
    between the same two objects are two facts, and drawn on one line they read as one.
    """
    x1, y1 = src.x + _NODE_W, src.y + _NODE_H / 2
    x2, y2 = dst.x, dst.y + _NODE_H / 2
    if x2 < x1:  # backward edge (cycle): leave from the left, arrive on the right
        x1, y1 = src.x, src.y + _NODE_H / 2
        x2, y2 = dst.x + _NODE_W, dst.y + _NODE_H / 2
    if abs(dst.layer - src.layer) <= 1:
        return [(x1, y1), (x2, y2)]
    step = _BEND if x2 >= x1 else -_BEND
    bend_out, bend_in = x1 + step, x2 - step
    corridor = _corridor_y(
        layout,
        min(bend_out, bend_in),
        max(bend_out, bend_in),
        (y1 + y2) / 2 + lane * _LANE_STEP,
    )
    if corridor is None:
        # Every band across the span is occupied. Nothing to do but go direct; a dense graph is
        # drawn as it was before rather than routed somewhere that would also cross a box.
        return [(x1, y1), (x2, y2)]
    return [(x1, y1), (bend_out, corridor), (bend_in, corridor), (x2, y2)]


def _route_segments(points: list[_Point], *, bow: float = 0.0) -> list[_Segment]:
    """The route as cubic segments, so SVG and PNG draw one shape from one source."""
    if len(points) == _DIRECT_POINTS:
        (x1, y1), (x2, y2) = points
        mid = (x1 + x2) / 2
        return [((x1, y1), (mid, y1 + bow), (mid, y2 + bow), (x2, y2))]
    (x1, y1), (bend_out, corridor), (bend_in, corridor_end), (x2, y2) = points
    first_mid = (x1 + bend_out) / 2
    last_mid = (bend_in + x2) / 2
    return [
        # Turn into the corridor, entirely inside the gutter beside the source column.
        ((x1, y1), (first_mid, y1), (first_mid, corridor), (bend_out, corridor)),
        # The run itself: a straight line expressed as a cubic so there is one segment type.
        (
            (bend_out, corridor),
            (bend_out, corridor),
            (bend_in, corridor_end),
            (bend_in, corridor_end),
        ),
        # And out again, inside the gutter beside the target column.
        ((bend_in, corridor_end), (last_mid, corridor_end), (last_mid, y2), (x2, y2)),
    ]


def _num(value: float) -> str:
    """Trim a coordinate for the path data: ``173.0`` carries no more meaning than ``173``."""
    return f"{value:g}"


def _svg_edge_path(points: list[_Point], *, bow: float = 0.0) -> str:
    segments = _route_segments(points, bow=bow)
    start = segments[0][0]
    parts = [f"M{_num(start[0])},{_num(start[1])}"]
    for _begin, control_a, control_b, end in segments:
        parts.append(
            f" C{_num(control_a[0])},{_num(control_a[1])} "
            f"{_num(control_b[0])},{_num(control_b[1])} {_num(end[0])},{_num(end[1])}"
        )
    return "".join(parts)


def _flatten_route(points: list[_Point], *, bow: float = 0.0, steps: int = 16) -> list[_Point]:
    """Sample the route into a polyline, for the raster renderer and for overlap tests."""
    flat: list[_Point] = []
    for begin, control_a, control_b, end in _route_segments(points, bow=bow):
        for index in range(steps + 1):
            t = index / steps
            inv = 1 - t
            flat.append(
                (
                    inv**3 * begin[0]
                    + 3 * inv**2 * t * control_a[0]
                    + 3 * inv * t**2 * control_b[0]
                    + t**3 * end[0],
                    inv**3 * begin[1]
                    + 3 * inv**2 * t * control_a[1]
                    + 3 * inv * t**2 * control_b[1]
                    + t**3 * end[1],
                )
            )
    return flat


def _route_label_spot(
    layout: DiagramLayout, points: list[_Point], *, lane: int = 0
) -> _Point | None:
    """Where to write an edge's label: on the corridor run, or the old search for a direct hop."""
    offset = lane * _LABEL_LANE_STEP
    if len(points) == _ROUTED_POINTS:
        # The corridor is free of boxes by construction, so its midpoint needs no collision test.
        (_x1, _y1), (bend_out, corridor), (bend_in, _corridor_end), (_x2, _y2) = points
        return (bend_out + bend_in) / 2, corridor + offset
    (x1, y1), (x2, y2) = points
    spot = _label_spot(layout, x1, y1 + lane * _LANE_STEP, x2, y2 + lane * _LANE_STEP)
    return None if spot is None else (spot[0], spot[1] + offset)


def _label_spot(
    layout: DiagramLayout, x1: float, y1: float, x2: float, y2: float
) -> tuple[float, float] | None:
    """A clear point to place an edge label, or ``None`` if every candidate overlaps a node.

    Long edges span columns and their midpoint often lands on top of an unrelated box, which is how
    a label ends up unreadable. Candidates are tried from the source end outward and rejected if
    they fall inside any node rectangle.
    """
    for fraction in (0.5, 0.28, 0.72, 0.15):
        x = x1 + (x2 - x1) * fraction
        y = y1 + (y2 - y1) * fraction
        if not any(
            node.x - 6 <= x <= node.x + _NODE_W + 6 and node.y - 4 <= y <= node.y + _NODE_H + 4
            for node in layout.nodes
        ):
            return x, y
    return None


def _edge_label(edge: LineageEdge) -> str:
    parts: list[str] = []
    if edge.kind == "routine_lookup":
        parts.append("routine")
    elif edge.kind == "composite_part":
        parts.append("part of")
    elif edge.kind == "dtp":
        parts.append("DTP")
    elif edge.kind == "multiprovider_part":
        parts.append("part of")
    # Every mode, not just the significant one: a diagram labelled 'delta' on a pair that also has
    # a full DTP tells the reader the opposite of the risk-relevant fact.
    if edge.update_modes:
        parts.append("+".join(edge.update_modes))
    elif edge.update_mode:
        parts.append(edge.update_mode)
    return " / ".join(parts)


# --- SVG ----------------------------------------------------------------------------------


def _style_for(node_type: LineageNodeType) -> tuple[str, str]:
    return _NODE_STYLE.get(node_type, _DEFAULT_STYLE)


def render_svg(layout: DiagramLayout) -> str:
    """A self-contained SVG document for the layout (stdlib only, no external references)."""
    by_id = layout.by_id
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" '
        f'height="{layout.height}" viewBox="0 0 {layout.width} {layout.height}" '
        f'font-family="{_FONT}">',
        f'<rect width="{layout.width}" height="{layout.height}" fill="#ffffff"/>',
        # Arrow markers: solid for declared edges, grey for advisory ones.
        "<defs>"
        '<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#475569"/></marker>'
        '<marker id="b" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"/></marker>'
        "</defs>",
        f'<text x="{_MARGIN}" y="30" font-size="17" font-weight="600" fill="#0f172a">'
        f"{html.escape(layout.title)}</text>",
    ]
    if layout.subtitle:
        out.append(
            f'<text x="{_MARGIN}" y="47" font-size="11.5" fill="#64748b">'
            f"{html.escape(layout.subtitle)}</text>"
        )

    # Edges first so nodes paint over the line ends.
    lanes: dict[tuple[str, str], int] = {}
    for edge in layout.edges:
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None:
            continue
        advisory = edge.confidence == "advisory"
        stroke = "#94a3b8" if advisory else "#475569"
        dash = ' stroke-dasharray="6 4"' if advisory else ""
        marker = "b" if advisory else "a"
        identity = (
            f'class="bw-edge" data-edge-src="{html.escape(edge.src, quote=True)}" '
            f'data-edge-dst="{html.escape(edge.dst, quote=True)}" '
            f'data-edge-kind="{html.escape(edge.kind, quote=True)}"'
        )
        if src.id == dst.id:  # self-loop: the object derives from itself
            cx, cy = src.x + _NODE_W, src.y + _NODE_H / 2
            path = f"M{cx},{cy - 10} C{cx + 44},{cy - 34} {cx + 44},{cy + 34} {cx},{cy + 10}"
            # Identified like any other edge. It was not, and a self-loop is exactly the edge a
            # reader most wants to interrogate: a transformation whose source and target are the
            # same object makes the loaded result depend on load order.
            out.append(
                f"<path {identity} "
                f'd="{path}" fill="none" stroke="{stroke}" stroke-width="1.6"{dash} '
                f'marker-end="url(#{marker})"/>'
            )
            out.append(
                f'<text x="{cx + 48}" y="{cy + 4}" font-size="10" fill="#b91c1c">self</text>'
            )
            continue
        lane_key = (edge.src, edge.dst)
        lane = lanes.get(lane_key, 0)
        lanes[lane_key] = lane + 1
        points = route_edge(layout, src, dst, lane=lane)
        bow = lane * _LANE_STEP if len(points) == _DIRECT_POINTS else 0.0
        path = _svg_edge_path(points, bow=bow)
        out.append(
            f"<path {identity} "
            f'd="{path}" fill="none" stroke="{stroke}" stroke-width="1.6"{dash} '
            f'marker-end="url(#{marker})"/>'
        )
        label = _edge_label(edge)
        spot = _route_label_spot(layout, points, lane=lane) if label else None
        if label and spot is not None:
            out.append(
                f'<text x="{spot[0]}" y="{spot[1] - 4}" font-size="9.5" fill="#64748b" '
                f'text-anchor="middle">{html.escape(label)}</text>'
            )

    for node in layout.nodes:
        fill, border = _style_for(node.node_type)
        width = 2.4 if node.is_root else 1.3
        # Grouped, and carrying the object's *full* name and type as data attributes. The visible
        # label is clipped for legibility, so without this the rendered box does not say which
        # object it is - which makes the SVG unusable as anything but a picture. A consumer that
        # wants to attach behaviour (a click, a tooltip, a link into a report) can now do it against
        # the same layout the PNG and the Mermaid come from, rather than re-deriving one that would
        # disagree. Nothing external is referenced, so the file stays self-contained.
        out.append(
            f'<g class="bw-node" data-node-id="{html.escape(node.id, quote=True)}" '
            f'data-node-type="{html.escape(node.node_type, quote=True)}"'
            + (' data-node-root="1"' if node.is_root else "")
            + ">"
        )
        out.append(
            f"<title>{html.escape(node.id)} "
            f"({html.escape(_TYPE_LABEL.get(node.node_type, 'object'))})</title>"
        )
        out.append(
            f'<rect x="{node.x}" y="{node.y}" width="{_NODE_W}" height="{_NODE_H}" rx="7" '
            f'fill="{fill}" stroke="{border}" stroke-width="{width}"/>'
        )
        out.append(
            f'<text x="{node.x + _NODE_W / 2}" y="{node.y + 20}" font-size="12" '
            f'font-weight="{"700" if node.is_root else "500"}" fill="#0f172a" '
            f'text-anchor="middle">{html.escape(node.label)}</text>'
        )
        out.append(
            f'<text x="{node.x + _NODE_W / 2}" y="{node.y + 35}" font-size="9.5" fill="#475569" '
            f'text-anchor="middle">{html.escape(_TYPE_LABEL.get(node.node_type, "object"))}</text>'
        )
        out.append("</g>")

    out.append(_svg_legend(layout))
    out.append("</svg>")
    return "\n".join(out)


def _svg_legend(layout: DiagramLayout) -> str:
    """Legend strip: only the types present, plus the advisory-edge convention."""
    y = layout.height - _MARGIN + 4
    present: list[LineageNodeType] = []
    for node in layout.nodes:
        if node.node_type not in present:
            present.append(node.node_type)
    parts: list[str] = []
    x = _MARGIN
    for node_type in present:
        fill, border = _style_for(node_type)
        label = _TYPE_LABEL.get(node_type, "object")
        parts.append(
            f'<rect x="{x}" y="{y - 9}" width="11" height="11" rx="2" fill="{fill}" '
            f'stroke="{border}"/>'
            f'<text x="{x + 16}" y="{y}" font-size="10" fill="#334155">{html.escape(label)}</text>'
        )
        x += 26 + len(label) * 6
    parts.append(
        f'<line x1="{x + 4}" y1="{y - 4}" x2="{x + 32}" y2="{y - 4}" stroke="#94a3b8" '
        'stroke-width="1.6" stroke-dasharray="6 4"/>'
        f'<text x="{x + 38}" y="{y}" font-size="10" fill="#334155">advisory (heuristic)</text>'
    )
    if layout.truncated:
        parts.append(
            f'<text x="{layout.width - _MARGIN}" y="{y}" font-size="10" fill="#b91c1c" '
            'text-anchor="end">graph truncated - not all objects shown</text>'
        )
    return "".join(parts)


# --- PNG ----------------------------------------------------------------------------------


def png_available() -> bool:
    """True when the optional ``viz`` extra (Pillow) is installed."""
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def render_png(layout: DiagramLayout, *, scale: int = 2) -> bytes | None:
    """Rasterise the layout locally with Pillow, or ``None`` if the ``viz`` extra is absent.

    Drawn directly rather than by converting the SVG, so no SVG engine is needed. ``scale``
    supersamples the canvas so text stays crisp in chat clients that downscale images.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
    except ImportError:
        return None
    import io  # noqa: PLC0415

    from .diagram_fonts import load_fonts  # noqa: PLC0415

    s = max(1, int(scale))
    image = Image.new("RGB", (layout.width * s, layout.height * s), "#ffffff")
    draw = ImageDraw.Draw(image)
    fonts = load_fonts(s)
    by_id = layout.by_id

    draw.text((_MARGIN * s, 14 * s), layout.title, fill="#0f172a", font=fonts["title"])
    if layout.subtitle:
        draw.text((_MARGIN * s, 38 * s), layout.subtitle, fill="#64748b", font=fonts["small"])

    lanes: dict[tuple[str, str], int] = {}
    for edge in layout.edges:
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None or src.id == dst.id:
            continue
        advisory = edge.confidence == "advisory"
        colour = "#94a3b8" if advisory else "#475569"
        lane_key = (edge.src, edge.dst)
        lane = lanes.get(lane_key, 0)
        lanes[lane_key] = lane + 1
        # Routed in layout units - the same waypoints the SVG uses - and scaled only when drawn, so
        # the raster and the vector cannot disagree about where an edge goes.
        points = route_edge(layout, src, dst, lane=lane)
        bow = lane * _LANE_STEP if len(points) == _DIRECT_POINTS else 0.0
        _draw_connector(draw, points, colour, s, dashed=advisory, bow=bow)
        label = _edge_label(edge)
        spot = _route_label_spot(layout, points, lane=lane) if label else None
        if label and spot is not None:
            draw.text(
                (spot[0] * s, spot[1] * s - 5 * s),
                label,
                fill="#64748b",
                font=fonts["small"],
                anchor="ms",
            )

    for node in layout.nodes:
        fill, border = _style_for(node.node_type)
        box = (node.x * s, node.y * s, (node.x + _NODE_W) * s, (node.y + _NODE_H) * s)
        draw.rounded_rectangle(
            box, radius=7 * s, fill=fill, outline=border, width=(3 if node.is_root else 2) * s
        )
        draw.text(
            ((node.x + _NODE_W / 2) * s, (node.y + 14) * s),
            node.label,
            fill="#0f172a",
            font=fonts["node"],
            anchor="mm",
        )
        draw.text(
            ((node.x + _NODE_W / 2) * s, (node.y + 32) * s),
            _TYPE_LABEL.get(node.node_type, "object"),
            fill="#475569",
            font=fonts["small"],
            anchor="mm",
        )

    _png_legend(draw, layout, fonts, s)
    if layout.truncated:
        draw.text(
            ((layout.width - _MARGIN) * s, (layout.height - _MARGIN) * s),
            "graph truncated - not all objects shown",
            fill="#b91c1c",
            font=fonts["small"],
            anchor="rs",
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _png_legend(draw: Any, layout: DiagramLayout, fonts: dict[str, Any], scale: int) -> None:
    """Legend strip for the raster output, mirroring the SVG one."""
    y = (layout.height - _MARGIN + 4) * scale
    present: list[LineageNodeType] = []
    for node in layout.nodes:
        if node.node_type not in present:
            present.append(node.node_type)
    x = _MARGIN * scale
    swatch = 11 * scale
    for node_type in present:
        fill, border = _style_for(node_type)
        label = _TYPE_LABEL.get(node_type, "object")
        draw.rounded_rectangle(
            (x, y - swatch, x + swatch, y),
            radius=2 * scale,
            fill=fill,
            outline=border,
            width=max(1, scale),
        )
        draw.text(
            (x + swatch + 5 * scale, y), label, fill="#334155", font=fonts["small"], anchor="ls"
        )
        x += (26 + len(label) * 6) * scale
    # Advisory-edge convention: a dashed sample line.
    dash_y = y - 4 * scale
    for step in range(0, 28 * scale, 8 * scale):
        draw.line(
            [(x + 4 * scale + step, dash_y), (x + 4 * scale + step + 4 * scale, dash_y)],
            fill="#94a3b8",
            width=max(1, 2 * scale) - 1,
        )
    draw.text(
        (x + 38 * scale, y),
        "advisory (heuristic)",
        fill="#334155",
        font=fonts["small"],
        anchor="ls",
    )


def _draw_connector(
    draw: Any,
    points: list[_Point],
    colour: str,
    scale: int,
    *,
    dashed: bool,
    bow: float = 0.0,
) -> None:
    """Draw a routed connector as a flattened polyline, dashed when advisory.

    Takes the waypoints rather than two endpoints so a corridor-routed edge rasterises as the shape
    the vector output draws. The arrow head follows the final segment's direction instead of
    assuming a left-to-right arrival, which also fixes the head on a backward (cycle) edge.
    """
    flat = [(x * scale, y * scale) for x, y in _flatten_route(points, bow=bow)]
    for index in range(len(flat) - 1):
        if dashed and index % 2:
            continue  # gap
        draw.line([flat[index], flat[index + 1]], fill=colour, width=max(1, 2 * scale) - 1)
    tip, before = flat[-1], flat[-2]
    dx, dy = tip[0] - before[0], tip[1] - before[1]
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    head = 5 * scale
    base = (tip[0] - ux * head * 1.6, tip[1] - uy * head * 1.6)
    draw.polygon(
        [
            tip,
            (base[0] - uy * head * 0.75, base[1] + ux * head * 0.75),
            (base[0] + uy * head * 0.75, base[1] - ux * head * 0.75),
        ],
        fill=colour,
    )


# --- Mermaid ------------------------------------------------------------------------------
#
# A third output from the same layout, for the places a picture cannot go: a markdown file, a wiki
# page, a chat reply. It reuses the layering, the label hygiene, the type colours and the edge
# labels above rather than re-deriving them, because a diagram that disagrees with the PNG of the
# same graph is worse than having only one of them.

# Nodes rendered before the diagram stops being something a person can read. A lineage graph is
# bounded at 400 nodes, and 400 boxes of Mermaid is a wall, not a diagram - so this bound exists for
# legibility, is separate from the graph's own bound, and is reported on the canvas when it binds.
_MERMAID_MAX_NODES = 80
# Above this, per-stage grouping stops helping: twenty labelled boxes around a hairball is noise.
_MERMAID_MAX_SUBGRAPH_NODES = 60
# Stage bands only mean something once there are at least two of them to compare.
_MERMAID_MIN_STAGES = 2

#: Mermaid reserves these in label position. `#` first - the replacements themselves contain one.
_MERMAID_ESCAPES: tuple[tuple[str, str], ...] = (
    ("#", "#35;"),
    ('"', "#quot;"),
    ("<", "#lt;"),
    (">", "#gt;"),
)


def _mermaid_label(text: str, *, limit: int = _MAX_LABEL_CHARS) -> str:
    """A BW object name safe to place inside a quoted Mermaid label.

    Two distinct jobs. Whitespace collapsing and clipping are the same readability treatment the
    image path applies - a DataSource endpoint is stored space-padded as
    ``<DATASOURCE><padding><LOGSYS>``, and pasted raw it produces a box wider than the rest of the
    diagram put together. Escaping is a correctness matter: an unescaped ``"`` closes the label
    early and Mermaid then fails to parse the **whole** diagram, so one odd name would cost the
    entire picture rather than one node. BW names are not expected to contain these characters, but
    node names also arrive from ABAP parse output, where that is an assumption about data rather
    than a guarantee.
    """
    out = _shorten(text, limit)
    for char, replacement in _MERMAID_ESCAPES:
        out = out.replace(char, replacement)
    return out


def _mermaid_class_defs(present: list[LineageNodeType]) -> list[str]:
    """One ``classDef`` per object type actually drawn, from the shared colour vocabulary."""
    lines: list[str] = []
    for node_type in present:
        fill, border = _style_for(node_type)
        lines.append(
            f"  classDef {_mermaid_class(node_type)} fill:{fill},stroke:{border},"
            "stroke-width:1px,color:#0f172a;"
        )
    return lines


def _mermaid_class(node_type: LineageNodeType) -> str:
    return f"t_{node_type}"


def render_mermaid(layout: DiagramLayout, *, max_nodes: int = _MERMAID_MAX_NODES) -> str:
    """Render the layout as a Mermaid ``flowchart LR`` block, fenced and ready to paste.

    What makes it readable rather than merely correct: nodes are grouped into the dependency stages
    the layout already computed, so the flow reads left to right in bands; each node carries its
    type colour from the same table the PNG uses; edge labels carry the hop kind *and* every update
    mode, so a pair with both a full and a delta DTP does not read as one of them; the root is
    emphasised; and advisory edges are dashed, as everywhere else.

    Bounded for legibility at ``max_nodes``, independently of the graph's own 400-node bound. When
    either bound binds, a note node says so inside the diagram - the same contract as the notice
    printed on the SVG canvas, because a reader who sees only the picture must be told the same
    thing as one who reads the payload.
    """
    kept = layout.nodes[:max_nodes]
    ids = {node.id: f"n{index}" for index, node in enumerate(kept)}

    present: list[LineageNodeType] = []
    for node in kept:
        if node.node_type not in present:
            present.append(node.node_type)

    grouped = _mermaid_grouped(kept)
    lines = ["```mermaid", "flowchart LR"]
    lines += _mermaid_class_defs(present)
    lines += _mermaid_nodes(kept, ids)

    for edge in layout.edges:
        src, dst = ids.get(edge.src), ids.get(edge.dst)
        if src is None or dst is None:
            continue  # an endpoint fell outside the legibility bound: no dangling arrow is drawn
        arrow = "-.->" if edge.confidence == "advisory" else "-->"
        label = _edge_label(edge) or edge.kind.replace("_", " ")
        lines.append(f'  {src} {arrow}|"{_mermaid_label(label, limit=40)}"| {dst}')

    lines += [f"  class {ids[node.id]} {_mermaid_class(node.node_type)};" for node in kept]
    root = next((node for node in kept if node.is_root), None)
    if root is not None:
        lines.append(f"  style {ids[root.id]} stroke-width:3px;")
    lines += _mermaid_bound_note(
        shown=len(kept), total=len(layout.nodes), cut=layout.truncated, grouped=grouped
    )
    lines.append("```")
    return "\n".join(lines)


def _mermaid_grouped(kept: list[LayoutNode]) -> bool:
    """Whether stage bands will help, or just add twenty labelled boxes around a hairball."""
    stages = {node.layer for node in kept}
    return len(stages) >= _MERMAID_MIN_STAGES and len(kept) <= _MERMAID_MAX_SUBGRAPH_NODES


def _mermaid_nodes(kept: list[LayoutNode], ids: dict[str, str]) -> list[str]:
    """Node declarations, grouped into dependency stages while grouping still aids reading."""

    def declare(node: LayoutNode, indent: str) -> str:
        type_label = _TYPE_LABEL.get(node.node_type, "object")
        return f'{indent}{ids[node.id]}["{_mermaid_label(node.label)}<br/>{type_label}"]'

    if not _mermaid_grouped(kept):
        return [declare(node, "  ") for node in kept]

    by_layer: dict[int, list[LayoutNode]] = {}
    for node in kept:
        by_layer.setdefault(node.layer, []).append(node)
    lines: list[str] = []
    for layer, members in sorted(by_layer.items()):
        lines.append(f'  subgraph stage{layer}["Stage {layer + 1}"]')
        lines.append("    direction TB")
        lines += [declare(node, "    ") for node in members]
        lines.append("  end")
    return lines


def _mermaid_bound_note(*, shown: int, total: int, cut: bool, grouped: bool) -> list[str]:
    """A note node stating any bound that shaped the picture, and what to do about it.

    The same contract as the notice printed on the SVG canvas: a reader who sees only the diagram is
    told what a reader of the payload is told. Each bound names its own remedy, because they differ:
    the legibility bound is answered by asking a narrower question, the walk's bound by a shallower
    one, and losing the stage bands by neither. Stating them together as one "truncated" would send
    a reader to the wrong fix.
    """
    notes: list[str] = []
    if total > shown:
        notes.append(f"showing {shown} of {total} objects - narrow the direction or depth")
    if cut:
        notes.append("graph truncated upstream - lower the depth for a complete reading")
    if not grouped and (total > shown or shown > _MERMAID_MAX_SUBGRAPH_NODES):
        notes.append("too wide for stage bands")
    if not notes:
        return []
    return [
        f'  bound["{_mermaid_label(". ".join(notes), limit=160)}"]',
        "  classDef bounded fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d;",
        "  class bound bounded;",
    ]
