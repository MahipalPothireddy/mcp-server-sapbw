"""Domain models for InfoProviders and InfoObjects (B4).

A single :class:`Provider` model spans every object-model variant discovered live on the connected
release: classic DSO (RSDODSO), advanced DSO (RSOADSO), InfoCube / MultiProvider / virtual provider
(RSDCUBE, discriminated by CUBETYPE), CompositeProvider (RSOHCPR), and InfoObject (RSDIOBJ). The
``object_type`` discriminator lets one tool (bw_describe_object) handle all of them. Every fact
carries provenance (mission Rule 3), and descriptions are labelled stored-vs-generated (Rule 7).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .aggregation import KeyFigureAggregation
from .description import Description
from .evidence import Evidence, evidence_for
from .objects import BwObjectRef, normalise_object_type
from .provenance import Provenance

# Unified provider/object type. Cube variants are split by RSDCUBE.CUBETYPE (B/M/V).
ProviderType = Literal[
    "dso",  # classic DataStore Object (RSDODSO)
    "adso",  # advanced DSO (RSOADSO)
    "infocube",  # basic InfoCube (RSDCUBE CUBETYPE='B')
    "multiprovider",  # MultiProvider (RSDCUBE CUBETYPE='M')
    "virtualprovider",  # virtual provider (RSDCUBE CUBETYPE='V')
    "compositeprovider",  # HANA CompositeProvider (RSOHCPR)
    "infoobject",  # InfoObject (RSDIOBJ)
]

# Object type for cross-domain search results (providers + chains).
SearchObjectType = Literal[
    "chain",
    "dso",
    "adso",
    "infocube",
    "multiprovider",
    "virtualprovider",
    "compositeprovider",
    "infoobject",
]

# Role of a field within a provider (or of an InfoObject's related object).
FieldRole = Literal[
    "characteristic",
    "key_figure",
    "unit",
    "time",
    "attribute",  # display/nav attribute of an InfoObject
    "navigation_attribute",
    "field",  # a field-based (non-InfoObject) column, e.g. an ADSO physical field
    "unknown",
]

# InfoObject kind decoded from RSDIOBJ.IOBJTP (CHA/KYF/UNI/TIM/DPA/XXL).
InfoObjectKind = Literal[
    "characteristic",
    "key_figure",
    "unit",
    "time",
    "data_packet",
    "other",
]


# RSDCUBE.CUBETYPE -> provider type (verified live, B4). Anything else is treated as a basic cube.
_CUBETYPE_TO_PROVIDER: dict[str, ProviderType] = {
    "B": "infocube",
    "M": "multiprovider",
    "V": "virtualprovider",
}


def classify_cube_type(cubetype: object) -> ProviderType:
    """Map an ``RSDCUBE.CUBETYPE`` code to a provider type, defaulting to ``infocube``."""
    return _CUBETYPE_TO_PROVIDER.get(str(cubetype).strip().upper(), "infocube")


class ProviderField(BaseModel):
    """One field/InfoObject of a provider, or one attribute of an InfoObject."""

    model_config = ConfigDict(extra="forbid")

    name: str  # IOBJNM, or a physical column name (ADSO/CompositeProvider)
    position: int | None = None
    is_key: bool = False  # part of the semantic key (DSO/ADSO KEYFLAG)
    role: FieldRole = "unknown"
    description: str | None = None  # field-level text where the source provides one
    # When the field is a navigation attribute, the characteristic it hangs off and the attribute
    # itself, resolved from RSDATRNAV. Without this a provider field list shows names like
    # ``0CUSTOMER__0COUNTRY`` with nothing saying where the value comes from - and on the reference
    # system 2,756 InfoCube field rows are exactly that.
    attribute_of: str | None = None
    attribute_name: str | None = None
    provenance: Provenance


class AttributeRef(BaseModel):
    """One attribute of a characteristic InfoObject.

    Two tables, keyed differently, and the difference is load-bearing. ``RSDBCHATR`` is keyed on the
    *basic* characteristic, so a reference characteristic inherits its base's attribute list;
    ``RSDATRNAV`` is keyed on the characteristic itself, so the same inherited attribute carries a
    navigation name of its own. Reading both from one name loses one side silently - measured on the
    reference system, 26% of characteristics are reference characteristics and 724 of 4,129
    navigation attributes are reachable only through the reference name.

    ``kind`` and ``navigable`` answer two different questions and can disagree. ``kind`` is the
    attribute type as defined on the basic characteristic, which is the only place BW stores it;
    ``navigable`` says whether *this* characteristic exposes the attribute for drilldown, which is
    true only when it has a navigation name of its own. A reference characteristic can therefore
    inherit an attribute the base defines as navigable and still not expose it - measured on the
    reference system, one such characteristic inherits 62 navigable attributes and exposes 55.
    Reading ``kind`` alone would claim a drilldown that does not exist.
    """

    model_config = ConfigDict(extra="forbid")

    name: str  # ATTRINM - the attribute's own InfoObject
    description: str | None = None
    kind: Literal["display", "navigation"]
    position: int | None = None
    # RSDBCHATR.ATRTIMFL. Domain RSDCNVFL: '1' is true, '0'/blank false - not the usual 'X'.
    time_dependent: bool = False
    # RSDBCHATR.NODISPINQUERYFL - present in the provider but hidden from query display.
    hidden_in_query: bool = False
    # The navigation attribute's technical name as stored (RSDATRNAV.ATRNAVNM), which is what a
    # provider field list and a query reference. Read rather than composed: it matches
    # <CHANM>__<ATTRINM> for 4,127 of 4,129 rows on the reference system, so composing it would be
    # wrong twice.
    navigation_name: str | None = None
    # Whether a query on *this* characteristic can drill down by the attribute. Stored rather than
    # left to be derived, because the derivation ("navigation_name is not None") is not obvious and
    # getting it wrong claims a drilldown the system does not offer.
    navigable: bool = False
    # RSDATRNAV.AUTHRELFL. A navigation attribute carries its own authorisation relevance, which can
    # differ from that of the characteristic behind it - so a report can be row-restricted on an
    # attribute nothing in the characteristic list would suggest.
    auth_relevant: bool = False
    text_from_characteristic: bool = False  # RSDATRNAV.TXTFROMCHAFL
    transitive: bool = False  # RSDATRNAV.TRANSITIVEFL - attribute of an attribute
    # Set when the attribute list came from a different (basic) characteristic than the one asked
    # about, so an inherited attribute never looks locally defined.
    inherited_from: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)


class PartProviderRef(BaseModel):
    """A part provider of a MultiProvider (RSDCUBEMULTI) or a CompositeProvider.

    For a CompositeProvider resolved through its generated calc view, ``via_table`` names the
    physical table the dependency graph reported and ``confidence`` says whether the table -> object
    reading was confirmed against the provider catalogue or is a naming-convention guess.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    part_type: ProviderType | None = None  # resolved type if known; None until resolved
    position: int | None = None
    via_table: str | None = None  # generated table the part was resolved from (calc-view route)
    confidence: Literal["confirmed", "advisory"] = "confirmed"
    evidence: Evidence | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _derive_evidence(self) -> PartProviderRef:
        if self.evidence is None:
            detail = None
            if self.via_table:
                detail = (
                    f"Resolved from generated table {self.via_table}, reached through the "
                    "CompositeProvider's calc-view dependencies."
                    + (
                        " Confirmed against the provider catalogue."
                        if self.confidence == "confirmed"
                        else " Not confirmed against the provider catalogue, so the table -> "
                        "object reading rests on the naming convention alone."
                    )
                )
            self.evidence = evidence_for("part_provider", self.confidence, detail=detail)
        return self


