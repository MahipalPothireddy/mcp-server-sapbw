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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .aggregation import AggregationRule, ExceptionAggregation
from .completeness import BoundedResult
from .evidence import Evidence, evidence_for
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


class ValueSource(BaseModel):
    """How an element setting's value is specified (``RSZELTPROP`` ``*FLAG`` columns).

    Two things matter here, and both were learnt from live data rather than assumed.

    ``runtime_resolved`` - a variable-driven setting resolves per execution, so metadata can name
    the mechanism but not the effective value. Reporting the mechanism as though it were the value
    is the same mistake as presenting a customer-exit variable's lineage as complete.

    ``value_holds`` - the paired value column does not always hold what its name suggests. When the
    flag says "reference to another element", ``HIENM`` holds a 25-character element UID, not a
    hierarchy name; observed on a live query whose root element stores exactly that. Rendering it as
    a hierarchy name would put a UID in front of a user as though it were an object they could look
    up.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    label: str | None = None
    confidence: Literal["dictionary", "advisory"] = "dictionary"
    evidence: Evidence | None = None
    runtime_resolved: bool = False
    value_holds: Literal[
        "literal",  # the value column holds the thing itself
        "element_uid",  # it holds a reference to another query element
        "variable_name",  # it holds a variable, resolved per execution
        "infoobject",  # it holds an InfoObject whose value supplies the setting
        "constant",
        "unknown",
    ] = "unknown"

    @model_validator(mode="after")
    def _derive_evidence(self) -> ValueSource:
        if self.evidence is None:
            self.evidence = evidence_for("code_decode", self.confidence)
        return self


class CurrencyTranslation(BaseModel):
    """Currency translation configured on a query element.

    A translated figure is denominated in something other than the currency the records were stored
    in, so it cannot be reconciled against the source data by inspection. ``translation_type``
    (``CTTNM``) is where the exchange-rate type, rate date and source/target rules live - it is a
    customising object, so the name is reported rather than its resolved behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    target_currency: str | None = None
    target_source: ValueSource | None = None
    translation_type: str | None = None
    key_date: str | None = None
    key_date_source: ValueSource | None = None


class UnitConversion(BaseModel):
    """Unit-of-measure conversion configured on a query element."""

    model_config = ConfigDict(extra="forbid")

    target_unit: str | None = None
    unit_infoobject: str | None = None
    target_source: ValueSource | None = None


class DisplayHierarchy(BaseModel):
    """The hierarchy a query element is displayed along, and how it was chosen.

    A hierarchy chosen by a variable groups the data differently per execution, which changes both
    the rows shown and the totals - so ``source.runtime_resolved`` is the field to read before
    trusting a comparison between two runs of the same report.

    ``hierarchy`` is the value as stored, which is not always a hierarchy name: read
    ``source.value_holds`` first. When the source is a reference to another element the column holds
    a 25-character element UID, observed live on a query root.
    """

    model_config = ConfigDict(extra="forbid")

    hierarchy: str | None = None
    source: ValueSource | None = None
    version: str | None = None
    valid_to: str | None = None
    start_level: int | None = None
    active: bool = False


class ElementProperties(BaseModel):
    """``RSZELTPROP``: what happens to a query element's value after its rows are selected.

    Restrictions say which rows an element covers. These settings say what is then done to the
    number - translated to another currency, aggregated locally as a last value rather than a sum,
    shown with its sign inverted, read against a different key date, or hidden. Two people comparing
    figures from one report can both be reading it correctly and still disagree; several of the
    reasons live here.

    ``changes_the_number`` names the settings that actually alter the value, so a caller does not
    have to work out which of a dozen properties are cosmetic.
    """

    model_config = ConfigDict(extra="forbid")

    eltuid: str
    currency_translation: CurrencyTranslation | None = None
    unit_conversion: UnitConversion | None = None
    display_hierarchy: DisplayHierarchy | None = None
    # Local aggregation for a structure member (STRMEM_LAGGR). Overrides how this one element
    # combines, independently of the key figure's own aggregation.
    local_aggregation: AggregationRule | None = None
    local_aggregation_direction: str | None = None
    total_suppression: str | None = None
    total_suppressed: bool = False
    display: str | None = None
    hidden: bool = False
    sign_inverted: bool = False
    constant_selection: bool = False
    cumulative: bool = False
    key_date: str | None = None
    key_date_source: ValueSource | None = None
    changes_the_number: list[str] = Field(default_factory=list)
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
    # How this element's number combines. Absent means RSZCALC records no aggregation for it, which
    # is the normal case for a characteristic or a plain key-figure reference.
    standard_aggregation: AggregationRule | None = None
    exception_aggregation: ExceptionAggregation | None = None
    # RSZELTPROP settings. Absent when the element has no row there, or when the table is
    # unavailable on this release - in which case the query carries a caveat saying so.
    properties: ElementProperties | None = None
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


