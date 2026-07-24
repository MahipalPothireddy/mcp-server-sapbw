"""MCP server: FastMCP instance, runtime wiring, and the tool surface (B3).

This is the first MCP-surface code (delivered after the B2 capability-discovery gate). It hosts the
FastMCP instance over stdio, resolves a profile per call, and exposes the system tools plus the
chains vertical slice. Every tool is read-only (``readOnlyHint``), and internal error details are
masked (``mask_error_details``) on top of the connection layer's secret scrubbing.

Conventions enforced here:
- Tool names must match ``^[a-zA-Z][a-zA-Z0-9_]*$`` and be <= 40 chars (validated at registration).
- List tools accept ``limit``/``offset`` and return a ``total_count``.
- Every tool takes ``system: str``; the server stays stateless across calls (connections pooled).
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .core.cache import SqliteCache
from .core.capabilities import CapabilityResolver
from .core.connection import ReadOnlyConnectionPool
from .core.profiles import ProfileManager
from .models.capability import CapabilityRecord
from .models.chains import Chain, ChainRuntimes, ChainSummary, ScheduleMatrixEntry
from .models.hana import CalcView, CalcViewLineage, HanaCrossingReport
from .models.lineage import ImpactAnalysis, LineageDirection, LineageGraph, TraceToSource
from .models.provenance import UnsupportedResult
from .models.providers import ObjectNotFound, Provider, ProviderType, SearchHit
from .models.queries import Query, QueryLineage, QuerySummary, QueryUsage
from .models.transformations import (
    RoutineAnalysis,
    RoutineCode,
    Transformation,
    TransformationSummary,
)
from .repositories.chains import ChainsRepository
from .repositories.hana import HanaRepository
from .repositories.providers import ProvidersRepository
from .repositories.queries import QueriesRepository
from .repositories.search import SearchRepository
from .repositories.transformations import TransformationsRepository
from .services.lineage import LineageService

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_MAX_TOOL_NAME = 40
_MAX_PAGE = 500
_DEFAULT_PAGE = 100

mcp: FastMCP = FastMCP(name="sapbw", mask_error_details=True)


# --- server-surface result models --------------------------------------------------------


class SystemStatus(BaseModel):
    """One configured profile and its discovery status (no host/credentials)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["configured", "discovered"]
    release: str | None = None
    read_only_user: bool = True


class RefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str
    scope: str
    removed: int


class ChainListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ChainSummary] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int


class ScheduleMatrixResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ScheduleMatrixEntry] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SearchHit] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int


class TransformationListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TransformationSummary] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int


class RoutineCodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transformation_id: str
    routines: list[RoutineCode] = Field(default_factory=list)


class RoutineAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transformation_id: str
    analyses: list[RoutineAnalysis] = Field(default_factory=list)


class QueryListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[QuerySummary] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int


class CalcViewListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CalcView] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int


# --- runtime -----------------------------------------------------------------------------


class Runtime(Protocol):
    """The surface the tools depend on (implemented by ServerRuntime; faked in tests)."""

    def list_systems(self) -> list[SystemStatus]: ...
    def capability(self, system: str) -> CapabilityRecord: ...
    def refresh_capabilities(self, system: str) -> CapabilityRecord: ...
    def refresh_cache(self, system: str, scope: str) -> RefreshResult: ...
    def chains(self, system: str) -> ChainsRepository: ...
    def providers(self, system: str) -> ProvidersRepository: ...
    def search(self, system: str) -> SearchRepository: ...
    def transformations(self, system: str) -> TransformationsRepository: ...
    def lineage(self, system: str) -> LineageService: ...
    def queries(self, system: str) -> QueriesRepository: ...
    def hana(self, system: str) -> HanaRepository: ...