class Provider(BaseModel):
    """Universal deep-dive for any InfoProvider or InfoObject.

    ``composition_source`` records how ``part_providers`` was derived: ``relational``
    (RSDCUBEMULTI), ``xml`` (parsed from RSOHCPR.XML_DEF), ``calc_view`` (resolved from the
    dependencies of the HANA calc view BW generates for the CompositeProvider), or ``none``. When a
    composition could not be derived at all, ``part_providers`` is empty, ``composition_source`` is
    ``none``, and a caveat says so (never silently implying a CompositeProvider has no parts).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    object_type: ProviderType
    #: Canonical reference. ``ref.id`` (``infocube:SALES_CUBE``) is the key to join this result
    #: against a lineage node, a search hit or a docs page - one spelling, type-qualified.
    ref: BwObjectRef | None = None
    subtype: str | None = None  # ODSOTYPE / CUBESUBTYPE / IOBJTP raw code, when meaningful
    infoobject_kind: InfoObjectKind | None = None  # only for object_type == 'infoobject'
    description: Description | None = None
    active: bool = True
    info_area: str | None = None
    owner: str | None = None
    application: str | None = None
    key_field_names: list[str] = Field(default_factory=list)  # semantic key (DSO/ADSO)
    fields: list[ProviderField] = Field(default_factory=list)
    # Only for a characteristic InfoObject: its display and navigation attributes. Empty for every
    # other object type, and for a characteristic whose attribute tables could not be read - in
    # which case a caveat says so rather than implying it has none.
    attributes: list[AttributeRef] = Field(default_factory=list)
    part_providers: list[PartProviderRef] = Field(default_factory=list)
    composition_source: Literal["relational", "xml", "calc_view", "none"] = "none"
    # Only for a key-figure InfoObject: how its number combines and in what unit. Absent for every
    # other object type, and for a key figure whose RSDKYF row could not be read.
    aggregation: KeyFigureAggregation | None = None
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_ref(self) -> Provider:
        if self.ref is None:
            self.ref = BwObjectRef(
                object_type=normalise_object_type(self.object_type),
                name=self.name,
                subtype=self.subtype,
            )
        return self


class ProviderSummary(BaseModel):
    """Compact provider entry for list/search results."""

    model_config = ConfigDict(extra="forbid")

    name: str
    object_type: ProviderType
    ref: BwObjectRef | None = None
    description_short: str | None = None
    active: bool = True
    info_area: str | None = None
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_ref(self) -> ProviderSummary:
        if self.ref is None:
            self.ref = BwObjectRef(
                object_type=normalise_object_type(self.object_type), name=self.name
            )
        return self


class SearchHit(BaseModel):
    """One cross-domain search result (provider, InfoObject, or chain)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    object_type: SearchObjectType
    ref: BwObjectRef | None = None
    description_short: str | None = None
    matched_on: Literal["name", "description"]
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_ref(self) -> SearchHit:
        if self.ref is None:
            self.ref = BwObjectRef(
                object_type=normalise_object_type(self.object_type), name=self.name
            )
        return self


class ObjectNotFound(BaseModel):
    """Structured 'no such object' result (distinct from UnsupportedResult, which is a release gap).

    Returned when the requested object's tables exist on this release but no active-version row
    matches the name in any searched provider type.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_found"] = "not_found"
    name: str
    searched_types: list[str] = Field(default_factory=list)
    detail: str
