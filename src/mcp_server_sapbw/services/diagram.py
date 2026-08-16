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
_NODE_STYLE: dict[LineageNodeType, tuple[str, str]] = {
    "datasource": ("#fde68a", "#b45309"),  # amber - the source-system boundary
    "infosource": ("#fed7aa", "#c2410c"),
    "dso": ("#bfdbfe", "#1d4ed8"),  # blue - persisted staging/EDW
    "adso": ("#c7d2fe", "#4338ca"),
    "infocube": ("#ddd6fe", "#6d28d9"),  # violet - aggregated
    "virtualprovider": ("#ede9fe", "#5b21b6"),
    "multiprovider": ("#e9d5ff", "#7e22ce"),
    "compositeprovider": ("#bbf7d0", "#15803d"),  # green - virtual consumption layer
    "infoobject": ("#fecdd3", "#be123c"),  # rose - master data
    "query": ("#a5f3fc", "#0e7490"),  # cyan - reporting
    "calcview": ("#d9f99d", "#4d7c0f"),  # lime - HANA
    "unknown": ("#e5e7eb", "#6b7280"),
}
_DEFAULT_STYLE = ("#e5e7eb", "#6b7280")

# Human labels for the legend (only types actually present are drawn).
_TYPE_LABEL: dict[LineageNodeType, str] = {
    "datasource": "DataSource",
    "infosource": "InfoSource",
    "dso": "DSO",
    "adso": "Advanced DSO",
    "infocube": "InfoCube",
    "virtualprovider": "VirtualProvider",
    "multiprovider": "MultiProvider",
    "compositeprovider": "CompositeProvider",
    "infoobject": "InfoObject",
    "query": "Query",
    "calcview": "Calc view",
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
    if edge.update_mode:
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
    for edge in layout.edges:
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None:
            continue
        advisory = edge.confidence == "advisory"
        stroke = "#94a3b8" if advisory else "#475569"
        dash = ' stroke-dasharray="6 4"' if advisory else ""
        marker = "b" if advisory else "a"
        if src.id == dst.id:  # self-loop: the object derives from itself
            cx, cy = src.x + _NODE_W, src.y + _NODE_H / 2
            path = f"M{cx},{cy - 10} C{cx + 44},{cy - 34} {cx + 44},{cy + 34} {cx},{cy + 10}"
            out.append(
                f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="1.6"{dash} '
                f'marker-end="url(#{marker})"/>'
            )
            out.append(
                f'<text x="{cx + 48}" y="{cy + 4}" font-size="10" fill="#b91c1c">self</text>'
            )
            continue
        x1, y1 = src.x + _NODE_W, src.y + _NODE_H / 2
        x2, y2 = dst.x, dst.y + _NODE_H / 2
        if x2 < x1:  # backward edge (cycle): route below to stay readable
            x1, y1 = src.x, src.y + _NODE_H / 2
            x2, y2 = dst.x + _NODE_W, dst.y + _NODE_H / 2
        mid = (x1 + x2) / 2
        path = f"M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}"
        out.append(
            f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="1.6"{dash} '
            f'marker-end="url(#{marker})"/>'
        )
        label = _edge_label(edge)
        spot = _label_spot(layout, x1, y1, x2, y2) if label else None
        if label and spot is not None:
            out.append(
                f'<text x="{spot[0]}" y="{spot[1] - 4}" font-size="9.5" fill="#64748b" '
                f'text-anchor="middle">{html.escape(label)}</text>'
            )

    for node in layout.nodes:
        fill, border = _style_for(node.node_type)
        width = 2.4 if node.is_root else 1.3
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

    for edge in layout.edges:
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None or src.id == dst.id:
            continue
        advisory = edge.confidence == "advisory"
        colour = "#94a3b8" if advisory else "#475569"
        x1, y1 = (src.x + _NODE_W) * s, (src.y + _NODE_H / 2) * s
        x2, y2 = dst.x * s, (dst.y + _NODE_H / 2) * s
        _draw_connector(draw, (x1, y1), (x2, y2), colour, s, dashed=advisory)
        label = _edge_label(edge)
        spot = (
            _label_spot(layout, x1 / s, y1 / s, x2 / s, y2 / s) if label else None
        )  # collision test in layout units
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
    start: tuple[float, float],
    end: tuple[float, float],
    colour: str,
    scale: int,
    *,
    dashed: bool,
) -> None:
    """Draw a left-to-right connector as a flattened cubic curve, dashed when advisory."""
    x1, y1 = start
    x2, y2 = end
    mid = (x1 + x2) / 2
    steps = 24
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        inv = 1 - t
        # Cubic Bezier with horizontal control points (matches the SVG path shape).
        x = inv**3 * x1 + 3 * inv**2 * t * mid + 3 * inv * t**2 * mid + t**3 * x2
        y = inv**3 * y1 + 3 * inv**2 * t * y1 + 3 * inv * t**2 * y2 + t**3 * y2
        points.append((x, y))
    for index in range(steps):
        if dashed and index % 2:
            continue  # gap
        draw.line([points[index], points[index + 1]], fill=colour, width=max(1, 2 * scale) - 1)
    # Arrow head at the destination.
    head = 5 * scale
    draw.polygon(
        [(x2, y2), (x2 - head * 1.6, y2 - head * 0.75), (x2 - head * 1.6, y2 + head * 0.75)],
        fill=colour,
    )