class Query(BoundedResult):
    """A BEx query definition: header, element tree, restrictions, and variables."""

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
    """One hop in a field's lineage path (advisory when it passes through a routine).

    When the hop crosses a transformation, the rule-level fields say *how* the field was derived:
    ``target_field`` is the field being populated, ``rule_type`` is BW's own rule classification
    (direct, constant, formula, routine, master-data read, time conversion), and ``source_fields``
    are the inputs the rule reads. A ``routine`` rule carries ``routine_code_id`` and is marked
    advisory, because what the ABAP actually reads is a heuristic lower bound.
    """

    model_config = ConfigDict(extra="forbid")

    object_name: str
    object_type: str
    via: Literal["provider", "transformation", "dtp", "routine_lookup", "datasource"] = (
        "transformation"
    )
    advisory: bool = False
    evidence: Evidence | None = None
    # Rule-level detail, populated for transformation hops.
    target_field: str | None = None
    rule_type: str | None = None
    source_fields: list[str] = Field(default_factory=list)
    transformation_id: str | None = None
    routine_code_id: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _derive_evidence(self) -> FieldLineageHop:
        """Reuse the lineage-edge vocabulary: a hop is an edge seen from a field's point of view."""
        if self.evidence is None:
            detail = None
            if self.advisory and self.routine_code_id:
                detail = (
                    f"This hop's inputs come from parsing routine {self.routine_code_id}, so what "
                    "the ABAP actually reads is a lower bound rather than a declared mapping."
                )
            elif not self.advisory and self.rule_type and self.target_field:
                detail = (
                    f"Rule type {self.rule_type!r} on field {self.target_field} declares this "
                    "derivation in the transformation's own metadata."
                )
            self.evidence = evidence_for(
                "lineage_edge", "advisory" if self.advisory else "exact", detail=detail
            )
        return self


class FieldLineagePath(BaseModel):
    """Lineage of one InfoObject used in the query, from provider back toward a DataSource.

    ``resolution`` says how far the walk got, which is the difference between a real answer and a
    shrug:

    * ``field`` — followed rule by rule through ``RSTRANFIELD``/``RSTRANRULE``: this field's own
      derivation, not the provider's.
    * ``provider`` — no rule populating this field was found, so the path falls back to the
      provider's upstream objects. The field's specific derivation is unknown.
    * ``none`` — nothing upstream resolved at all.

    A caller must be able to tell these apart: a ``provider``-level path repeated across many
    InfoObjects looks like field lineage but is not, and treating it as such is how wrong
    conclusions get drawn about which source field feeds a number.
    """

    model_config = ConfigDict(extra="forbid")

    iobjnm: str
    provider: str | None = None
    hops: list[FieldLineageHop] = Field(default_factory=list)
    reaches_datasource: bool = False
    has_routine_hop: bool = False
    resolution: Literal["field", "provider", "none"] = "provider"
    unresolved_reason: str | None = None
    evidence: Evidence | None = None
    provenance: Provenance | list[Provenance]

    @model_validator(mode="after")
    def _derive_evidence(self) -> FieldLineagePath:
        if self.evidence is None:
            self.evidence = evidence_for(
                "field_lineage", self.resolution, detail=self.unresolved_reason
            )
            # A routine hop anywhere makes the whole path a lower bound, whatever the resolution:
            # the walk itself was exact, but what the routine reads is a heuristic.
            if self.has_routine_hop and self.evidence.completeness == "complete":
                self.evidence = self.evidence.model_copy(
                    update={
                        "completeness": "lower_bound",
                        "detail": (self.evidence.detail or "")
                        + " At least one hop passes through a routine, so the inputs on that hop "
                        "are a parsed lower bound rather than a declared mapping.",
                    }
                )
        return self


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