class ServerRuntime:
    """Holds the profile manager, connection pool, resolver, and per-profile capability cache."""

    def __init__(
        self,
        profile_manager: ProfileManager,
        pool: ReadOnlyConnectionPool,
        resolver: CapabilityResolver,
        cache_dir: Path = Path("cache"),
    ) -> None:
        self._profiles = profile_manager
        self._pool = pool
        self._resolver = resolver
        self._cache_dir = cache_dir
        self._capabilities: dict[str, CapabilityRecord] = {}

    @classmethod
    def from_env(cls) -> "ServerRuntime":
        return cls(ProfileManager(), ReadOnlyConnectionPool(), CapabilityResolver())

    def _connection(self, system: str) -> object:
        return self._pool.acquire(self._profiles.get(system))

    def capability(self, system: str, *, refresh: bool = False) -> CapabilityRecord:
        record = self._capabilities.get(system)
        if refresh or record is None or record.is_expired():
            record = self._resolver.resolve(self._profiles.get(system), self._connection(system))  # type: ignore[arg-type]
            self._capabilities[system] = record
        return record

    def refresh_capabilities(self, system: str) -> CapabilityRecord:
        return self.capability(system, refresh=True)

    def refresh_cache(self, system: str, scope: str) -> RefreshResult:
        record = self.capability(system)
        cache = SqliteCache(
            self._cache_dir / f"{system}.sqlite",
            system=system,
            fingerprint=record.discovered_at.isoformat(),
        )
        removed = cache.refresh(scope)
        cache.close()
        return RefreshResult(system=system, scope=scope, removed=removed)

    def chains(self, system: str) -> ChainsRepository:
        return ChainsRepository(self._connection(system), self.capability(system))  # type: ignore[arg-type]

    def providers(self, system: str) -> ProvidersRepository:
        return ProvidersRepository(self._connection(system), self.capability(system))

    def search(self, system: str) -> SearchRepository:
        return SearchRepository(self._connection(system), self.capability(system))  # type: ignore[arg-type]

    def transformations(self, system: str) -> TransformationsRepository:
        return TransformationsRepository(self._connection(system), self.capability(system))

    def lineage(self, system: str) -> LineageService:
        return LineageService(self._connection(system), self.capability(system))

    def queries(self, system: str) -> QueriesRepository:
        return QueriesRepository(self._connection(system), self.capability(system))

    def hana(self, system: str) -> HanaRepository:
        return HanaRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
        )

    def list_systems(self) -> list[SystemStatus]:
        result: list[SystemStatus] = []
        for name in self._profiles.names():
            profile = self._profiles.get(name)
            record = self._capabilities.get(name)
            result.append(
                SystemStatus(
                    name=name,
                    status="discovered" if record is not None else "configured",
                    release=record.bw_release if record is not None else None,
                    read_only_user=profile.read_only_user,
                )
            )
        return result


# Single-element holder for the process runtime (avoids a module-level ``global`` rebind).
_runtime_holder: dict[str, Runtime] = {}


def runtime() -> Runtime:
    """Return the process runtime, lazily built from the environment on first use."""
    if "runtime" not in _runtime_holder:
        _runtime_holder["runtime"] = ServerRuntime.from_env()
    return _runtime_holder["runtime"]


def set_runtime(value: Runtime) -> None:
    """Install a runtime (used by tests to inject a fixture-backed runtime)."""
    _runtime_holder["runtime"] = value


# --- tool registration helper ------------------------------------------------------------


def _readonly_tool(func: Callable[..., Any]) -> Any:
    """Register a read-only tool, asserting the MCP naming constraint at registration time."""
    name = getattr(func, "__name__", "")
    if not _TOOL_NAME_RE.match(name) or len(name) > _MAX_TOOL_NAME:
        raise ValueError(f"tool name {name!r} violates MCP naming (^[a-zA-Z][a-zA-Z0-9_]*$, <=40)")
    return mcp.tool(annotations={"readOnlyHint": True})(func)


def _clamp_page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, _MAX_PAGE)), max(0, offset)


# --- system tools ------------------------------------------------------------------------


@_readonly_tool
def bw_list_systems() -> list[SystemStatus]:
    """List configured connection profiles and their discovery status."""
    return runtime().list_systems()


@_readonly_tool
def bw_system_profile(system: str) -> CapabilityRecord:
    """Release, ABAP schema, object-model variants, log window, and table presence/counts."""
    return runtime().capability(system)


@_readonly_tool
def bw_refresh_capabilities(system: str) -> CapabilityRecord:
    """Re-run capability discovery for a system, replacing the cached record."""
    return runtime().refresh_capabilities(system)


@_readonly_tool
def bw_refresh_cache(system: str, scope: str = "all") -> RefreshResult:
    """Invalidate cached extracts for a system by scope (all | <object_type> | <object_id>)."""
    return runtime().refresh_cache(system, scope)


# --- chain tools -------------------------------------------------------------------------


@_readonly_tool
def bw_list_chains(
    system: str,
    name_pattern: str | None = None,
    active_only: bool = True,
    limit: int = _DEFAULT_PAGE,
    offset: int = 0,
) -> ChainListResult | UnsupportedResult:
    """Process chains filtered by name pattern / active status, paginated with a total_count."""
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .chains(system)
        .list_chains(name_pattern=name_pattern, active_only=active_only, limit=limit, offset=offset)
    )
    if isinstance(result, UnsupportedResult):
        return result
    items, total = result
    return ChainListResult(items=items, total_count=total, limit=limit, offset=offset)


