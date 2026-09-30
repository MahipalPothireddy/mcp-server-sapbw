"""The CompositeProvider's declared model: its part providers and its field mapping.

**Why this is a module of its own.** A CompositeProvider is the one InfoProvider whose composition
BW keeps outside the relational dictionary. There is no part table and no field-mapping table: the
whole model lives as XML on the header row of ``RSOHCPR``. Everything a caller wants to know about a
CompositeProvider - which objects it unions or joins, and which of their fields becomes which of its
own - is in that one column or nowhere.

**Which column.** ``XML_DEF`` is the column the name suggests and the one this server used to probe.
Measured on a production system it is empty for *all* 108 active CompositeProviders, while
``XML_UI`` holds the model for all 108. The consequence was quiet and expensive: part providers were
being resolved instead from the base tables of the generated calc view and reported as a
naming-convention reading, and field-level lineage had no CompositeProvider hop at all, so every
field of every CompositeProvider-based query fell back to provider level. Both facts were declared
metadata the whole time.

**Two distinctions the models keep.** A mapping that carries a constant is not a mapping to a source
field, so ``source_field`` is ``None`` and ``mapping_kind`` says ``constant`` rather than the reader
inventing a field name. And a part whose kind BW labels with a code this release does not decode is
reported as ``unknown`` carrying that code, rather than being guessed from its name - a wrong part
type sends a change review to the wrong object.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .completeness import BoundedResult
from .evidence import Evidence, evidence_for
from .provenance import Provenance

#: What a part of a CompositeProvider is. Each value here was confirmed against the catalogue that
#: owns it on a live system (see ``services.composite_parser._ALIAS_KIND``); ``unknown`` is what a
#: code with no confirmed catalogue resolves to, and it is deliberately not a guess.
CompositePartKind = Literal[
    "adso",
    "infocube",
    "dso",
    "infoobject",
    "compositeprovider",
    "calcview",
    "unknown",
]

#: Nodes one field resolution will walk. A stacked model measured two nodes deep; this leaves room
#: and guarantees termination if a model ever references a node cyclically.
_MAX_RESOLVE_NODES = 32


class CompositeFieldMapping(BaseModel):
    """One field of a part provider becoming one element of the CompositeProvider.

    ``target_field`` is the CompositeProvider's own element and ``source_field`` the part's field.
    They are equal far more often than not, and that is exactly why the pair has to be reported
    rather than assumed: where they differ, the rename is the reason a field-level trace would
    otherwise dead-end.
    """

    model_config = ConfigDict(extra="forbid")

    target_field: str
    #: ``None`` for a constant mapping, which has no source field to follow.
    source_field: str | None = None
    mapping_kind: Literal["element", "constant", "unknown"] = "element"
    #: BW's own ``xsi:type`` on the mapping, kept verbatim so an unrecognised kind is still legible.
    raw_type: str | None = None


class CompositeViewNode(BaseModel):
    """One node of a CompositeProvider's model, and how it combines its inputs.

    The kind is not decoration: a union stacks its inputs' rows while a join widens them, so the
    same set of parts means two different things and a reader told only the parts cannot tell which.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    #: BW's own ``xsi:type`` (``View:Union``, ``View:JoinNode`` ...), kept verbatim.
    node_type: str | None = None


class CompositeInput(BaseModel):
    """One input of a CompositeProvider's view node: what it reads, and its field mapping.

    An input reads *either* an object outside the CompositeProvider (``part_name``) *or* another
    node of the same model (``internal_node``) - the stacked case, where a join sits over a union.
    Exactly one of the two is set. Conflating them reports a node name as a missing InfoProvider.
    """

    model_config = ConfigDict(extra="forbid")

    #: BW's alias for the input, e.g. ``U1.ADSO.1``. Its middle segment is BW's own kind code.
    alias: str
    #: The view node this input belongs to. Which node an input feeds is what makes a stacked model
    #: readable; flattened, a union of two objects looks identical to a join of them.
    node_name: str | None = None
    part_name: str
    part_kind: CompositePartKind = "unknown"
    #: The raw kind code out of the alias (``ADSO``, ``CALC``, ``FBPA`` ...). Present even when the
    #: kind did not decode, so a caller can see what BW said rather than only that we did not know.
    alias_kind: str | None = None
    #: The model's own reference to the part, verbatim (``<OBJECT>.composite#//``). The audit trail
    #: for how ``part_name`` was derived.
    entity_ref: str | None = None
    #: Set instead of ``part_name`` when this input reads another node of the same model. Such an
    #: input is an edge inside the CompositeProvider, not a part provider: measured, 40 of 226
    #: inputs on a production system are of this kind, and reading them as parts reported 40 objects
    #: that do not exist while a field walk that stopped at one lost every part beneath it.
    internal_node: str | None = None
    #: For a calc-view part: the ``_SYS_BIC`` runtime name, whose package separator is a dot where
    #: the model writes a slash. Joining on the model's form finds nothing.
    runtime_view_name: str | None = None
    select_all: bool = False
    mappings: list[CompositeFieldMapping] = Field(default_factory=list)
    #: Mappings this input declares, which exceeds ``len(mappings)`` when the bound was hit.
    mapping_count: int = 0
    evidence: Evidence | None = None

    @property
    def is_internal(self) -> bool:
        """Whether this input reads another node of the same model rather than an object."""
        return bool(self.internal_node) and not self.part_name


