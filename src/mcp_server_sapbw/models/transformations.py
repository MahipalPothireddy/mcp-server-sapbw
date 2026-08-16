"""Domain models for transformations and routine analysis (B5).

Covers the transformation header (source/target endpoints + routine code-ids), field-level rule
mappings (target field <- rule type <- source fields, with a routine reference where the rule is a
routine), full routine source (RoutineCode), and the heuristic routine analysis (table dependencies,
anti-patterns, unresolved calls, complexity). Routine analysis is always a **lower bound**: dynamic
SQL, function-module and class-method calls are not followed by static parsing (mission Known
Limitation 3), and every RoutineAnalysis says so in its payload.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import Evidence, evidence_for
from .provenance import Provenance

# Rule type decoded from RSTRANRULE.RULETYPE (values are self-describing on the wire).
RuleType = Literal[
    "direct",
    "constant",
    "routine",
    "formula",
    "master",  # master-data lookup / read
    "time",  # time conversion
    "unit",
    "start",
    "end",
    "expert",
    "adso",
    "odso",
    "hier_split",
    "unknown",
]

# Which routine slot the ABAP belongs to. "exit" is not a BW transformation slot: it marks ABAP
# read from a source system's extractor exit, which the same parser analyses for the same signals.
RoutineKind = Literal[
    "start", "end", "expert", "global", "field", "formula", "unit", "exit", "unknown"
]

# Aggregation behaviour of a rule (RSTRANRULE.AGGR). Decoded from the ABAP dictionary domain
# RSTRAN_AGGREGATION (verified live), NOT assumed: MOV/SUM/MIN/MAX/NOP.
AggregationBehaviour = Literal[
    "direct_assignment",  # MOV - overwrite the target value
    "summation",  # SUM - add to the target value
    "minimum",  # MIN
    "maximum",  # MAX
    "none",  # NOP - no aggregation
]

# Rule group type (RSTRANRULE.GROUPTYPE, domain RSTRAN_GROUPTYPE).
RuleGroupType = Literal["standard", "normal", "return_table", "technical_fields"]

# What a declared lookup reads.
LookupKind = Literal["master_data", "dso", "adso"]

# What BW does when a declared lookup finds no record (RSTRANSTEPODSO/ADSO.BEHAVIOR,
# domain RSTRAN_ODSO).
LookupMissBehaviour = Literal["error", "constant"]

# Key date a time-dependent master-data lookup reads at (RSTRANSTEPMASTER.MPER, domain RSMPER).
LookupKeyDate = Literal["period_start", "period_end", "current_date", "constant_date"]

# BW logical-object type of a transformation endpoint (RSTLOGO code -> readable kind).
EndpointKind = Literal[
    "datasource",  # RSDS
    "infosource",  # TRCS
    "dso",  # ODSO
    "adso",  # ADSO
    "infocube",  # CUBE
    "multiprovider",  # MPRO
    "compositeprovider",  # HCPR
    "infoobject",  # IOBJ
    "query_element",  # ELEM
    "other",
]


class TransformationEndpoint(BaseModel):
    """The source or target of a transformation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: EndpointKind
    type_code: str  # raw RSTLOGO code (RSDS/ODSO/ADSO/CUBE/IOBJ/HCPR/TRCS/MPRO/ELEM)
    subtype: str | None = None


