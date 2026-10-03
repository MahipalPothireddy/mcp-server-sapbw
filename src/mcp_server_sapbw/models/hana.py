"""Domain models for the HANA layer (B8).

Calc views live in the ``_SYS_BIC`` schema; their dependencies come from
``SYS.OBJECT_DEPENDENCIES`` (DEPENDENCY_TYPE=1 = direct). The BW<->HANA crossing table captures
both directions: a calc view reading BW data (``hana_reads_bw``) and a BW-layer object reading a
calc view (``bw_reads_hana``). Every fact cites ``SYS.OBJECT_DEPENDENCIES``.

**BW exposes its data to HANA under three generated naming schemes, and they are not
interchangeable.** Reading only the first is how a CompositeProvider went missing from a view's
bases while the providers beneath it were promoted into its place:

* ``/BIC/`` and ``/BI0/`` **tables** - a provider's physical storage. Resolves to a BW object by
  naming convention only, so the reading is advisory. A CompositeProvider has no such table.
* ``system-local.bw.bw2hana/<OBJECT>`` - one generated calculation view per InfoProvider, for
  *both* Advanced DSOs and CompositeProviders. What a modeller builds on, and the only route by
  which a CompositeProvider can be read. Parsed, then type-confirmed against the header tables.
* ``0BW:BIA:<PROVIDER>`` - the view BW generates to *consume* a calc view, with internal nodes
  suffixed ``:<node>`` / ``.<node>``. :class:`BwProviderView` carries the provider parsed from it
  with the type confirmed by lookup, which is what turns a raw dependency row into the calc-view
  -> CompositeProvider hop.

:attr:`BaseTableRef.resolution` and :attr:`HanaCrossing.resolution` name which scheme answered, so
a type-confirmed provider and a table-name convention never read as the same strength of fact.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .completeness import BoundedResult
from .evidence import Evidence, evidence_for
from .provenance import Provenance

CalcViewType = Literal["calc", "join", "olap", "hierarchy", "other"]
CrossingDirection = Literal["hana_reads_bw", "bw_reads_hana"]


class CalcView(BaseModel):
    """A HANA calc/analytic view in _SYS_BIC."""

    model_config = ConfigDict(extra="forbid")

    name: str
    schema_name: str = "_SYS_BIC"
    view_type: CalcViewType = "calc"
    base_table_count: int | None = None
    is_bw_consuming: bool = False  # reads /BIC/ or /BI0/ tables
    provenance: Provenance | list[Provenance]


#: How the BW object behind a calc-view base was identified. The three routes are not equally
#: strong, which is why the base carries which one was used rather than just the result.
BaseResolution = Literal["bic_table", "generated_provider_view", "unresolved"]


class BaseTableRef(BaseModel):
    """A base object a calc view reads, with a best-effort resolution to a BW object.

    ``resolution`` says *how* the BW object was identified, because the routes differ in strength
    and a caller acting on the answer needs to know which one it got:

    * ``bic_table`` - decomposed from a ``/BIC/`` or ``/BI0/`` generated table name. Advisory:
      nothing records which BW object owns a generated table.
    * ``generated_provider_view`` - BW's generated per-provider view
      (``system-local.bw.bw2hana/<OBJECT>``), parsed and then **type-confirmed** against the
      provider header tables. This is how a view reading a CompositeProvider resolves; a
      CompositeProvider has no ``/BIC/`` table, so the table route cannot see it at all.
    * ``unresolved`` - a base this server could not map back to a BW object.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    schema_name: str | None = None
    object_type: str | None = None  # HANA BASE_OBJECT_TYPE (TABLE / VIEW / ...)
    is_bw_generated: bool = False
    resolved_object: str | None = None
    resolved_kind: str | None = None
    resolution: BaseResolution = "unresolved"
    evidence: Evidence | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _derive_evidence(self) -> BaseTableRef:
        if self.evidence is None:
            detail = None
            if self.resolution == "generated_provider_view" and self.resolved_kind:
                detail = (
                    f"{self.table} is BW's generated view for {self.resolved_object}, "
                    f"type-confirmed as {self.resolved_kind} against the provider header tables."
                )
            self.evidence = evidence_for("calc_view_base", self.resolution, detail=detail)
        return self