@_readonly_tool
def bw_get_chain(
    system: str, chain_id: str, resolve_subchains: bool = True
) -> Chain | UnsupportedResult:
    """Chain structure: processes, event-linked edges, and nested sub-chains (resolved)."""
    return runtime().chains(system).get_chain(chain_id, resolve_subchains=resolve_subchains)


@_readonly_tool
def bw_get_chain_runtimes(
    system: str, chain_id: str, days: int = 90
) -> ChainRuntimes | UnsupportedResult:
    """Runtime statistics (min/median/mean/p95/max, success rate, bottlenecks) over the window."""
    return runtime().chains(system).get_chain_runtimes(chain_id, days=days)


@_readonly_tool
def bw_get_schedule_matrix(
    system: str, active_only: bool = True, limit: int = _DEFAULT_PAGE, offset: int = 0
) -> ScheduleMatrixResult | UnsupportedResult:
    """All chains x frequency x typical start x p95 completion, paginated with a total_count."""
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .chains(system)
        .get_schedule_matrix(active_only=active_only, limit=limit, offset=offset)
    )
    if isinstance(result, UnsupportedResult):
        return result
    items, total = result
    return ScheduleMatrixResult(items=items, total_count=total, limit=limit, offset=offset)


# --- object / provider tools -------------------------------------------------------------


@_readonly_tool
def bw_describe_object(
    system: str, name: str, object_type: ProviderType | None = None
) -> Provider | ObjectNotFound | UnsupportedResult:
    """Universal deep-dive for any provider/InfoObject: definition, fields, parts, description.

    Auto-detects the object type when ``object_type`` is omitted. The description is labelled
    stored vs generated (origin) with a quality flag. ``ObjectNotFound`` when the name matches no
    object; ``UnsupportedResult`` when the requested type's tables are absent on this release.
    """
    return runtime().providers(system).describe(name, object_type)


@_readonly_tool
def bw_search_objects(
    system: str,
    pattern: str,
    object_types: list[str] | None = None,
    *,
    match_descriptions: bool = True,
    limit: int = _DEFAULT_PAGE,
    offset: int = 0,
) -> SearchResult:
    """Fuzzy search by technical name or description across chains, providers, and InfoObjects."""
    limit, offset = _clamp_page(limit, offset)
    items, total = (
        runtime()
        .search(system)
        .search(
            pattern,
            object_types=object_types,
            match_descriptions=match_descriptions,
            limit=limit,
            offset=offset,
        )
    )
    return SearchResult(items=items, total_count=total, limit=limit, offset=offset)


# --- transformation / routine tools ------------------------------------------------------


@_readonly_tool
def bw_list_transformations(
    system: str,
    source_name: str | None = None,
    target_name: str | None = None,
    *,
    with_routines_only: bool = False,
    limit: int = _DEFAULT_PAGE,
    offset: int = 0,
) -> TransformationListResult | UnsupportedResult:
    """Transformations filtered by source/target name or routine presence, paginated."""
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .transformations(system)
        .list_transformations(
            source_name=source_name,
            target_name=target_name,
            with_routines_only=with_routines_only,
            limit=limit,
            offset=offset,
        )
    )
    if isinstance(result, UnsupportedResult):
        return result
    items, total = result
    return TransformationListResult(items=items, total_count=total, limit=limit, offset=offset)


@_readonly_tool
def bw_get_transformation(
    system: str, transformation_id: str
) -> Transformation | UnsupportedResult:
    """Transformation header, field-level rule mappings, and routine references."""
    return runtime().transformations(system).get_transformation(transformation_id)


@_readonly_tool
def bw_get_routine_code(
    system: str, transformation_id: str
) -> RoutineCodeResult | UnsupportedResult:
    """Full ABAP source for a transformation's start/end/expert/global and field routines."""
    result = runtime().transformations(system).get_routine_code(transformation_id)
    if isinstance(result, UnsupportedResult):
        return result
    return RoutineCodeResult(transformation_id=transformation_id, routines=result)


@_readonly_tool
def bw_analyze_routine(
    system: str, transformation_id: str
) -> RoutineAnalysisResult | UnsupportedResult:
    """Heuristic (lower-bound) analysis of each routine: table deps, anti-patterns, complexity."""
    result = runtime().transformations(system).analyze_routines(transformation_id)
    if isinstance(result, UnsupportedResult):
        return result
    return RoutineAnalysisResult(transformation_id=transformation_id, analyses=result)


