"""One object-type vocabulary and one object identity, shared by every subsystem.

**The problem this solves.** Each subsystem grew its own list of object types, and they drifted:

* ``ProviderType``, ``SearchObjectType`` and ``EndpointKind`` call a basic InfoCube ``infocube``;
  ``LineageNodeType`` called the same object ``cube``. A caller correlating a
  ``bw_describe_object`` result against ``bw_get_lineage`` nodes had to know they meant the same
  thing, and nothing said so.
* ``LineageNodeType`` had no ``virtualprovider`` at all, so a virtual provider arrived as a plain
  cube and the distinction the provider vocabulary makes was silently lost.
* ``EndpointKind`` spelled "we could not type this" as ``other``, ``LineageNodeType`` as
  ``unknown``.
* The RSTLOGO code table was written out three times - in the lineage service, in the transformation
  models, and again as the table resolver's own kind list - so keeping them in step was a matter of
  remembering to.

:data:`BwObjectType` is the single vocabulary. :data:`TLOGO_TO_TYPE` is the single RSTLOGO decode.
:data:`TYPE_ALIASES` accepts every legacy spelling so an older caller's value still normalises, and
:func:`normalise_object_type` is the one function that turns anything into a canonical type.

:class:`BwObjectRef` is the canonical identity: a type plus a name, with ``id`` as ``type:name``.
BW technical names are near-unique across the system but not formally so - an InfoObject and a DSO
can share a name - so qualifying the id by type is what makes it safe to use as a graph key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field

#: Every kind of object this server can name. One closed vocabulary; anything untypable is
#: ``unknown`` rather than a second spelling of it.
BwObjectType = Literal[
    # --- inbound boundary -----------------------------------------------------------------
    "datasource",
    "infosource",
    "transfer_structure",
    "comm_structure",
    # --- data targets ---------------------------------------------------------------------
    "dso",  # classic DataStore Object
    "adso",  # advanced DSO
    "infocube",
    "multiprovider",
    "virtualprovider",
    "compositeprovider",
    "openodsview",
    "infoobject",
    # --- the things that move data --------------------------------------------------------
    "transformation",
    "dtp",
    "infopackage",
    "update_rule",
    "routine",
    # --- orchestration --------------------------------------------------------------------
    "chain",
    "process",
    # --- consumption ----------------------------------------------------------------------
    "query",
    "query_element",
    "report",
    # --- HANA and beyond ------------------------------------------------------------------
    "calcview",
    "source_object",  # an object in a source system, attached by a connector
    "analysis_auth",
    # --- honest gaps ----------------------------------------------------------------------
    "unresolved_dependency",  # a call the routine parser named but could not follow
    "unknown",
]

#: The BW logical-object (TLOGO) code as it appears in ``RSTRAN.SRCTLOGO``/``TGTTLOGO`` and
#: friends. Written once here; the lineage service, the transformation models and the table
#: resolver all decode through it rather than each keeping a copy.
TLOGO_TO_TYPE: dict[str, BwObjectType] = {
    "RSDS": "datasource",
    "TRCS": "infosource",
    "ISTS": "transfer_structure",
    "ODSO": "dso",
    "ADSO": "adso",
    "CUBE": "infocube",
    "MPRO": "multiprovider",
    "HCPR": "compositeprovider",
    "IOBJ": "infoobject",
    "ELEM": "query_element",
    "TRFN": "transformation",
    "DTPA": "dtp",
    "ISIP": "infopackage",
    "UPDR": "update_rule",
    "RSPC": "chain",
}

#: Legacy and alternative spellings, so a value from an older response still normalises. Kept
#: deliberately small: this is a compatibility shim, not a second vocabulary.
TYPE_ALIASES: dict[str, BwObjectType] = {
    "cube": "infocube",  # LineageNodeType's old spelling
    "basiccube": "infocube",
    "other": "unknown",  # EndpointKind's spelling of "untypable"
    "": "unknown",
    "iobj": "infoobject",
    "odso": "dso",
    "hcpr": "compositeprovider",
    "mpro": "multiprovider",
    "rsds": "datasource",
    "calc_view": "calcview",
    "calculationview": "calcview",
    "process_chain": "chain",
    "bex_query": "query",
}

#: Object types that hold or present data, as opposed to describing how it moves. Used wherever a
#: question is about "a thing you can query" rather than "a thing that runs".
PROVIDER_TYPES: frozenset[BwObjectType] = frozenset(
    {
        "dso",
        "adso",
        "infocube",
        "multiprovider",
        "virtualprovider",
        "compositeprovider",
        "openodsview",
    }
)

_CANONICAL: frozenset[str] = frozenset(
    {
        "datasource",
        "infosource",
        "transfer_structure",
        "comm_structure",
        "dso",
        "adso",
        "infocube",
        "multiprovider",
        "virtualprovider",
        "compositeprovider",
        "openodsview",
        "infoobject",
        "transformation",
        "dtp",
        "infopackage",
        "update_rule",
        "routine",
        "chain",
        "process",
        "query",
        "query_element",
        "report",
        "calcview",
        "source_object",
        "analysis_auth",
        "unresolved_dependency",
        "unknown",
    }
)


def normalise_object_type(value: object) -> BwObjectType:
    """Turn any object-type spelling into a canonical :data:`BwObjectType`.

    Accepts a canonical value, a legacy alias, or a raw TLOGO code. Anything else becomes
    ``unknown`` - never a silent pass-through, because an unrecognised type flowing into a graph key
    would split one object across two nodes.
    """
    text = str(value or "").strip()
    if text in _CANONICAL:
        return text  # type: ignore[return-value]
    upper = text.upper()
    if upper in TLOGO_TO_TYPE:
        return TLOGO_TO_TYPE[upper]
    lower = text.lower()
    if lower in _CANONICAL:
        return lower  # type: ignore[return-value]
    return TYPE_ALIASES.get(lower, "unknown")


def object_id(object_type: object, name: str) -> str:
    """The canonical id for an object: ``<type>:<NAME>``.

    Type-qualified because BW technical names are only near-unique - an InfoObject and a DSO may
    share one - so an unqualified name is not a safe graph key. Upper-cased on the name side because
    BW stores technical names upper-case and a mixed-case caller should still hit the same node.
    """
    return f"{normalise_object_type(object_type)}:{name.strip().upper()}"


class BwObjectRef(BaseModel):
    """A canonical reference to one BW object: what it is, what it is called, and its id.

    The one shape every subsystem uses to name an object, so a lineage node, a transformation
    endpoint, a search hit and a docs backlink all agree on what they are pointing at.
    """

    model_config = ConfigDict(extra="forbid")

    object_type: BwObjectType
    name: str
    #: Raw type code or subtype as BW stored it (TLOGO, CUBETYPE, ODSOTYPE, IOBJTP), kept so an
    #: undecoded value stays visible instead of being flattened into the canonical type.
    subtype: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """``<type>:<NAME>`` - stable, type-qualified, and safe as a graph key."""
        return object_id(self.object_type, self.name)

    @property
    def is_provider(self) -> bool:
        """True when the object holds or presents data, rather than describing how data moves."""
        return self.object_type in PROVIDER_TYPES

    @classmethod
    def from_tlogo(cls, code: object, name: str) -> BwObjectRef:
        """Build a reference from a raw TLOGO code, keeping the code as ``subtype``."""
        raw = str(code or "").strip()
        return cls(object_type=normalise_object_type(raw), name=name, subtype=raw or None)