class CompositeFieldOrigin(BaseModel):
    """Where one element of a CompositeProvider comes from: an object, and its field there.

    The end of a resolution through the model, including through a stack of nodes. ``via_aliases``
    is the route taken, in order, so a multi-node hop is auditable rather than a bare conclusion.
    """

    model_config = ConfigDict(extra="forbid")

    target_field: str
    part_name: str
    part_kind: CompositePartKind = "unknown"
    #: The field *in the part* that supplies it. ``None`` for a constant mapping, which has no
    #: source field - the value is fixed in the model and there is nothing upstream to follow.
    source_field: str | None = None
    mapping_kind: Literal["element", "constant", "unknown"] = "element"
    runtime_view_name: str | None = None
    #: The input aliases traversed, outermost first. One entry for a direct part, more for a stack.
    via_aliases: list[str] = Field(default_factory=list)


class CompositeModel(BoundedResult):
    """The declared composition and field mapping of one CompositeProvider.

    ``parsed`` is the field to read first. ``False`` means the model could not be read and
    ``unparsed_reason`` says which case it is - no definition stored, above the size bound, or a
    shape this parser does not recognise. An unreadable model is never reported as a
    CompositeProvider without parts.

    Bounded rather than merely truncatable: ``completeness`` names *which* bound stopped the read -
    the input cap, the mapping cap, the element cap, or the definition size - because "the input
    list is a prefix" and "the element count is a prefix" send a caller to different places.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    parsed: bool = False
    unparsed_reason: str | None = None
    #: Which column the definition came from. Both are read; which one is populated varies by
    #: release, and saying so is the difference between a gap and a mystery.
    source_column: Literal["XML_UI", "XML_DEF"] | None = None
    #: The node a consumer reads, from the model's own ``defaultNode``, with its ``xsi:type``.
    node_type: str | None = None
    node_name: str | None = None
    #: Every node of the model. More than one means the provider is stacked.
    nodes: list[CompositeViewNode] = Field(default_factory=list)
    with_hana_model: bool = False
    schema_version: str | None = None
    #: Elements the CompositeProvider publishes. Counted even when the list is not carried.
    element_count: int = 0
    inputs: list[CompositeInput] = Field(default_factory=list)
    #: Inputs the model declares, which exceeds ``len(inputs)`` when the bound was hit.
    input_count: int = 0
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]

    @property
    def part_inputs(self) -> list[CompositeInput]:
        """Inputs that read an object, excluding the model's own internal node references."""
        return [candidate for candidate in self.inputs if not candidate.is_internal]

    def inputs_of(self, node_name: str | None) -> list[CompositeInput]:
        """Inputs belonging to one node. All of them when the model attributed none."""
        if node_name is None:
            return list(self.inputs)
        matched = [c for c in self.inputs if (c.node_name or "") == node_name]
        # A model whose inputs carry no node attribution still has to resolve, so fall back to the
        # whole set rather than reporting a provider with no inputs at all.
        return matched or [c for c in self.inputs if not c.node_name]

    def mapping_for(self, target_field: str) -> list[tuple[CompositeInput, CompositeFieldMapping]]:
        """Every input that supplies ``target_field``, with the mapping that does it.

        A list rather than one answer, because a union CompositeProvider is normally fed the same
        element by several parts. Returning one of them would name an arbitrary source for a figure
        that has several, which is the sort of confident-and-wrong answer this server exists to
        avoid. Case-insensitive: BW field names are upper-case, callers' are not always.
        """
        wanted = target_field.strip().upper()
        found: list[tuple[CompositeInput, CompositeFieldMapping]] = []
        for candidate in self.inputs:
            for mapping in candidate.mappings:
                if mapping.target_field.strip().upper() == wanted:
                    found.append((candidate, mapping))
        return found

    def resolve_field(self, target_field: str) -> list[CompositeFieldOrigin]:
        """Every object that supplies ``target_field``, following the model's own node stack.

        The difference from :meth:`mapping_for` is the stacked case. An input can read another node
        of the same model instead of an object, so a field of a join over a union is mapped to the
        union node first and only then to the objects beneath it. Stopping at the node reference -
        which is what treating inputs as a flat list does - loses every part below it, and on the
        measured system 40 of 226 inputs are such references.

        Deduplicated on (part, source field) so an object reached twice through a stack is reported
        once, and bounded by ``_MAX_RESOLVE_NODES`` so a model that references itself terminates.
        """
        origins: list[CompositeFieldOrigin] = []
        seen: set[tuple[str, str]] = set()
        # (node to search, field name in that node's output, aliases traversed to get here)
        pending: list[tuple[str | None, str, list[str]]] = [
            (self.node_name, target_field.strip(), [])
        ]
        visited_nodes = 0
        while pending and visited_nodes < _MAX_RESOLVE_NODES:
            node, field, route = pending.pop(0)
            visited_nodes += 1
            wanted = field.upper()
            for candidate in self.inputs_of(node):
                for mapping in candidate.mappings:
                    if mapping.target_field.strip().upper() != wanted:
                        continue
                    route_here = [*route, candidate.alias]
                    if candidate.is_internal:
                        # Follow the source field into the referenced node; the field is renamed at
                        # every hop, so carrying the original name would find nothing below.
                        if mapping.source_field:
                            pending.append(
                                (candidate.internal_node, mapping.source_field, route_here)
                            )
                        continue
                    key = (candidate.part_name, (mapping.source_field or "").upper())
                    if not candidate.part_name or key in seen:
                        continue
                    seen.add(key)
                    origins.append(
                        CompositeFieldOrigin(
                            # The *declared* spelling, not the caller's. Matching is
                            # case-insensitive, so echoing the argument would make the same
                            # resolution read differently depending on how it was asked for.
                            target_field=mapping.target_field,
                            part_name=candidate.part_name,
                            part_kind=candidate.part_kind,
                            source_field=mapping.source_field,
                            mapping_kind=mapping.mapping_kind,
                            runtime_view_name=candidate.runtime_view_name,
                            via_aliases=route_here,
                        )
                    )
        return origins


