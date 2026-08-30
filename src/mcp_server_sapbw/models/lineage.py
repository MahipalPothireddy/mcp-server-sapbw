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

# Node object types. A subset of the canonical BwObjectType vocabulary, not a parallel list.
#
# **Invariant, enforced by test:** every type ``models.objects.TLOGO_TO_TYPE`` can decode to must
# appear here. The lineage walk types a node by passing a raw TLOGO code through
# ``normalise_object_type`` and casting the result to this Literal, so a decodable type missing from
# this list is not a type error - the cast hides it from mypy - but a Pydantic failure at runtime
# that rejects the **entire** graph rather than one node.
#
# That is not hypothetical. ``ELEM`` (a query element used as a transformation endpoint) was
# decodable and absent, so ``bw_get_lineage`` returned no graph at all the moment a walk reached one
# on a real system - and four more codes (``ISTS``, ``ISIP``, ``UPDR``, ``RSPC``) were one landscape
# away from doing the same. ``chain`` is included for completeness of the decode table rather than
# because a chain is expected as a data-flow endpoint; representing it costs nothing, and crashing
# on it costs the caller their answer.
#
# BREAKING (pre-1.0): a basic InfoCube is now ``infocube``, matching bw_describe_object and every
# other surface. It was ``cube`` here alone, so correlating a lineage node with a described object
# required knowing the two words meant one thing. ``virtualprovider`` is added for the same reason -
# it previously arrived as a plain cube, silently discarding the distinction. Legacy ``cube`` still
# normalises through models.objects.TYPE_ALIASES.
LineageNodeType = Literal[
    "datasource",
    "infosource",
    "transfer_structure",  # ISTS - the BW 3.x hop between DataSource and InfoSource
    "dso",
    "adso",
    "infocube",
    "multiprovider",
    "virtualprovider",
    "compositeprovider",
    "infoobject",
    "transformation",
    "dtp",
    "infopackage",  # ISIP - moves data from a DataSource into the PSA
    "update_rule",  # UPDR - the BW 3.x equivalent of a transformation
    "chain",  # RSPC - orchestration; here so a decodable code cannot fail the graph
    "calcview",
    "query",
    "query_element",  # ELEM - a query element used as a transformation endpoint
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

# Why a graph is - or is not - a complete reading of the metadata.
#
# ``truncated`` said only *that* expansion stopped, and it was set in exactly one place (the node
# cap), so a walk that stopped for any other reason reported ``truncated=False`` and read as a
# complete answer. A bounded discovery that looks complete is worse than one that admits its bound:
# the caller cannot tell "this object has two consumers" from "we stopped after two".
#
# ``semantic_limit``     a stated cap on resolved objects stopped discovery
# ``query_budget``       the per-call statement allowance would not cover another read
# ``time_budget``        the per-call wall-clock allowance would not cover another read
# ``node_limit``         the graph-wide node cap stopped expansion (the historical bool)
# ``error_degraded``     a branch failed and the graph is what survived
# ``unsupported_branch`` this release lacks the metadata one branch needs
LineageCompleteness = Literal[
    "complete",
    "semantic_limit",
    "query_budget",
    "time_budget",
    "node_limit",
    "error_degraded",
    "unsupported_branch",
]


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
    #: Every distinct active update mode BW has between this pair, from RSBKDTP. A pair commonly
    #: carries more than one - a repair/init full DTP alongside the regular delta - and on a
    #: measured production system 208 of 1043 active pairs (20%) did. Reporting one of them was a
    #: defect rather than a simplification: which one won depended on the order the database
    #: returned rows, so the same edge read 'full' or 'delta' on different runs of the same call.
    update_modes: list[UpdateMode] = Field(default_factory=list)
    #: The most operationally significant mode among ``update_modes`` (full > init > delta), kept
    #: as a scalar for readers that want one value. A full load is what makes a re-run destructive
    #: and what creates the stale-lookup hazard, so it is the one worth surfacing first. Read
    #: ``update_modes`` when the distinction matters.
    update_mode: UpdateMode | None = None
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
    #: Why the graph stopped, where ``truncated`` only said *that* it did. Kept beside the bool
    #: rather than replacing it so existing clients keep working; the two are reconciled below and
    #: therefore cannot disagree.
    completeness: LineageCompleteness = "complete"
    evidence_summary: EvidenceSummary | None = None
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _summarise_evidence(self) -> LineageGraph:
        if self.evidence_summary is None and self.edges:
            self.evidence_summary = summarise([e.evidence for e in self.edges if e.evidence])
        return self

    @model_validator(mode="after")
    def _reconcile_completeness(self) -> LineageGraph:
        """Neither field may claim completeness the other denies.

        A caller that only reads ``truncated`` must still be told the graph is bounded, and a caller
        that only reads ``completeness`` must not see ``complete`` on a graph built by older code
        that set the bool alone.
        """
        if self.completeness != "complete":
            self.truncated = True
        elif self.truncated:
            self.completeness = "node_limit"
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