# --- lineage tools -----------------------------------------------------------------------


@_readonly_tool
def bw_get_lineage(
    system: str, name: str, direction: LineageDirection = "both", depth: int = 3
) -> LineageGraph | UnsupportedResult:
    """Directed lineage graph around an object (upstream/downstream/both) to a depth.

    Nodes + edges JSON, including advisory routine-derived edges (a target's routines' reads). Large
    graphs are truncated at a node cap with ``truncated=true``.
    """
    return runtime().lineage(system).get_lineage(name, direction=direction, depth=depth)


@_readonly_tool
def bw_impact_analysis(
    system: str, name: str, depth: int = 3
) -> ImpactAnalysis | UnsupportedResult:
    """Downstream blast radius of a change, including objects whose routines read the target.

    The routine-embedded consumers are dependencies invisible to BW's own where-used lists; they are
    advisory (heuristic lower bound).
    """
    return runtime().lineage(system).impact_analysis(name, depth=depth)


@_readonly_tool
def bw_trace_to_source(system: str, name: str, depth: int = 8) -> TraceToSource | UnsupportedResult:
    """Trace an object upstream, hop by hop, to the originating DataSource boundary."""
    return runtime().lineage(system).trace_to_source(name, depth=depth)


# --- BEx query tools ---------------------------------------------------------------------


@_readonly_tool
def bw_list_queries(
    system: str,
    provider: str | None = None,
    owner: str | None = None,
    limit: int = _DEFAULT_PAGE,
    offset: int = 0,
) -> QueryListResult | UnsupportedResult:
    """BEx queries filtered by provider or owner, paginated with a total_count."""
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .queries(system)
        .list_queries(provider=provider, owner=owner, limit=limit, offset=offset)
    )
    if isinstance(result, UnsupportedResult):
        return result
    items, total = result
    return QueryListResult(items=items, total_count=total, limit=limit, offset=offset)


@_readonly_tool
def bw_get_query(system: str, query: str) -> Query | UnsupportedResult:
    """Full query definition: description, element tree, restrictions, and variables.

    ``query`` may be the technical name (COMPID) or the COMPUID. The description comes from the
    RSZELTTXT/COMPUID join. Customer-exit variables are flagged (their values resolve in ABAP).
    """
    return runtime().queries(system).get_query(query)


@_readonly_tool
def bw_get_query_lineage(system: str, query: str) -> QueryLineage | UnsupportedResult:
    """Field-level lineage: each InfoObject in the query traced toward its DataSource.

    Object-level (InfoObject -> provider -> upstream trace); routine hops are advisory.
    Customer-exit variables are reported as lineage dead ends.
    """
    return runtime().queries(system).get_query_lineage(query)


@_readonly_tool
def bw_get_query_usage(
    system: str, query: str, stale_days: int = 365
) -> QueryUsage | UnsupportedResult:
    """Query usage from RSZCOMPDIR.LASTUSED, flagging decommission candidates (unused/stale)."""
    return runtime().queries(system).get_query_usage(query, stale_days=stale_days)


# --- HANA-layer tools --------------------------------------------------------------------


@_readonly_tool
def bw_list_calc_views(
    system: str, bw_consuming_only: bool = False, limit: int = _DEFAULT_PAGE, offset: int = 0
) -> CalcViewListResult | UnsupportedResult:
    """Calc views (_SYS_BIC), optionally only those reading BW /BIC/ tables, paginated."""
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .hana(system)
        .list_calc_views(bw_consuming_only=bw_consuming_only, limit=limit, offset=offset)
    )
    if isinstance(result, UnsupportedResult):
        return result
    items, total = result
    return CalcViewListResult(items=items, total_count=total, limit=limit, offset=offset)


@_readonly_tool
def bw_get_calc_view_lineage(system: str, view_name: str) -> CalcViewLineage | UnsupportedResult:
    """A calc view's direct base tables (SYS.OBJECT_DEPENDENCIES), resolved to BW objects."""
    return runtime().hana(system).get_calc_view_lineage(view_name)


@_readonly_tool
def bw_get_hana_crossings(
    system: str, calc_view: str | None = None, limit: int = _DEFAULT_PAGE, offset: int = 0
) -> HanaCrossingReport | UnsupportedResult:
    """Every BW<->HANA boundary crossing, both directions (calc-view<->BW-object)."""
    limit, offset = _clamp_page(limit, offset)
    return (
        runtime().hana(system).get_hana_crossings(calc_view=calc_view, limit=limit, offset=offset)
    )


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
