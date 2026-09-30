"""Models for ABAP source read out of a source system over ADT.

BW records *that* a DataSource was enhanced (customer-namespace fields in the extract structure)
but holds none of the enhancement's logic - that lives in the source system's ABAP. These models
describe what an ADT read returns and what static analysis of it establishes.

**Provenance for a non-table source.** :class:`Provenance` describes a metadata row
(``source_table`` + ``source_key``); an ADT fetch is not a row. :class:`AdtProvenance` therefore
records the ADT endpoint path, object name and client instead, and deliberately omits the host so a
tool response can never leak it (mission Rule 5). The embedded :class:`RoutineAnalysis` keeps its
own ``Provenance`` with ``source_table='ADT'`` so the fact is still self-describing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCategory, ErrorCode, derive_failure_fields
from .transformations import RoutineAnalysis

# ADT addresses each object kind under its own path segment.
AdtObjectKind = Literal["include", "program", "function_module"]

# What a BW extractor exit slot serves. Confirmed against SAP documentation of enhancement
# RSAP0001, whose four components are the transaction-data, attribute, text and hierarchy exits.
ExitDataKind = Literal[
    "transaction_data",
    "master_data_attributes",
    "master_data_texts",
    "master_data_hierarchies",
]

# Why an exit slot has no source. "absent" is a finding, not an error: an unimplemented slot means
# no enhancement of that kind exists. The others mean the answer is unknown.
ExitUnavailableReason = Literal["absent", "forbidden", "unauthorized", "fetch_failed"]


class AdtProvenance(BaseModel):
    """Where an ADT-fetched artefact came from. The host is deliberately absent (mission Rule 5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: Literal["ecc_adt"] = "ecc_adt"
    profile: str
    client: str
    adt_path: str
    object_name: str
    object_kind: AdtObjectKind
    fetched_at: datetime


class TableOwner(BaseModel):
    """Which development package a table belongs to, read from the source system (D67).

    **Why a package and not an application component.** Mission §9.6 asks for enhancements reading
    *another team's* data to be flagged, and doing that needs an ownership signal the server can
    read
    rather than infer. ADT's information system returns a package name and description per object in
    one GET; a formal application component is **not** exposed - ``/packages/<name>`` answers 404 on
    the reference system, and the package's own description is as far as it goes. So the package is
    the
    ownership unit used, and its limits are stated rather than glossed:

    * a package is a **development-organisation** unit, which usually tracks functional ownership
    and
      is not guaranteed to;
    * some packages are **shared infrastructure** - change documents, address services - and reading
      one is not reaching into another team's business data;
    * a customer or partner namespace may carry **no package at all** in the search result, which is
      reported as unknown rather than as "no owner".

    Hence ``basis`` for the advisory flag: this is a signal a reviewer acts on, not a verdict.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    #: Development package, e.g. ``VF``. Empty string when the search returned the object with no
    #: package, which happens for customer and partner namespaces.
    package: str | None = None
    #: The package's own short text, so ``VF`` is readable as "Application development R/3 invoice".
    package_description: str | None = None
    #: The table's description, which is often the clearest statement of what was actually read.
    table_description: str | None = None
    #: ADT object type, e.g. ``TABL/DT``. Recorded because the search returns several objects per
    #: name - the table, its maintenance object, documentation data elements - and which one was
    #: chosen decides the package. Taking the first match attributed ``VBRP`` to ``VFW`` (its
    #: maintenance object) instead of ``VF``.
    object_type: str | None = None
    provenance: AdtProvenance | None = None


class ExitBranch(BaseModel):
    """One DataSource's own branch of the exit's ``CASE``, analysed in isolation.

    An exit include serves every enhanced DataSource, so attributing the whole include's table reads
    to one of them would overstate. Risk is therefore measured within the branch that actually runs
    for this DataSource. Where a branch cannot be delimited - a nested ``CASE``, or dispatch by
    ``IF`` rather than ``CASE`` - the branch is reported unresolved rather than approximated.
    """

    model_config = ConfigDict(extra="forbid")

    datasource: str
    resolved: bool
    line_count: int = 0
    table_reads: list[str] = Field(default_factory=list)
    # SELECTs inside a LOOP: read cost scales with extract volume (mission 9.6).
    per_record_selects: int = 0
    anti_pattern_kinds: list[str] = Field(default_factory=list)
    unresolved_call_count: int = 0


class ExitSatellite(BaseModel):
    """A per-DataSource exit program reached by dynamic dispatch from ``ZXRSAU0n``.

    Some sites keep no logic in the exit include at all: it derives a program name from
    ``I_DATASOURCE`` and calls it (``PERFORM ... IN PROGRAM (name)``). ABAP resolves that name at
    runtime, so no static read of the include can follow it - the include looks nearly empty and the
    enhancement's real table reads, ``FOR ALL ENTRIES`` and per-record ``SELECT``s are invisible.

    Each satellite is one program serving exactly one DataSource, so unlike :class:`ExitBranch`
    there is no attribution problem: everything the program does belongs to that DataSource.

    ``available=False`` with reason ``absent`` is the ordinary case and is not a defect - it means
    this DataSource has no satellite, which is a genuine finding. Any other reason means unknown.
    """

    model_config = ConfigDict(extra="forbid")

    program_name: str
    datasource: str
    # The prefix that produced the candidate name, so the naming rule used is auditable.
    prefix: str
    # Which exit slot's dispatch contributed the prefix; None when it came from configuration.
    dispatched_from: ExitDataKind | None = None
    available: bool
    unavailable_reason: ExitUnavailableReason | None = None
    line_count: int = 0
    source: str | None = None
    table_reads: list[str] = Field(default_factory=list)
    per_record_selects: int = 0
    # FOR ALL ENTRIES with no is-not-initial guard: an empty driver table reads the whole table.
    unguarded_for_all_entries: int = 0
    anti_pattern_kinds: list[str] = Field(default_factory=list)
    unresolved_call_count: int = 0
    analysis: RoutineAnalysis | None = None
    provenance: AdtProvenance | None = None
    note: str | None = None


class ExitSource(BaseModel):
    """One extractor-exit slot: its ABAP, the DataSources it dispatches on, and its risk signals."""

    model_config = ConfigDict(extra="forbid")

    exit_function_module: str
    include_name: str
    data_kind: ExitDataKind
    available: bool
    unavailable_reason: ExitUnavailableReason | None = None
    line_count: int = 0
    source: str | None = None
    # DataSources named in the exit's CASE dispatch. Advisory: a dispatch built dynamically, or
    # delegated to a subroutine or class, will not appear here.
    handled_datasources: list[str] = Field(default_factory=list)
    branches: list[ExitBranch] = Field(default_factory=list)
    # True when the include calls a program whose name is computed at runtime. This is the signal
    # that the include's own emptiness proves nothing about the enhancement.
    dynamic_dispatch: bool = False
    # Literal prefixes the include concatenates with I_DATASOURCE to build that program name.
    satellite_prefixes: list[str] = Field(default_factory=list)
    # Whole-include analysis, for the "what does this exit do overall" view.
    analysis: RoutineAnalysis | None = None
    provenance: AdtProvenance | None = None
    note: str | None = None


class ExitInventory(BaseModel):
    """All four extractor-exit slots for one source system, plus any satellite programs found."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    client: str
    exits: list[ExitSource] = Field(default_factory=list)
    available_count: int = 0
    # Union of every DataSource named across the four exits.
    handled_datasources: list[str] = Field(default_factory=list)
    # Per-DataSource satellite programs, present only when candidates were supplied and a naming
    # rule was known. Programs that do not exist are included with reason 'absent', because "no
    # satellite" is a result worth citing and its absence from the list would be ambiguous.
    satellites: list[ExitSatellite] = Field(default_factory=list)
    satellite_prefixes: list[str] = Field(default_factory=list)
    satellites_found_count: int = 0
    satellite_candidates_considered: int = 0
    caveats: list[str] = Field(default_factory=list)


class ConnectorUnavailable(BaseModel):
    """No source-system connector is configured, so exit ABAP cannot be read.

    Distinct from :class:`~..models.provenance.UnsupportedResult`, which means a *BW release* lacks
    a table. Here the metadata exists but in a system this server has not been pointed at.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["connector_not_configured"] = "connector_not_configured"
    code: ErrorCode = "connector_not_configured"
    category: ErrorCategory | None = None
    remedy: str | None = None
    retryable: bool | None = None
    connector: Literal["ecc"] = "ecc"
    configured_profiles: list[str] = Field(default_factory=list)
    detail: str

    @model_validator(mode="after")
    def _derive(self) -> ConnectorUnavailable:
        self.category, self.retryable, self.remedy = derive_failure_fields(
            self.code, self.category, self.retryable, self.remedy
        )
        return self
