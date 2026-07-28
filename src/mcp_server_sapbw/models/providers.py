"""Domain models for InfoProviders and InfoObjects (B4).

A single :class:`Provider` model spans every object-model variant discovered live on the connected
release: classic DSO (RSDODSO), advanced DSO (RSOADSO), InfoCube / MultiProvider / virtual provider
(RSDCUBE, discriminated by CUBETYPE), CompositeProvider (RSOHCPR), and InfoObject (RSDIOBJ). The
``object_type`` discriminator lets one tool (bw_describe_object) handle all of them. Every fact
carries provenance (mission Rule 3), and descriptions are labelled stored-vs-generated (Rule 7).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .description import Description
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
    provenance: Provenance


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
    provenance: Provenance


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
    subtype: str | None = None  # ODSOTYPE / CUBESUBTYPE / IOBJTP raw code, when meaningful
    infoobject_kind: InfoObjectKind | None = None  # only for object_type == 'infoobject'
    description: Description | None = None
    active: bool = True
    info_area: str | None = None
    owner: str | None = None
    application: str | None = None
    key_field_names: list[str] = Field(default_factory=list)  # semantic key (DSO/ADSO)
    fields: list[ProviderField] = Field(default_factory=list)
    part_providers: list[PartProviderRef] = Field(default_factory=list)
    composition_source: Literal["relational", "xml", "calc_view", "none"] = "none"
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class ProviderSummary(BaseModel):
    """Compact provider entry for list/search results."""

    model_config = ConfigDict(extra="forbid")

    name: str
    object_type: ProviderType
    description_short: str | None = None
    active: bool = True
    info_area: str | None = None
    provenance: Provenance | list[Provenance]


class SearchHit(BaseModel):
    """One cross-domain search result (provider, InfoObject, or chain)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    object_type: SearchObjectType
    description_short: str | None = None
    matched_on: Literal["name", "description"]
    provenance: Provenance | list[Provenance]


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
