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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import Evidence, EvidenceSummary, evidence_for, summarise
from .objects import BwObjectRef, normalise_object_type
from .provenance import Provenance
from .transformations import UnresolvedRef

# Node object types (RSTLOGO-derived for BW objects, plus boundary/extension types).
# Node object types. A subset of the canonical BwObjectType vocabulary, not a parallel list.
#
# BREAKING (pre-1.0): a basic InfoCube is now ``infocube``, matching bw_describe_object and every
# other surface. It was ``cube`` here alone, so correlating a lineage node with a described object
# required knowing the two words meant one thing. ``virtualprovider`` is added for the same reason -
# it previously arrived as a plain cube, silently discarding the distinction. Legacy ``cube`` still
# normalises through models.objects.TYPE_ALIASES.
LineageNodeType = Literal[
    "datasource",
    "infosource",
    "dso",
    "adso",
    "infocube",
    "multiprovider",
    "virtualprovider",
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
    #: The canonical reference, whose own ``id`` is type-qualified (``infocube:SALES_CUBE``). ``id``
    #: above stays the graph's internal key - the edges reference it and changing it would break
    #: every stored graph - so ``ref.id`` is the value to join on across tools.
    ref: BwObjectRef | None = None
    upstream_resolved: bool = True  # False on a DataSource with no resolved source-system parents
    source_system: SourceSystemRef | None = None
    unresolved_ref: UnresolvedRef | None = None
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_ref(self) -> LineageNode:
        if self.ref is None:
            self.ref = BwObjectRef(
                object_type=normalise_object_type(self.object_type), name=self.name
            )
        return self


class LineageEdge(BaseModel):
    """A directed data-flow edge ``src -> dst`` (``confidence='advisory'`` for routine edges).

    ``evidence`` says why this edge was concluded to exist, in the vocabulary every subsystem
    shares. It is derived from ``confidence`` automatically, so the two can never disagree, and it
    names the mechanism per edge: a declared edge is a row in ``RSTRAN``/``RSBKDTP``, a routine edge
    is a SELECT parsed out of a named transformation's ABAP. That difference decides whether the
    edge can be acted on or has to be checked first, and it was previously only inferable from a
    one-word field.
    """

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
    evidence: Evidence | None = None
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_evidence(self) -> LineageEdge:
        """Fill ``evidence`` from ``confidence`` unless a caller supplied a richer one."""
        if self.evidence is None:
            self.evidence = evidence_for(
                "lineage_edge", self.confidence, detail=self._edge_detail()
            )
        return self

    def _edge_detail(self) -> str | None:
        """The reason *this* edge exists, where it is more specific than its class's reason."""
        if self.derivation == "routine" and self.transformation_id:
            return (
                f"A SELECT in transformation {self.transformation_id}'s routine reads {self.src}. "
                "BW's own where-used lists do not contain this edge; dynamic SQL and "
                "function-module calls are not followed, so it is a lower bound."
            )
        if self.derivation == "declared" and self.transformation_id:
            return (
                f"Transformation {self.transformation_id} declares {self.src} as its source and "
                f"{self.dst} as its target."
            )
        return None


class LineageGraph(BaseModel):
    """A resolved lineage graph around a root object.

    ``evidence_summary`` counts the edges by basis. A graph of 200 edges is a different object
    depending on whether 2 or 150 of them rest on a routine parse, and a count says so without the
    caller walking every edge.
    """

    model_config = ConfigDict(extra="forbid")

    root_id: str
    direction: LineageDirection
    depth: int
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    truncated: bool = False  # a depth or node cap stopped expansion
    evidence_summary: EvidenceSummary | None = None
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _summarise_evidence(self) -> LineageGraph:
        if self.evidence_summary is None and self.edges:
            self.evidence_summary = summarise([e.evidence for e in self.edges if e.evidence])
        return self


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