class BwProviderView(BaseModel):
    """A BW-generated per-InfoProvider HANA view (``0BW:BIA:<PROVIDER>``) and its owner.

    The provider name is parsed from the view name; ``resolved_kind`` is confirmed against the
    provider header tables, and ``verified`` says whether that confirmation succeeded. An
    unverified entry names the parsed provider without asserting it exists.
    """

    model_config = ConfigDict(extra="forbid")

    view_name: str
    provider: str
    resolved_kind: str | None = None  # 'compositeprovider' / 'adso' / 'dso' / cube variant
    verified: bool = False
    provenance: Provenance


class CalcViewLineage(BoundedResult):
    """A calc view's direct base tables and the BW providers that consume it.

    ``consuming_bw_providers`` is the BW side of the boundary: the InfoProviders whose generated
    ``0BW:BIA:`` views read this calc view. For a CompositeProvider that is exactly the
    calc-view -> CompositeProvider hop, which BW's own where-used lists do not report.
    """

    view_name: str
    schema_name: str = "_SYS_BIC"
    base_tables: list[BaseTableRef] = Field(default_factory=list)
    resolved_bw_objects: list[str] = Field(default_factory=list)
    consuming_bw_providers: list[BwProviderView] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class HanaCrossing(BaseModel):
    """One BW<->HANA boundary crossing (a single dependency edge across the boundary)."""

    model_config = ConfigDict(extra="forbid")

    direction: CrossingDirection
    hana_object: str  # the calc view (in _SYS_BIC)
    bw_object: str  # the BW-layer object (a /BIC/ table, or an ABAP-schema view/synonym)
    # Resolved BW object: from the /BIC/ table name (advisory, naming-based) or from a
    # '0BW:BIA:<PROVIDER>' view name (parsed, then type-confirmed against the header tables).
    bw_object_resolved: str | None = None
    bw_object_kind: str | None = None
    resolution: Literal[
        "bic_table", "bw_provider_view", "generated_provider_view", "unresolved"
    ] = "unresolved"
    object_type: str | None = None  # the HANA object type on the BW side (TABLE/VIEW/SYNONYM)
    evidence: Evidence | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _derive_evidence(self) -> HanaCrossing:
        if self.evidence is None:
            detail = None
            type_confirmed = {"bw_provider_view", "generated_provider_view"}
            if self.bw_object_resolved:
                detail = f"{self.bw_object} was read as BW object {self.bw_object_resolved}" + (
                    f", type-confirmed as {self.bw_object_kind}."
                    if self.resolution in type_confirmed and self.bw_object_kind
                    else " by the generated-table naming convention, which nothing records."
                )
            self.evidence = evidence_for("hana_crossing", self.resolution, detail=detail)
        return self


CalcViewNodeType = Literal["projection", "join", "aggregation", "union", "rank", "other"]


class CalcViewDataSourceRef(BaseModel):
    """One input a calc view reads, as the definition names it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    #: As stored: CALCULATION_VIEW, DATA_BASE_TABLE, DATA_BASE_VIEW.
    source_type: str | None = None
    schema_name: str | None = None
    #: The catalog object for a table/view source.
    column_object: str | None = None
    #: The repository path for a calc-view source, e.g. ``/pkg/calculationviews/NAME``.
    resource_uri: str | None = None
    resolved_object: str | None = None  # BW object behind a /BIC/ or /BI0/ table (advisory)
    resolved_kind: str | None = None
    provenance: Provenance


class CalcViewCalculatedColumn(BaseModel):
    """A column the view computes, with the expression that computes it.

    This is the answer to "where does this number come from" when the answer is not a BW
    transformation. Reported verbatim rather than summarised: a formula that has been paraphrased
    cannot be checked against the view.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    formula: str | None = None
    datatype: str | None = None
    length: str | None = None
    expression_language: str | None = None
    #: The node that computes it, so the column can be found in the modeller.
    node: str | None = None


