"""Portfolio-wide routine register models.

``bw_analyze_routine`` answers "what does this transformation's ABAP do". The register answers the
question you cannot ask one transformation at a time: *across the whole system, which routines carry
the most custom logic and the worst patterns* - the list you work down before an upgrade, or when
deciding where a rewrite pays for itself.

Two properties keep it honest on a large system:

* **Size ranking is complete; deep analysis is budgeted.** Every routine's line count comes from one
  aggregate over the ABAP source table, so ``total_routines`` and ``total_lines`` cover the whole
  portfolio. Parsing is then limited to the largest ``parse_budget`` routines, because parsing means
  fetching source. Entries outside that budget carry ``analyzed=False`` and no pattern counts - not
  zeroes, which would read as "clean".
* **Ranking basis is stated, not implied.** Entries are ordered by anti-pattern count then line
  count, both measured. There is no composite "risk score" with invented weights.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance
from .transformations import RoutineKind


class RoutineRegisterEntry(BaseModel):
    """One routine's place in the portfolio: where it lives, how big, and what it does badly."""

    model_config = ConfigDict(extra="forbid")

    code_id: str
    kind: RoutineKind
    transformation_id: str
    source_name: str | None = None
    target_name: str | None = None
    line_count: int = 0
    # False when the routine fell outside the parse budget: its patterns are unknown, not absent.
    analyzed: bool = False
    select_count: int = 0
    loop_count: int = 0
    max_loop_nesting: int = 0
    unresolved_call_count: int = 0
    # {anti_pattern_kind: occurrences}; empty when analyzed is False.
    anti_pattern_counts: dict[str, int] = Field(default_factory=dict)
    anti_pattern_total: int = 0
    table_reads: list[str] = Field(default_factory=list)
    provenance: Provenance


class RoutineRegister(BaseModel):
    """Every transformation routine in the system, ranked, with analysis depth reported."""

    model_config = ConfigDict(extra="forbid")

    entries: list[RoutineRegisterEntry] = Field(default_factory=list)
    # Portfolio totals, complete regardless of the parse budget.
    total_routines: int = 0
    total_lines: int = 0
    # How many routines were parsed, and the cap that governed it.
    analyzed_count: int = 0
    parse_budget: int = 0
    # Portfolio-wide pattern tally over the analysed subset only.
    anti_pattern_totals: dict[str, int] = Field(default_factory=dict)
    limit: int = 0
    offset: int = 0
    truncated: bool = False
    caveats: list[str] = Field(default_factory=list)


class UnusedProvider(BaseModel):
    """A provider with no maintained consumer found, and what was checked to establish that."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    object_type: str | None = None
    # Each consumer route checked, and whether it found anything.
    feeds_transformation: bool = False
    has_designed_query: bool = False
    is_composite_part: bool = False
    # Ad-hoc queries do not count as maintained consumers but are reported, since a provider used
    # only for ad-hoc navigation is a different conversation from one nothing touches at all.
    ad_hoc_query_count: int = 0
    provenance: Provenance