class ConstantValue(BaseModel):
    """The literal a ``CONSTANT`` rule writes (RSTRANSTEPCNST).

    Without the value you cannot tell a meaningful business default from a technical zero-fill,
    which is why the rule type alone is not enough.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    internal_type: str | None = None  # ABAP internal type: C/N/D/T/P/I/F
    internal_type_label: str | None = None
    length: int | None = None
    decimals: int | None = None


class FieldMapping(BaseModel):
    """One rule's field-level mapping: which target field(s) come from which source field(s).

    ``aggregation`` is the rule's aggregation behaviour (RSTRANRULE.AGGR, decoded from the ABAP
    dictionary domain). It changes what every key figure in the target *means* — ``MOV`` (direct
    assignment) overwrites the existing value, whereas ``SUM`` adds to it — so it is surfaced
    alongside the mapping rather than left implicit.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: int
    rule_type: RuleType
    target_fields: list[str] = Field(default_factory=list)  # RSTRANFIELD PARAMTYPE='1'
    source_fields: list[str] = Field(default_factory=list)  # RSTRANFIELD PARAMTYPE='0'
    key_fields: list[str] = Field(default_factory=list)  # target fields with RSTRANFIELD.KEYFLAG
    routine_code_id: str | None = None  # set when the rule derives via a routine (-> RSAABAP)
    aggregation: AggregationBehaviour | None = None  # decoded RSTRANRULE.AGGR
    aggregation_code: str | None = None  # raw code, so an undecoded value is still visible
    group_type: RuleGroupType | None = None  # decoded RSTRANRULE.GROUPTYPE
    no_conversion: bool = False  # RSTRANRULE.NO_CONV: conversion routine suppressed
    constant: ConstantValue | None = None  # populated for CONSTANT rules
    provenance: Provenance


class DeclaredLookup(BaseModel):
    """A lookup the transformation *declares* against another object.

    These come from the typed rule-step tables (``RSTRANSTEPMASTER`` for master-data reads,
    ``RSTRANSTEPODSO`` / ``RSTRANSTEPADSO`` for DataStore reads), so unlike the dependencies the
    routine parser infers from ABAP text these are **exact**: BW itself records them. That makes
    them the reliable half of a transformation's read dependencies, and they are labelled
    ``derivation='declared'`` wherever they feed lineage or latency analysis.

    ``miss_behaviour`` is what happens when the lookup finds nothing — erroring the record versus
    substituting a constant — which decides whether a missing master-data row fails the load or
    silently changes the data.
    """

    model_config = ConfigDict(extra="forbid")

    kind: LookupKind
    object_name: str
    rule_id: int | None = None
    step_id: int | None = None
    miss_behaviour: LookupMissBehaviour | None = None
    miss_constant: str | None = None  # value substituted when miss_behaviour == 'constant'
    key_date: LookupKeyDate | None = None  # master-data lookups only
    key_date_field: str | None = None  # RSTRANSTEPMASTER.DATEIOBJNM
    derivation: Literal["declared"] = "declared"
    provenance: Provenance


class RoutineRef(BaseModel):
    """A reference to a routine's ABAP code (without the source itself)."""

    model_config = ConfigDict(extra="forbid")

    kind: RoutineKind
    code_id: str
    rule_id: int | None = None  # set for field/formula/unit routines (RSTRANSTEPROUT)
    provenance: Provenance


class RoutineCode(BaseModel):
    """Full ABAP source for one routine (lines in order)."""

    model_config = ConfigDict(extra="forbid")

    kind: RoutineKind
    code_id: str
    rule_id: int | None = None
    line_count: int = 0
    lines: list[str] = Field(default_factory=list)
    provenance: Provenance


class Transformation(BaseModel):
    """A transformation: endpoints, field-level rule mappings, and routine references."""

    model_config = ConfigDict(extra="forbid")

    tran_id: str
    description: str | None = None
    active: bool = True
    source: TransformationEndpoint | None = None
    target: TransformationEndpoint | None = None
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    routines: list[RoutineRef] = Field(default_factory=list)
    declared_lookups: list[DeclaredLookup] = Field(default_factory=list)
    has_start_routine: bool = False
    has_end_routine: bool = False
    has_expert_routine: bool = False
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance | list[Provenance]


class TransformationSummary(BaseModel):
    """Compact transformation entry for list results."""

    model_config = ConfigDict(extra="forbid")

    tran_id: str
    description: str | None = None
    source_name: str | None = None
    source_kind: EndpointKind | None = None
    target_name: str | None = None
    target_kind: EndpointKind | None = None
    has_routines: bool = False
    provenance: Provenance | list[Provenance]


# --- routine analysis (heuristic, lower-bound) -------------------------------------------

TableAccess = Literal["read", "unknown"]

