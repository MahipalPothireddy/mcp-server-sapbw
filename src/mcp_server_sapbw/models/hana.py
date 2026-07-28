"""Domain models for the HANA layer (B8).

Calc views live in the ``_SYS_BIC`` schema; their dependencies come from
``SYS.OBJECT_DEPENDENCIES`` (DEPENDENCY_TYPE=1 = direct). A calc view is "BW-consuming" when it
reads BW-generated ``/BIC/`` or ``/BI0/`` tables, which resolve back to BW objects by naming
convention (advisory). The BW<->HANA crossing table captures both directions: a calc view reading a
BW table (``hana_reads_bw``) and a BW-layer object reading a calc view (``bw_reads_hana``). Every
fact cites ``SYS.OBJECT_DEPENDENCIES``.

On the BW side of a ``bw_reads_hana`` crossing sits a BW-generated per-InfoProvider view named
``0BW:BIA:<PROVIDER>``; :class:`BwProviderView` carries the provider parsed from that name with its
type confirmed by lookup, which is what turns a raw dependency row into the calc-view ->
CompositeProvider hop.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class BaseTableRef(BaseModel):
    """A base object a calc view reads, with a best-effort resolution to a BW object."""

    model_config = ConfigDict(extra="forbid")

    table: str
    schema_name: str | None = None
    object_type: str | None = None  # HANA BASE_OBJECT_TYPE (TABLE / VIEW / ...)
    is_bw_generated: bool = False
    resolved_object: str | None = None  # heuristic BW object (advisory)
    resolved_kind: str | None = None
    provenance: Provenance


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


class CalcViewLineage(BaseModel):
    """A calc view's direct base tables and the BW providers that consume it.

    ``consuming_bw_providers`` is the BW side of the boundary: the InfoProviders whose generated
    ``0BW:BIA:`` views read this calc view. For a CompositeProvider that is exactly the
    calc-view -> CompositeProvider hop, which BW's own where-used lists do not report.
    """

    model_config = ConfigDict(extra="forbid")

    view_name: str
    schema_name: str = "_SYS_BIC"
    base_tables: list[BaseTableRef] = Field(default_factory=list)
    resolved_bw_objects: list[str] = Field(default_factory=list)
    consuming_bw_providers: list[BwProviderView] = Field(default_factory=list)
    truncated: bool = False
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
    resolution: Literal["bic_table", "bw_provider_view", "unresolved"] = "unresolved"
    object_type: str | None = None  # the HANA object type on the BW side (TABLE/VIEW/SYNONYM)
    provenance: Provenance


class HanaCrossingReport(BaseModel):
    """The bidirectional BW<->HANA crossing table for a scope."""

    model_config = ConfigDict(extra="forbid")

    crossings: list[HanaCrossing] = Field(default_factory=list)
    total_count: int = 0
    hana_reads_bw_count: int = 0
    bw_reads_hana_count: int = 0
    truncated: bool = False
    caveats: list[str] = Field(default_factory=list)