def composite_mapping_evidence(part_kind: CompositePartKind) -> Evidence:
    """Evidence for a hop taken through the declared CompositeProvider model.

    Exact, not advisory: this is BW's own stored model, read whole, not a convention applied to a
    generated table name. The distinction matters because the route it replaces *was* advisory, and
    a caller deciding whether to act on a mapping needs to know which of the two produced it.
    """
    return evidence_for(
        "composite_field_mapping",
        "declared",
        detail=(
            "Read from the CompositeProvider's own stored model on RSOHCPR, which declares this "
            f"element as coming from the named {part_kind} part's field. Not derived from a table "
            "name."
        ),
    )


def multiprovider_identification_evidence(*, renamed: bool) -> Evidence:
    """Evidence for a hop taken through a MultiProvider's InfoObject identification.

    Exact rather than advisory, and for the same reason the CompositeProvider hop is: this is BW's
    own stored identification on ``RSDICMULTIIOBJ``, read row by row, not a naming convention
    applied to the provider's field. ``renamed`` is surfaced because it is the case a same-name
    assumption would have got wrong, so a reader can see that the mapping was actually consulted
    rather than guessed - measured at 66 of 5,372 active rows on the reference system.
    """
    return evidence_for(
        "multiprovider_identification",
        "declared",
        detail=(
            "Read from the MultiProvider's own InfoObject identification on RSDICMULTIIOBJ, which "
            "names the part providing this field and the field's name inside that part."
            + (
                " Identification maps it to a DIFFERENTLY named field in the part, so following the"
                " provider's own field name into the part would have been wrong here."
                if renamed
                else " Identification maps it to the same field name in the part."
            )
        ),
    )


def reference_characteristic_evidence() -> Evidence:
    """Evidence for a hop that crosses from a reference characteristic to the one it references.

    Exact rather than advisory, for the same reason the two hops above are: ``RSDCHA.CHABASNM``
    states the reference outright. Nothing here is derived from a table name - which matters,
    because the *constructed* route to the same fact is the one that gets it wrong (a reference
    characteristic's master-data table does not carry its own name), and that was the whole of D35.
    """
    return evidence_for(
        "reference_characteristic",
        "declared",
        detail=(
            "RSDCHA.CHABASNM names the characteristic this one references. A reference "
            "characteristic owns no master data: its attribute, SID, text and view tables belong "
            "to the referenced characteristic, and no transformation loads into it, so the field "
            "populated there."
        ),
    )