# Anti-patterns the static parser can flag (mission Scenario 9.6 / routine review).
AntiPatternKind = Literal[
    "select_in_loop",
    "missing_for_all_entries",
    "hardcoded_value",
    "recordset_delete",  # DELETE that alters the result set (DELETE itab / ADJACENT DUPLICATES)
    "database_modification",  # a routine writing to the DB (should never happen; flag if seen)
    "nested_loop",
]

# Kinds of call the parser names but cannot follow (the lineage dead-ends).
CallKind = Literal["function_module", "class_method", "form", "dynamic", "unknown"]


class TableDependency(BaseModel):
    """A table the routine reads, with a best-effort resolution back to a BW object."""

    model_config = ConfigDict(extra="forbid")

    table: str  # physical table as written (e.g. /BIC/A<dso>00, /BI0/..., or a standard table)
    access: TableAccess = "read"
    is_bw_generated: bool = False  # /BIC/ or /BI0/ generated table
    resolved_object: str | None = None  # BW object name the table belongs to
    resolved_kind: str | None = None  # 'dso' | 'adso' | 'infocube' | 'infoobject' | None
    # 'confirmed' when the reading was checked against a catalogue of real object names,
    # 'advisory' when it rests on the naming convention alone. Without this, a checked fact and a
    # name-shaped guess are indistinguishable to the caller.
    resolution_confidence: Literal["confirmed", "advisory"] | None = None
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def _derive_evidence(self) -> TableDependency:
        if self.evidence is None and self.resolution_confidence is not None:
            self.evidence = evidence_for(
                "table_resolution",
                self.resolution_confidence,
                detail=(
                    f"Table {self.table} was decomposed to {self.resolved_object}"
                    + (
                        " and that object was found in the catalogue."
                        if self.resolution_confidence == "confirmed"
                        else ", but no catalogue entry confirmed the object exists."
                    )
                )
                if self.resolved_object
                else None,
            )
        return self


class AntiPattern(BaseModel):
    """A flagged code smell with the line it was seen on."""

    model_config = ConfigDict(extra="forbid")

    kind: AntiPatternKind
    line_no: int | None = None
    detail: str | None = None  # short note (may quote a few tokens of the line)


class UnresolvedRef(BaseModel):
    """A call the static parser named but could not follow (an advisory lineage dead-end)."""

    model_config = ConfigDict(extra="forbid")

    call_kind: CallKind
    object_name: str
    line_no: int | None = None


class ComplexitySignals(BaseModel):
    """Coarse complexity signals from the source."""

    model_config = ConfigDict(extra="forbid")

    line_count: int = 0
    select_count: int = 0
    loop_count: int = 0
    call_count: int = 0
    max_loop_nesting: int = 0


class RoutineAnalysis(BaseModel):
    """Heuristic analysis of one routine's ABAP source. Always a lower bound.

    ``completeness`` is fixed to ``lower_bound``: dynamic SQL, function-module and class-method
    calls are not followed, so the true dependency set can only be larger than what is reported.
    """

    model_config = ConfigDict(extra="forbid")

    code_id: str
    kind: RoutineKind
    table_dependencies: list[TableDependency] = Field(default_factory=list)
    anti_patterns: list[AntiPattern] = Field(default_factory=list)
    unresolved_refs: list[UnresolvedRef] = Field(default_factory=list)
    complexity: ComplexitySignals = Field(default_factory=ComplexitySignals)
    completeness: Literal["lower_bound"] = "lower_bound"
    evidence: Evidence | None = None
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance  # RSAABAP:<code_id>

    @model_validator(mode="after")
    def _derive_evidence(self) -> RoutineAnalysis:
        if self.evidence is None:
            unfollowed = len(self.unresolved_refs)
            self.evidence = evidence_for(
                "routine_analysis",
                self.completeness,
                detail=(
                    "Static parse of this routine's ABAP. "
                    f"{unfollowed} call(s) were named but not followed, so the dependency set can "
                    "only be larger than reported."
                )
                if unfollowed
                else None,
            )
        return self
