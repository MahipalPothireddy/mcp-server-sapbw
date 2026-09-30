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
#
# ``SEL`` needs a second input and this is the whole of D28. BW uses one code for *selection*, and a
# selection serves two unrelated purposes: a restricted key figure, and a plain characteristic
# placed on an axis or in a filter. Decoding ``SEL`` as ``restricted_key_figure`` unconditionally
# was wrong for most of them - measured across 400 production queries, 54,788 ``SEL`` elements are
# characteristic placements against 6,480 that are key-figure-side. On the subject query it
# reported 78 characteristics as restricted key figures and made the count "75 restricted key
# figures" mean something other than what it says.
#
# The discriminator is the element's own ``RSZSELECT`` rows: a key-figure-side selection restricts
# ``1KYFNM`` (the key-figure dimension), a characteristic placement does not. It also explains why
# ``name`` was null on 61 of those 75 - a characteristic placement has no technical name of its own.
#
# ``SEL`` needs a *third* input, and this is D26. ``RSZELTDIR.SUBDEFTP`` is a declared column with
# 14 fixed values (domain ``RSZSUBDEFTP``) that states the element's real type outright, including 2
# the ``1KYFNM`` split cannot see at all: ``CON`` Condition and ``EXC`` Exception. Neither restricts
# ``1KYFNM``, so both were being classified as characteristic placements - on the reference system
# 127 conditions and 77 exceptions, and on the subject query exactly 4 of the 45 reported
# characteristics were conditions. A count that says "45 characteristics" and means "41 plus 4
# conditions" is a wrong fact, which is why this sits with D28-D31 rather than with the gaps.
ElementType = Literal[
    "query",  # REP (the root)
    "restricted_key_figure",  # SEL restricting 1KYFNM
    #: SEL naming an InfoObject but not 1KYFNM: a characteristic sitting on an axis or in a filter.
    "characteristic",
    #: SEL with ``SUBDEFTP='CON'``: a ranking or threshold condition applied to the result set.
    #: Reported as its own type because a condition changes which rows a reader sees while changing
    #: no figure, so counting it as a characteristic placement misstates both.
    "condition",
    #: SEL with ``SUBDEFTP='EXC'``: an exception, which colours cells against thresholds. Same
    #: storage and the same previous misclassification; 38 active queries on the reference system.
    "exception",
    #: SEL with no RSZSELECT row to classify it either way. Named rather than folded into
    #: ``unknown`` so "a selection we could not classify" stays distinct from "an unknown code".
    "selection",
    "calculated_key_figure",  # CKF
    "formula",  # FML
    "variable",  # VAR
    "structure",  # STR
    "filter",  # SOB
    #: SHT - the query sheet, the layout node that owns the axes. Previously fell through to
    #: ``unknown`` (D29), which made the axis tree unreachable by element type even though this one
    #: node is the parent of every placement in the query.
    "query_sheet",
    "cell",  # CEL
    "attribute",  # ATR
    "none",  # NIL
    "unknown",
]

# Where an element actually sits, derived from (parent element DEFTP, edge LAYTP) together.
#
# This is deliberately *not* ``ElementRole``. ``role`` is a faithful decode of one column against
# SAP's own domain and stays that way; ``axis`` is a derived fact needing two inputs, so it gets its
# own name and its own basis rather than overwriting a correct decode with an interpretation.
#
# The reason it needs two inputs is D30: the same LAYTP means different things under different
# parents. Measured across 400 production queries, ``AGG`` is pure ``SEL``-characteristic under both
# parents but denotes free characteristics under the query sheet (26,748 edges) and filter
# characteristics under the selection object (28,040 edges) - 54,788 edges whose meaning only the
# parent settles. Reporting them all as "aggregated" left the S04 subject's 37 free characteristics
# and 9 filter characteristics indistinguishable, and its 28 column members labelled "free".
QueryAxis = Literal[
    "rows",  # (SHT, ROW)
    "columns",  # (SHT, COL)
    "free_characteristics",  # (SHT, AGG)
    "filter",  # (SOB, AGG) - the query's global filter
    "structure_member",  # (SHT, FLT) - a member of the structure that sits on an axis
    "attribute_order",  # (SHT, ATR)
    "variable_sequence",  # (REP, VAR)
    #: A structure reached again from the filter node. The same structure already has its placement
    #: from the query sheet, so counting this as a second one would double-count the axis.
    "reference",
    #: The query root's own scaffolding children: the sheet and the filter nodes themselves.
    "structural",
    #: LAYTP ``NIL`` - referenced without being placed, whatever the parent.
    "unplaced",
    #: A (parent, LAYTP) pair with no measured meaning. ``axis_basis`` names the pair so an
    #: unmapped combination is visible rather than guessed at.
    "unknown",
]

