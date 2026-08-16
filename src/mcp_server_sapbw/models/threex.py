"""Models for the BW 3.x dataflow layer: transfer rules, update rules, InfoSources.

Scenario 5 of the mission. This is not legacy trivia on a 7.50 system: on the reference system
1,090 DataSources reach BW through a transfer structure with no 7.x transformation anywhere, so
without these objects the documented lineage simply stops at those DataSources.

The 3.x path is ``DataSource -> InfoSource -> transfer structure (transfer rules) -> communication
structure -> update rules -> target``, against the 7.x path's single transformation plus DTP.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance


class RoutineRef(BaseModel):
    """A routine as the ABAP routine registry (``RSAROUT``) describes it.

    ``RSAROUT`` was previously assumed to hold 3.x routine source. It does not - it holds no source
    at all, only a header per routine. The source lives in ``RSAABAP`` keyed on the same code id,
    which is the table the 7.x routines already come from: measured on the reference system, all 164
    transfer-rule conversion routines, all 4 transfer-structure start routines and all 3 update-rule
    routines resolve there. So 3.x routine logic was never unreachable, only unattributed.

    What the registry adds is what kind of routine a code id belongs to and how it uses its input.
    ``kind`` spans both worlds (9,971 transformation routines, 378 transfer-rule, 15 InfoObject
    conversion, 3 update-rule), which is what makes a routine reachable from the flow that owns it.
    """

    model_config = ConfigDict(extra="forbid")

    code_id: str
    # Decoded RSAROUT.CODETP. "unknown" when the dictionary does not document the code, which is
    # reported rather than guessed.
    kind: str = "unknown"
    kind_code: str | None = None
    description: str | None = None
    owner: str | None = None
    # RSAROUT.OBJSTAT/ACTIVFL. 30 routines on the reference system have an active-version row but
    # are not activated, so the flag is reported rather than inferred from the row's existence.
    active: bool = True
    # Decoded RSAROUT.DEPENDENCY: whether the routine reads no source field, selected fields, or the
    # whole source structure. A routine taking the whole structure has a wider change-impact surface
    # than one naming its fields, and BW records the difference.
    source_dependency: str | None = None
    line_count: int = 0
    # False when RSAABAP holds no lines for this code id, which means the logic cannot be read at
    # all - a different statement from a routine that is simply short.
    source_available: bool = False
    provenance: list[Provenance] = Field(default_factory=list)


class TransferRule(BaseModel):
    """One field-level transfer rule: how a transfer-structure field becomes an InfoObject.

    Exactly one mechanism applies per rule, and which one it is decides whether the logic is
    readable from metadata: a fixed value or a plain assignment is fully described here, a formula
    holds its logic in the formula builder, and a conversion routine's ABAP is in ``RSAABAP`` -
    reachable, and now attributed through ``routine``.
    """

    model_config = ConfigDict(extra="forbid")

    transfer_structure: str
    comm_structure: str | None = None
    infoobject: str | None = None  # target InfoObject in the communication structure
    infoobject_ts: str | None = None  # source field in the transfer structure
    fixed_value: str | None = None
    conversion_routine_global: str | None = None
    conversion_routine_local: str | None = None
    formula_id: str | None = None
    conversion: str | None = None
    # The conversion routine's registry entry, when the rule names one. Absent for every other
    # mechanism, and for a routine whose registry row could not be read.
    routine: RoutineRef | None = None
    provenance: Provenance

    @property
    def mechanism(self) -> str:
        """How this rule derives its value, for grouping without re-deriving the logic."""
        if self.conversion_routine_local or self.conversion_routine_global:
            return "routine"
        if self.formula_id:
            return "formula"
        if self.fixed_value:
            return "constant"
        if self.infoobject_ts:
            return "direct_assignment"
        return "unmapped"


class UpdateRule(BaseModel):
    """A 3.x update rule: InfoSource -> target, the step a 7.x transformation replaced."""

    model_config = ConfigDict(extra="forbid")

    update_id: str
    infosource: str | None = None
    target: str | None = None  # RSUPDINFO.INFOCUBE, despite the name also holds InfoObjects
    has_start_routine: bool = False
    expert_mode: bool = False
    object_status: str | None = None
    routine_count: int = 0
    # The registry entries for this rule's routines, so the logic is reachable from the rule rather
    # than only counted. Empty when the rule has none, or when the registry is unavailable.
    routines: list[RoutineRef] = Field(default_factory=list)
    provenance: Provenance


class ThreeXFlow(BaseModel):
    """A DataSource's 3.x route into BW, and whether a 7.x transformation also exists.

    ``has_seven_x_transformation`` is the field that matters for a migration or an upgrade: false
    means this DataSource has no 7.x path at all, so the transfer rules are the live load logic
    rather than a leftover.
    """

    model_config = ConfigDict(extra="forbid")

    datasource: str
    logical_system: str | None = None
    infosource: str | None = None
    transfer_structure: str | None = None
    infosource_type: str | None = None  # RSISOSMAP.ISTYPE
    has_start_routine: bool = False
    rule_count: int = 0
    rules_with_routine: int = 0
    rules_with_formula: int = 0
    rules_with_constant: int = 0
    has_seven_x_transformation: bool = False
    update_rule_targets: list[str] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)


class ThreeXFlowReport(BaseModel):
    """Paginated 3.x flows plus the totals that make the migration state legible at a glance."""

    model_config = ConfigDict(extra="forbid")

    flows: list[ThreeXFlow] = Field(default_factory=list)
    total_count: int = 0
    limit: int = 100
    offset: int = 0
    active_transfer_structures: int = 0
    datasources_with_3x_route: int = 0
    datasources_with_7x_transformation: int = 0
    datasources_3x_only: int = 0
    active_update_rules: int = 0
    caveats: list[str] = Field(default_factory=list)
