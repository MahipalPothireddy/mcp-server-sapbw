"""Lineage graph models (B6).

A directed data-flow graph over BW objects. Nodes are objects (providers, DataSources, routines'
unresolved calls, and — later — source-system objects); edges are ``src -> dst`` in the direction
data flows. Declared edges (transformations, DTPs, MultiProvider parts) are exact; routine-derived
edges are advisory (the routine parser is a lower bound, mission Known Limitation 3).

The DataSource is an explicit **boundary node**: in the BW-only build it carries
``upstream_resolved=False`` (there is an upstream source system, simply not resolved here) and an
empty ``source_system``. An ECC connector / source bundle later attaches ``source_object`` parents
via ``source_extract`` edges and flips the flag — reusing these types only (an "extension point").
Aligns with the design.md model sketch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance
from .transformations import UnresolvedRef

# Node object types (RSTLOGO-derived for BW objects, plus boundary/extension types).
LineageNodeType = Literal[
    "datasource",
    "infosource",
    "dso",
    "adso",
    "cube",
    "multiprovider",
    "compositeprovider",
    "infoobject",
    "transformation",
    "dtp",
    "calcview",
    "query",
    "report",
    "source_object",  # a node in a source system (ECC), attached by a connector/bundle
    "unresolved_dependency",  # a custom class/FM/method the parser could not resolve
    "unknown",
]

LineageEdgeKind = Literal[
    "transformation",
    "dtp",
    "multiprovider_part",
    "composite_part",
    "calcview_base",
    "query_provider",
    "routine_lookup",
    "source_extract",
    "unresolved_call",
]

LineageDirection = Literal["upstream", "downstream", "both"]
UpdateMode = Literal["full", "delta", "init"]


class SourceSystemRef(BaseModel):
    """Slot on a DataSource boundary node for source-system detail (identifiers only)."""

    model_config = ConfigDict(extra="forbid")

    system_type: Literal["ecc", "other", "unknown"] = "unknown"
    system_id: str | None = None  # logical system / SID (RSDS.LOGSYS)
    object_name: str | None = None  # extract structure / source object, filled by a connector


class LineageNode(BaseModel):
    """One object in the lineage graph (id is the canonical ``type:name``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    object_type: LineageNodeType
    name: str
    upstream_resolved: bool = True  # False on a DataSource with no resolved source-system parents
    source_system: SourceSystemRef | None = None
    unresolved_ref: UnresolvedRef | None = None
    provenance: Provenance | list[Provenance]


class LineageEdge(BaseModel):
    """A directed data-flow edge ``src -> dst`` (``confidence='advisory'`` for routine edges)."""

    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    kind: LineageEdgeKind
    derivation: Literal["declared", "routine"] = "declared"
    confidence: Literal["exact", "advisory"] = "exact"
    transformation_id: str | None = None
    update_mode: UpdateMode | None = None  # from RSBKDTP for dtp edges
    chain_id: str | None = None
    chain_frequency: str | None = None
    note: str | None = None
    provenance: Provenance | list[Provenance]


class LineageGraph(BaseModel):
    """A resolved lineage graph around a root object."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    direction: LineageDirection
    depth: int
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    truncated: bool = False  # a depth or node cap stopped expansion
    caveats: list[str] = Field(default_factory=list)


class ImpactAnalysis(BaseModel):
    """Downstream blast radius of a change to an object, including routine-embedded consumers.

    ``routine_lookup_consumers`` are objects whose transformation *routines* read the root object —
    dependencies invisible to BW's own where-used lists. They are advisory (heuristic lower bound).
    """

    model_config = ConfigDict(extra="forbid")

    root_id: str
    graph: LineageGraph
    affected_object_count: int = 0
    affected_by_type: dict[str, int] = Field(default_factory=dict)
    routine_lookup_consumers: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class TraceToSource(BaseModel):
    """Upstream trace of an object back to the DataSource boundary, hop by hop."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    graph: LineageGraph
    datasources_reached: list[str] = Field(default_factory=list)
    unresolved_boundaries: list[str] = Field(
        default_factory=list
    )  # datasources upstream_resolved=False
    caveats: list[str] = Field(default_factory=list)