# Element position/role from RSZELTXREF.LAYTP, decoded against the field's own domain RSZLAYTP in
# DD07T. All 17 documented values are covered.
#
# The decode originally held 9 of them, and the other 8 fell through to ``other``. Silently: on the
# reference system that put 40,233 of 164,877 active edges (24.4%) into a bucket that reads as
# "miscellaneous axis" (D24). ``unplaced`` is the one that cost the most - ``NIL`` "No Layout" is
# 38,164 of those edges, and it does not mean an unknown axis, it means the element is *referenced
# without being placed on one*. A query's reusable definitions all look like this, so conflating
# them with a real axis makes the element tree impossible to reconcile against Query Designer.
ElementRole = Literal[
    "rows",  # ROW - Row
    "columns",  # COL - Column
    "filter",  # FIX - Filter
    "free",  # FLT - Formatted Reporting - Order (free chars)
    "variable",  # VAR - Variable Sequence
    "cell",  # CEL - Cell
    "structure_member",  # MBR - Structure element
    "navigation",  # NAV - Navigation
    "aggregated",  # AGG - Aggregated
    #: Referenced by the query but not placed on any axis - a reusable definition, not a layout
    #: slot.
    "unplaced",  # NIL - No Layout
    "query_sheet",  # SHT - Query Sheet
    "selection_object",  # SOB - Selection Object
    "attribute_order",  # ATR - Order of Attributes
    "query_variable_sequence",  # QVR - Variable Sequence of Query Variables
    "operand",  # OPD - Operand
    "area",  # RNG - Area
    "internal",  # REP - Internal Use
    #: Reached only by a code the domain does not document. The raw code is kept on the edge's
    #: ``role_code`` so an unrecognised value is auditable rather than indistinguishable from a
    #: documented-but-miscellaneous one.
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


#: What a value column actually holds, once its paired ``*FLAG`` has been decoded. Named so callers
#: outside this module can build a :class:`ValueSource` without restating the literal.
ValueHolds = Literal[
    "literal",  # the value column holds the thing itself
    "element_uid",  # it holds a reference to another query element
    "variable_name",  # it holds a variable, resolved per execution
    "infoobject",  # it holds an InfoObject whose value supplies the setting
    "constant",
    "unknown",
]


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
    value_holds: ValueHolds = "unknown"

    @model_validator(mode="after")
    def _derive_evidence(self) -> ValueSource:
        if self.evidence is None:
            self.evidence = evidence_for("code_decode", self.confidence)
        return self


class DecodedCode(BaseModel):
    """A raw BW code together with whatever meaning could be established, and on what basis.

    The raw ``code`` is always reported. That is the point: a code this system uses but SAP's own
    domain does not declare still has to reach the reader as itself rather than be dropped or
    guessed at. ``confidence='advisory'`` marks exactly that case.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    label: str | None = None
    confidence: Literal["dictionary", "advisory"] = "dictionary"
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def _derive_evidence(self) -> DecodedCode:
        if self.evidence is None:
            self.evidence = evidence_for("code_decode", self.confidence)
        return self


class AlertLevel(BaseModel):
    """One threshold band of an exception, with the severity BW assigns to it.

    An exception is not one threshold. It is a set of bands, each an ``RSZRANGE`` row with its own
    operator, bounds and ``ALERTLEVEL`` - and the level is what makes the band mean something: the
    same range flags a success or a problem depending on it. Decoded against the column's own
    declared domain ``RSRA_ALERT_LEVEL``: nine values, ``01``-``03`` Good 1-3, ``04``-``06``
    Critical 1-3, ``07``-``09`` Bad 1-3. All nine occur on the reference system.

    Reporting only one band - which this server did when exceptions were first added - turns a
    three-band exception into a single rule and silently drops the rest (D41).
    """

    model_config = ConfigDict(extra="forbid")

    level: DecodedCode
    operator: DecodedCode | None = None
    low: str | None = None
    high: str | None = None
    provenance: Provenance


class QueryCondition(BaseModel):
    """A condition or exception evaluated on the query's result set.

    Stored as an element (``DEFTP='SEL'`` with ``SUBDEFTP='CON'`` or ``'EXC'``) rather than in any
    condition table - there is no such table, which is what made this look like a missing read. The
    definition is two ``RSZRANGE`` rows keyed ``IOBJNM='1CONDITION'``: one pointing at the key
    figure being ranked, one carrying the operator and the threshold.

    **``active`` is the field to read first.** A condition that exists but is switched off changes
    nothing a reader sees, so "this query has 4 Top N conditions" means something entirely different
    depending on it. It comes from ``RSZSELECT.ACTIVE``, which is declared and does vary in practice
    - 89 active against 45 inactive across the reference system's condition rows.

    **The threshold is usually not a number.** On the reference system it is a variable reference
    far more often than a literal, so the figure a condition actually cuts at resolves per execution
    and is not in metadata. ``threshold_source`` says which case this is; when it names a variable,
    the variable's own record says whether even *that* is knowable.
    """

    model_config = ConfigDict(extra="forbid")

    eltuid: str
    kind: Literal["condition", "exception"]
    name: str | None = None  # MAPNAME; a condition is normally unnamed
    description: str | None = None  # RSZELTTXT - in practice the only readable label
    active: bool
    #: ``RSZSELECT.CONTYPE``. An independent second statement of the same thing ``SUBDEFTP`` says,
    #: kept because two declared columns agreeing is stronger evidence than either alone.
    condition_type: DecodedCode | None = None
    #: ``RSZRANGE.OPT`` on the ``1VALUE`` row: ranking versus threshold. Note that the two ranking
    #: operators this system uses are **not** declared fixed values of the operator domain, so they
    #: arrive as ``advisory``.
    operator: DecodedCode | None = None
    #: Raw ``LOW``. A variable uid, an element uid or a literal depending on ``threshold_source``.
    threshold: str | None = None
    #: ``RSZRANGE.LOWFLAG``, which is a declared value-source flag - so "is this a real number or a
    #: variable" is answerable exactly rather than inferred from the shape of the string.
    threshold_source: ValueSource | None = None
    #: The element uid of the key figure being ranked or tested (``1STRUC`` row, ``LOWFLAG='2'``).
    #: Present for both kinds - every one of the reference system's 77 exceptions and 127 conditions
    #: has exactly one.
    measure_eltuid: str | None = None
    #: That element's description, when the query's own element tree carries one for it.
    measure_description: str | None = None
    #: **Exceptions only.** The threshold bands, each with its own severity. Empty for a condition,
    #: which is not an oversight: measured across all 254 of the reference system's condition range
    #: rows, ``ALERTLEVEL`` is always ``00``, so a condition provably has no bands. The two kinds
    #: share a table and a column but genuinely differ in shape, and flattening them would have to
    #: invent an alert level for conditions or drop the levels from exceptions (D41).
    alert_levels: list[AlertLevel] = Field(default_factory=list)
    #: **Exceptions only.** The characteristics the exception is evaluated at, stored as extra
    #: ``RSZRANGE`` rows whose ``FACIOBJNM`` is a real characteristic rather than a structural
    #: pseudo-object. Without these, "which rows get coloured" is unanswerable.
    drilldown_characteristics: list[str] = Field(default_factory=list)
    #: **Exceptions only.** ``RSZSELECT.EXCABSREL``, declared: results only, or all rows. It decides
    #: whether the colouring applies to totals or to every row, which changes what a reader sees.
    evaluation_scope: DecodedCode | None = None
    provenance: list[Provenance] = Field(default_factory=list)


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
    #: For a characteristic placement, the InfoObject it places (``RSZSELECT.IOBJNM``). Such an
    #: element has no ``MAPNAME`` of its own, so without this it is an anonymous row - which is why
    #: only 31 of the subject query's 107 elements carried any identifier at all. Absent for every
    #: other element type, and never set to ``1KYFNM``, which is the key-figure dimension rather
    #: than a business characteristic.
    iobjnm: str | None = None
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
    #: The raw ``LAYTP`` this role was decoded from, kept for the same reason ``aggregation_code``
    #: is kept beside its decode: a value the domain gains later reports as ``other`` here, and
    #: without the code there is no way to tell that from a documented placement (D24).
    role_code: str | None = None
    #: Where the child actually sits, derived from the parent's DEFTP together with ``role_code``.
    #: This is the field to read when reconciling against Query Designer; ``role`` is the literal
    #: single-column decode and cannot answer it (D30).
    axis: QueryAxis = "unknown"
    #: How ``axis`` was arrived at, naming the (parent DEFTP, LAYTP) pair. Present on every edge,
    #: including the ones that came out ``unknown`` - an unmapped pair says so here rather than
    #: being indistinguishable from a mapped one.
    axis_basis: str | None = None
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
    #: Conditions and exceptions, which restrict or colour the result set without changing any
    #: figure. Empty means the query has none; the tree is always walked, so empty is an answer here
    #: rather than a silence (D26).
    conditions: list[QueryCondition] = Field(default_factory=list)
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

    A ``composite_part`` hop crosses a CompositeProvider, which has no transformation at all: its
    mapping is declared in the model BW stores on ``RSOHCPR``. That hop routinely **fans out** - a
    union is normally fed the same element by several parts - so ``source_objects`` carries every
    object that supplies the field while ``object_name`` names the one this chain follows. The two
    stand in the same relation as ``LineageEdge.update_mode`` to ``update_modes``: a scalar for
    readers that want one value, the full set beside it so nothing is hidden by the choice.
    """

    model_config = ConfigDict(extra="forbid")

    object_name: str
    object_type: str
    via: Literal[
        "provider",
        "transformation",
        "dtp",
        "routine_lookup",
        "datasource",
        "composite_part",
        "calc_view",
        #: The field is a navigation attribute, so the hop crosses from the provider to the
        #: characteristic it hangs off - no transformation involved, because none populates it into
        #: the provider. Declared in ``RSDATRNAV``, so ``observed`` rather than advisory (D23/D32).
        "nav_attribute",
        #: The object reached holds no master data of its own: it is a *reference characteristic*,
        #: whose attribute, SID, text and view tables all belong to the characteristic it
        #: references. Nothing loads into it, so the hop crosses to the referenced characteristic
        #: where the field is actually populated. Declared in ``RSDCHA.CHABASNM`` (D35).
        "reference_characteristic",
        #: The provider is a MultiProvider, which holds no data and has no inbound transformation:
        #: it unions its part providers. The hop crosses to the part that supplies the field, named
        #: by BW's own InfoObject identification on ``RSDICMULTIIOBJ`` - which is read rather than
        #: assumed, because identification may map the provider's field to a *differently named*
        #: field in the part (measured: 66 of 5,372 active rows on the reference system) (D22).
        "multiprovider_part",
    ] = "transformation"
    advisory: bool = False
    evidence: Evidence | None = None
    # Rule-level detail, populated for transformation hops.
    target_field: str | None = None
    rule_type: str | None = None
    source_fields: list[str] = Field(default_factory=list)
    transformation_id: str | None = None
    routine_code_id: str | None = None
    #: Every object that supplies ``target_field`` at this hop, when more than one does. Empty when
    #: the hop has a single source, so its presence is itself the signal that a choice was made.
    #: Sorted, so which object ``object_name`` names is reproducible and not row-order dependent.
    source_objects: list[str] = Field(default_factory=list)
    #: The CompositeProvider input aliases traversed to reach this part, outermost first. More than
    #: one entry means the field came through a stacked model's internal node.
    via_aliases: list[str] = Field(default_factory=list)
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
    #: Every DataSource the query's provider reaches upstream. A property of the *provider*, carried
    #: once here rather than repeated on every path that falls back to it - it is identical for
    #: each, and on a measured production query repeating it cost 6,500 hops asserting a shape that
    #: does not exist. A path with ``resolution='provider'`` points here instead of restating it.
    provider_datasources: list[str] = Field(default_factory=list)
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
