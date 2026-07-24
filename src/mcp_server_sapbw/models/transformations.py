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

from pydantic import BaseModel, ConfigDict, Field

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

# Which routine slot the ABAP belongs to.
RoutineKind = Literal["start", "end", "expert", "global", "field", "formula", "unit", "unknown"]

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


class FieldMapping(BaseModel):
    """One rule's field-level mapping: which target field(s) come from which source field(s)."""

    model_config = ConfigDict(extra="forbid")

    rule_id: int
    rule_type: RuleType
    target_fields: list[str] = Field(default_factory=list)  # RSTRANFIELD PARAMTYPE='1'
    source_fields: list[str] = Field(default_factory=list)  # RSTRANFIELD PARAMTYPE='0'
    routine_code_id: str | None = None  # set when the rule derives via a routine (-> RSAABAP)
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
    has_start_routine: bool = False
    has_end_routine: bool = False
    has_expert_routine: bool = False
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
    resolved_object: str | None = None  # heuristic BW object name (e.g. the DSO)
    resolved_kind: str | None = None  # heuristic: 'dso' | 'infoobject' | ... (advisory)


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
    caveats: list[str] = Field(default_factory=list)
    provenance: Provenance  # RSAABAP:<code_id>
