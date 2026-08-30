"""Result model for rendered data-flow diagrams (image rendering slice).

The image itself travels as MCP image content so a client can display it inline; this model is the
accompanying structured payload — what was drawn, how complete it is, and where the vector copy was
written. Keeping the counts and caveats machine-readable means a model consuming the tool can reason
about completeness instead of inferring it from a picture.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .completeness import BoundedResult

#: ``mermaid`` is text rather than an image, and is here because it answers the same question in a
#: medium a picture cannot reach: a markdown file, a wiki page, a chat reply. It is rendered from
#: the same layout as the other two, so the three cannot disagree about the graph.
DiagramFormat = Literal["png", "svg", "mermaid"]


class DiagramResult(BoundedResult):
    """Metadata describing a rendered lineage diagram."""

    root: str
    direction: str
    depth: int
    image_format: DiagramFormat
    node_count: int = 0
    edge_count: int = 0
    layer_count: int = 0
    advisory_edge_count: int = 0  # dashed edges: routine-derived or convention-resolved
    width: int = 0
    height: int = 0
    svg_path: str | None = None  # vector copy on disk, when an output directory was given
    png_available: bool = True  # False when the optional viz extra is not installed
    caveats: list[str] = Field(default_factory=list)