class CalcViewColumnMapping(BaseModel):
    """One column crossing one node boundary."""

    model_config = ConfigDict(extra="forbid")

    target: str
    source: str | None = None
    #: Set instead of ``source`` when the mapping supplies a constant.
    value: str | None = None
    #: The mapping's own xsi:type, which distinguishes an attribute mapping from a constant one.
    kind: str | None = None
    from_node: str | None = None


class CalcViewNode(BaseModel):
    """One node of the calculation scenario: a projection, join, aggregation or union."""

    model_config = ConfigDict(extra="forbid")

    id: str
    node_type: CalcViewNodeType = "other"
    #: The stored xsi:type, kept so an unmapped node kind is still reported as itself.
    raw_type: str | None = None
    join_type: str | None = None  # inner / leftOuter / rightOuter / fullOuter / referential
    cardinality: str | None = None
    join_order: str | None = None
    join_attributes: list[str] = Field(default_factory=list)
    #: Node or data-source ids feeding this node.
    inputs: list[str] = Field(default_factory=list)
    mappings: list[CalcViewColumnMapping] = Field(default_factory=list)
    filter_expression: str | None = None


class CalcViewParameter(BaseModel):
    """An input parameter or a variable the view declares.

    ``is_input_parameter`` reflects ``parameter="true"`` in the definition. Both are declared the
    same way, and the distinction decides whether a value is supplied by the caller at query time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    is_input_parameter: bool = False
    description: str | None = None
    datatype: str | None = None
    length: str | None = None
    mandatory: bool | None = None
    selection_type: str | None = None


class CalcViewSemanticColumn(BaseModel):
    """A column the view publishes, and how it is aggregated."""

    model_config = ConfigDict(extra="forbid")

    name: str
    role: Literal["attribute", "measure"]
    description: str | None = None
    aggregation: str | None = None  # sum / min / max / count / avg
    measure_type: str | None = None
    is_key: bool = False
    calculated: bool = False
    origin_node: str | None = None
    origin_column: str | None = None
    formula: str | None = None


class CalcViewDefinition(BoundedResult):
    """What a calculation view actually does, read from its activated definition.

    ``parsed`` is the field to read first. False means the logic could not be read and
    ``unparsed_reason`` says why - the definition is absent from the repository, too large to read
    within the stated bound, or not a shape this parser recognises. An unreadable definition is
    reported as unreadable rather than as a view with no logic, because those are opposite readings
    and only one of them is about the view.
    """

    model_config = ConfigDict(extra="forbid")

    view_name: str
    schema_name: str = "_SYS_BIC"
    package_id: str | None = None
    object_name: str | None = None
    description: str | None = None
    changed_at: str | None = None  # as stored; not normalised to a timestamp type
    data_category: str | None = None  # CUBE / DIMENSION
    output_view_type: str | None = None
    schema_version: str | None = None
    scenario_type: str | None = None
    #: True when BW generated this view rather than a person modelling it. BW-generated definitions
    #: are machine-produced projections whose interesting facts are base tables and consumers, which
    #: ``bw_get_calc_view_lineage`` already answers.
    is_bw_generated: bool = False
    definition_bytes: int | None = None
    parsed: bool = False
    unparsed_reason: str | None = None
    final_node: str | None = None
    applies_analytic_privilege: bool = False
    data_sources: list[CalcViewDataSourceRef] = Field(default_factory=list)
    nodes: list[CalcViewNode] = Field(default_factory=list)
    calculated_columns: list[CalcViewCalculatedColumn] = Field(default_factory=list)
    semantic_columns: list[CalcViewSemanticColumn] = Field(default_factory=list)
    input_parameters: list[CalcViewParameter] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    #: Node counts by kind, so the shape is answerable without walking every node.
    node_counts: dict[str, int] = Field(default_factory=dict)
    #: Elements inside the semantic layer this grammar does not cover: a gap, not an absence.
    unrecognised_elements: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    evidence: Evidence | None = None
    provenance: Provenance | list[Provenance]


class HanaCrossingReport(BoundedResult):
    """The bidirectional BW<->HANA crossing table for a scope."""

    crossings: list[HanaCrossing] = Field(default_factory=list)
    total_count: int = 0
    hana_reads_bw_count: int = 0
    bw_reads_hana_count: int = 0
    caveats: list[str] = Field(default_factory=list)
