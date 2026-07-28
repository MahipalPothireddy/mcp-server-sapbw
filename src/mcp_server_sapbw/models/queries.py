"""Domain models for BEx queries (B7, mission Section 6).

A query is itself an element (its root ELTUID equals its COMPUID), so its own description comes from
RSZELTTXT joined on ELTUID = COMPUID. The definition is an element tree (RSZELTXREF, parent SELTUID
-> child TELTUID) whose nodes are typed by RSZELTDIR.DEFTP and positioned by RSZELTXREF.LAYTP.
Restrictions (RSZSELECT/RSZRANGE) attach to elements; variables (RSZGLOBV) carry a processing type
whose customer-exit value is a lineage dead end (resolved in ABAP at runtime, mission Known
Limitation 4). Field-level lineage paths a query's InfoObjects toward DataSource fields; usage comes
from RSZCOMPDIR.LASTUSED. Provenance on every fact.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance

# Element type from RSZELTDIR.DEFTP (decoded from DD07T).
ElementType = Literal[
    "query",  # REP (the root)
    "restricted_key_figure",  # SEL
    "calculated_key_figure",  # CKF
    "formula",  # FML
    "variable",  # VAR
    "structure",  # STR
    "filter",  # SOB
    "cell",  # CEL
    "attribute",  # ATR
    "none",  # NIL
    "unknown",
]

# Element position/role from RSZELTXREF.LAYTP (decoded from DD07T).
ElementRole = Literal[
    "rows",  # ROW
    "columns",  # COL
    "filter",  # FIX
    "free",  # FLT
    "variable",  # VAR
    "cell",  # CEL
    "structure_member",  # MBR
    "navigation",  # NAV
    "aggregated",  # AGG
    "other",
]

# Variable processing type from RSZGLOBV.VPROCTP (decoded from DD07T).
VariableProcessingType = Literal[
    "replacement_path",  # 1
    "customer_exit",  # 3  -> lineage dead end
    "sap_exit",  # 4
    "user_entry",  # 5
    "authorization",  # 6
    "hana_exit",  # 7
    "unknown",
]

# Variable kind from RSZGLOBV.VARTYP (decoded from DD07T).
VariableKind = Literal[
    "characteristic",  # 1
    "hierarchy_node",  # 2
    "text",  # 3
    "formula",  # 4
    "hierarchy",  # 5
    "unknown",
]

# How a query came into existence, read from the shape of its technical name (RSZCOMPDIR.COMPID).
#
#   designed - authored in Query Designer and given a technical name. A maintained report.
#   ad_hoc   - COMPID begins "!!". SAP generates that name for a query created directly in the BEx
#              Analyzer against a provider, without Query Designer; the common case is someone
#              wanting a quick look at the data. It is a navigation artefact, not a curated report,
#              so it should not be counted as a consumer when judging whether a provider is used.
#
# This is a NAME-SHAPE reading, not a stored flag: BW records no "is this a real report" column. It
# is therefore reported with its basis so a caller can discount it, and it is never the sole ground
# for a destructive recommendation.
QueryOrigin = Literal["designed", "ad_hoc"]

# Filter selector for list operations. "all" preserves the unfiltered result.
QueryOriginFilter = Literal["all", "designed", "ad_hoc"]


class Restriction(BaseModel):
    """A single restriction (RSZRANGE) on an element's InfoObject."""

    model_config = ConfigDict(extra="forbid")

    iobjnm: str
    sign: str | None = None  # I (include) / E (exclude)
    operator: str | None = None  # EQ / BT / CP / ...
    low: str | None = None  # literal value, or a variable name when low_is_variable
    high: str | None = None
    low_is_variable: bool = False  # LOWFLAG = 3 (value is a variable reference)
    high_is_variable: bool = False
    provenance: Provenance


class QueryElement(BaseModel):
    """One node of the query's element tree."""

    model_config = ConfigDict(extra="forbid")

    eltuid: str
    element_type: ElementType
    name: str | None = None  # MAPNAME (technical name), when the element is reusable/named
    description: str | None = None
    reusable: bool = False
    restrictions: list[Restriction] = Field(default_factory=list)
    calc_step_count: int = 0  # RSZCALC steps (CKF/formula definition size)
    provenance: Provenance


class QueryElementEdge(BaseModel):
    """A parent -> child edge in the element tree (role = where the child sits)."""

    model_config = ConfigDict(extra="forbid")

    parent_uid: str
    child_uid: str
    role: ElementRole
    position: int | None = None
    provenance: Provenance


class QueryVariable(BaseModel):
    """A BEx variable (RSZGLOBV). Customer-exit variables are a lineage dead end."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    iobjnm: str | None = None
    kind: VariableKind = "unknown"
    processing_type: VariableProcessingType = "unknown"
    is_customer_exit: bool = False  # processing_type == customer_exit (values resolve in ABAP)
    input_ready: bool = False
    provenance: Provenance


class Query(BaseModel):
    """A BEx query definition: header, element tree, restrictions, and variables."""

    model_config = ConfigDict(extra="forbid")

    compuid: str
    compid: str | None = None  # technical name (RSZCOMPDIR.COMPID)
    description: str | None = None
    active: bool = True
    provider: str | None = None  # master InfoProvider (RSZCOMPIC IS_MASTER)
    providers: list[str] = Field(default_factory=list)
    owner: str | None = None
    last_changed_by: str | None = None
    last_used: date | None = None
    elements: list[QueryElement] = Field(default_factory=list)
    edges: list[QueryElementEdge] = Field(default_factory=list)
    variables: list[QueryVariable] = Field(default_factory=list)
    truncated: bool = False
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class QuerySummary(BaseModel):
    """Compact query entry for list results."""

    model_config = ConfigDict(extra="forbid")

    compuid: str
    compid: str | None = None
    description: str | None = None
    provider: str | None = None
    owner: str | None = None
    last_used: date | None = None
    # Read from the technical-name shape, not from a stored flag - see QueryOrigin.
    origin: QueryOrigin = "designed"
    provenance: Provenance | list[Provenance]


class FieldLineageHop(BaseModel):
    """One hop in a field's lineage path (advisory when it passes through a routine)."""

    model_config = ConfigDict(extra="forbid")

    object_name: str
    object_type: str
    via: Literal["provider", "transformation", "dtp", "routine_lookup", "datasource"] = (
        "transformation"
    )
    advisory: bool = False


class FieldLineagePath(BaseModel):
    """Lineage of one InfoObject used in the query, from provider back toward a DataSource."""

    model_config = ConfigDict(extra="forbid")

    iobjnm: str
    provider: str | None = None
    hops: list[FieldLineageHop] = Field(default_factory=list)
    reaches_datasource: bool = False
    has_routine_hop: bool = False
    provenance: Provenance | list[Provenance]


class QueryLineage(BaseModel):
    """Field-level lineage for a whole query (one path per referenced InfoObject)."""

    model_config = ConfigDict(extra="forbid")

    compuid: str
    compid: str | None = None
    providers: list[str] = Field(default_factory=list)
    paths: list[FieldLineagePath] = Field(default_factory=list)
    customer_exit_variables: list[str] = Field(default_factory=list)  # lineage dead ends
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class QueryUsage(BaseModel):
    """Usage signal for a query, from RSZCOMPDIR.LASTUSED."""

    model_config = ConfigDict(extra="forbid")

    compuid: str
    compid: str | None = None
    last_used: date | None = None
    decommission_candidate: bool = False  # never used, or not used within the threshold window
    reason: str | None = None
    provenance: Provenance | list[Provenance]
