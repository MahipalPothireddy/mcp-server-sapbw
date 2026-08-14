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

import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image
from pydantic import BaseModel, ConfigDict, Field

from .connectors.base import ConnectorRegistry
from .connectors.ecc import EccConnector
from .core.cache import SqliteCache
from .core.capabilities import CapabilityResolver
from .core.connection import ReadOnlyConnectionPool
from .core.profiles import ProfileManager
from .models.capability import CapabilityRecord
from .models.chains import (
    Chain,
    ChainRuntimes,
    ChainSummary,
    LoadClosure,
    ScheduleMatrixEntry,
)
from .models.diagram import DiagramFormat, DiagramResult
from .models.ecc import ConnectorUnavailable, ExitInventory
from .models.findings import ScenarioReport
from .models.hana import CalcView, CalcViewLineage, HanaCrossingReport
from .models.health import ProviderHealth
from .models.lineage import ImpactAnalysis, LineageDirection, LineageGraph, TraceToSource
from .models.provenance import UnsupportedResult
from .models.providers import ObjectNotFound, Provider, ProviderType, SearchHit
from .models.queries import Query, QueryLineage, QueryOriginFilter, QuerySummary, QueryUsage
from .models.register import RoutineRegister
from .models.sources import EnhancementInventory, SourceTopology
from .models.threex import ThreeXFlowReport, TransferRule, UpdateRule
from .models.transformations import (
    RoutineAnalysis,
    RoutineCode,
    Transformation,
    TransformationSummary,
)
from .prompts.workflows import register_prompts
from .repositories.chains import ChainsRepository
from .repositories.hana import HanaRepository
from .repositories.health import HealthRepository
from .repositories.providers import ProvidersRepository
from .repositories.queries import QueriesRepository
from .repositories.search import SearchRepository
from .repositories.sources import SourcesRepository
from .repositories.threex import ThreeXRepository
from .repositories.transformations import TransformationsRepository
from .services.analyzers import Analyzers
from .services.diagram import build_layout, png_available, render_png, render_svg
from .services.docgen import DocGenerator, DocGenResult
from .services.exit_analysis import ExitAnalysisService
from .services.lineage import LineageService
from .services.load_closure import LoadClosureService
from .services.routine_register import RoutineRegisterService

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

# A diagram deeper than this stops being readable, whatever the lineage service would return.
_MAX_DIAGRAM_DEPTH = 8


def _slug_for_file(name: str) -> str:
    """Filesystem-safe stem for a written diagram file."""
    stem = "".join(c if c.isalnum() else "_" for c in name).strip("_")
    return stem or "object"


_MAX_TOOL_NAME = 40
_MAX_PAGE = 500
_DEFAULT_PAGE = 100
# Doc generation writes pages to disk and returns only a manifest of filenames, so the row bound
# that protects the reply size on the list tools does not apply. Bounded only to stop a runaway.
_MAX_DOCGEN_PAGES = 5000

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
    def analyzers(self, system: str) -> Analyzers: ...
    def docgen(self, system: str) -> DocGenerator: ...
    def load_closure(self, system: str) -> LoadClosureService: ...
    def health(self, system: str) -> HealthRepository: ...
    def sources(self, system: str) -> SourcesRepository: ...
    def threex(self, system: str) -> ThreeXRepository: ...
    def routine_register(self, system: str) -> RoutineRegisterService: ...
    def exit_analysis(
        self, ecc_system: str | None
    ) -> ExitAnalysisService | ConnectorUnavailable: ...


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
        # system -> (capability fingerprint, open cache). Retired when the fingerprint changes.
        self._caches: dict[str, tuple[str, SqliteCache]] = {}

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

    def _cache(self, system: str) -> SqliteCache | None:
        """The per-profile metadata cache, tied to the current capability fingerprint.

        One SQLite connection is kept per system for the process's lifetime. A capability refresh
        changes the fingerprint, which retires the instance so entries extracted under the old
        release picture are not reused. A cache that cannot be opened (read-only filesystem, locked
        file) is never fatal: the repositories simply run uncached.
        """
        record = self.capability(system)
        fingerprint = record.discovered_at.isoformat()
        existing = self._caches.get(system)
        if existing is not None:
            if existing[0] == fingerprint:
                return existing[1]
            # Drop the reference but do NOT close it: a repository built moments ago may still hold
            # this instance, and closing the connection under it would raise mid-call. The retired
            # entries are unreachable anyway — they carry the old fingerprint, so they always miss.
            del self._caches[system]
        try:
            cache = SqliteCache(
                self._cache_dir / f"{system}.sqlite",
                system=system,
                fingerprint=fingerprint,
            )
        except (sqlite3.Error, OSError):
            return None
        self._caches[system] = (fingerprint, cache)
        return cache

    def refresh_cache(self, system: str, scope: str) -> RefreshResult:
        cache = self._cache(system)
        removed = cache.refresh(scope) if cache is not None else 0
        return RefreshResult(system=system, scope=scope, removed=removed)

    def chains(self, system: str) -> ChainsRepository:
        return ChainsRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
            self._cache(system),
        )

    def providers(self, system: str) -> ProvidersRepository:
        return ProvidersRepository(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def search(self, system: str) -> SearchRepository:
        return SearchRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
            self._cache(system),
        )

    def transformations(self, system: str) -> TransformationsRepository:
        return TransformationsRepository(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def lineage(self, system: str) -> LineageService:
        return LineageService(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def queries(self, system: str) -> QueriesRepository:
        return QueriesRepository(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def hana(self, system: str) -> HanaRepository:
        return HanaRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
            self._cache(system),
        )

    def _ecc_connector(self, name: str | None = None) -> EccConnector:
        """Build the ECC connector for a named profile, or the sole one if only one is configured.

        With several ECC profiles and no name, no connector is returned: which source system feeds a
        given BW system is not derivable from the profiles file, and picking one would be a guess.
        """
        names = self._profiles.ecc_names()
        chosen = name or (names[0] if len(names) == 1 else None)
        if chosen is None:
            return EccConnector()
        return EccConnector(self._profiles.get_ecc(chosen))

    def _registry(self) -> ConnectorRegistry:
        """Connector registry for the analyzers.

        The ECC connector appears only when an ``ecc_systems`` profile is configured; otherwise the
        connector-gated scenarios (9.6/9.7/9.8) report "not configured" rather than guessing.
        Tableau/BOBJ connectors remain deferred.
        """
        connector = self._ecc_connector()
        return ConnectorRegistry([connector] if connector.is_configured() else [])

    def analyzers(self, system: str) -> Analyzers:
        return Analyzers(
            self._connection(system),
            self.capability(system),
            self._cache(system),
            registry=self._registry(),
        )

    def docgen(self, system: str) -> DocGenerator:
        return DocGenerator(
            self._connection(system),
            self.capability(system),
            self._cache(system),
            registry=self._registry(),
        )

    def exit_analysis(self, ecc_system: str | None) -> ExitAnalysisService | ConnectorUnavailable:
        names = self._profiles.ecc_names()
        connector = self._ecc_connector(ecc_system)
        if not connector.is_configured():
            detail = (
                "no ABAP source system is configured; add an 'ecc_systems' entry to profiles.yaml "
                "to read extractor-exit ABAP over ADT"
                if not names
                else (
                    f"{len(names)} source systems are configured; name one in 'ecc_system' "
                    "(which source system feeds a BW system is not recorded in the profiles file)"
                )
            )
            return ConnectorUnavailable(configured_profiles=names, detail=detail)
        return ExitAnalysisService(connector)

    def load_closure(self, system: str) -> LoadClosureService:
        return LoadClosureService(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def health(self, system: str) -> HealthRepository:
        return HealthRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
            self._cache(system),
        )

    def sources(self, system: str) -> SourcesRepository:
        return SourcesRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
            self._cache(system),
        )

    def threex(self, system: str) -> ThreeXRepository:
        return ThreeXRepository(
            self._connection(system),  # type: ignore[arg-type]
            self.capability(system),
            self._cache(system),
        )

    def routine_register(self, system: str) -> RoutineRegisterService:
        return RoutineRegisterService(
            self._connection(system), self.capability(system), self._cache(system)
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
    """Invalidate cached extracts for a system by scope, returning how many entries were removed.

    ``scope`` is ``all``, one object type, or a single object id. Cached object types:
    ``transformation``, ``routine_code``, ``routine_analysis``, ``query``, ``query_lineage``,
    ``provider``, ``chain``, ``chain_runtimes``, ``calc_view``.

    Only needed after a transport: structural extracts carry a 24h TTL, runtime statistics one hour,
    and everything is dropped automatically when capability discovery re-runs.
    """
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
    """Process chains filtered by name pattern / active status, paginated with a total_count.

    ``name_pattern`` is a substring match on the chain id, case-insensitive, with ``_`` treated
    literally. Include ``%`` to author the wildcards yourself (e.g. ``LOAD%`` to anchor the start).
    """
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
    """Fuzzy search by technical name or description across chains, providers, and InfoObjects.

    ``pattern`` is a substring match, case-insensitive, with ``_`` treated literally — so a partial
    BW name like ``SD_O3`` works. Include ``%`` to author the wildcards yourself.
    """
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
    """Transformations filtered by source/target name or routine presence, paginated.

    ``source_name`` / ``target_name`` are substring matches (``_`` literal). This is deliberate: a
    DataSource endpoint is stored as ``<DATASOURCE><padding><LOGSYS>``, so passing just the
    DataSource name works. Include ``%`` to author the wildcards yourself.
    """
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
    *,
    origin: QueryOriginFilter = "all",
    limit: int = _DEFAULT_PAGE,
    offset: int = 0,
) -> QueryListResult | UnsupportedResult:
    """BEx queries filtered by provider, owner, or origin, paginated with a total_count.

    Every entry carries ``origin``. ``designed`` means authored in Query Designer - a maintained
    report. ``ad_hoc`` means the technical name is prefixed ``!!``, which SAP generates for a query
    created straight in the BEx Analyzer; it is a navigation artefact rather than a report, so
    ``origin="designed"`` is the filter to use when counting real reports. That reading comes from
    the name's shape, since BW stores no flag for it.
    """
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .queries(system)
        .list_queries(provider=provider, owner=owner, origin=origin, limit=limit, offset=offset)
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
    """A calc view's base tables and the InfoProviders consuming it, resolved to BW objects.

    Base tables come from SYS.OBJECT_DEPENDENCIES with ``/BIC/`` names resolved (advisory).
    ``consuming_bw_providers`` reads BW's generated ``0BW:BIA:<PROVIDER>`` views, giving the
    calc-view -> CompositeProvider hop that BW's own where-used lists omit.
    """
    return runtime().hana(system).get_calc_view_lineage(view_name)


@_readonly_tool
def bw_get_hana_crossings(
    system: str, calc_view: str | None = None, limit: int = _DEFAULT_PAGE, offset: int = 0
) -> HanaCrossingReport | UnsupportedResult:
    """Every BW<->HANA boundary crossing, both directions (calc-view<->BW-object).

    Each crossing says how its BW side was resolved: ``bic_table`` (from a ``/BIC/`` name,
    advisory), ``bw_provider_view`` (a ``0BW:BIA:`` view parsed to its InfoProvider and
    type-confirmed), or ``unresolved``.
    """
    limit, offset = _clamp_page(limit, offset)
    return (
        runtime().hana(system).get_hana_crossings(calc_view=calc_view, limit=limit, offset=offset)
    )


@_readonly_tool
def bw_get_source_systems(system: str) -> SourceTopology | UnsupportedResult:
    """Which systems feed this BW system, and of what kind.

    Built from the logical systems the DataSources actually extract from, compared against the
    source-system registry — so it also surfaces logical systems that DataSources reference but the
    registry does not know, the usual signature of a system copy where BDLS was not run. System
    kinds decoded from the ABAP dictionary are marked as such; codes the dictionary does not
    document carry a conventional reading labelled advisory.
    """
    return runtime().sources(system).get_topology()


# --- BW 3.x dataflow tools (scenario 5) --------------------------------------------------


@_readonly_tool
def bw_list_3x_flows(
    system: str,
    datasource: str | None = None,
    only_without_transformation: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> ThreeXFlowReport | UnsupportedResult:
    """DataSources that reach BW through a BW 3.x transfer structure, with their rule profile.

    The 3.x path is ``DataSource -> InfoSource -> transfer structure (transfer rules) ->
    communication structure -> update rules -> target``, against the 7.x path's single
    transformation plus DTP. On a 7.50 system this is not legacy trivia: where a DataSource has no
    7.x transformation, its transfer rules *are* the live load logic, and lineage that ignores them
    stops dead at that DataSource.

    ``has_seven_x_transformation`` is the field to read first. False means there is no 7.x path at
    all. Set ``only_without_transformation`` to list just those. Transfer structures carrying no
    rules are excluded, since a rule-less structure is a PSA shell rather than a dataflow.

    Per flow the rule profile counts how the fields are derived — routine, formula, constant — so a
    flow whose logic lives outside metadata is visible without reading every rule.
    """
    limit, offset = _clamp_page(limit, offset)
    return (
        runtime()
        .threex(system)
        .list_flows(
            datasource=datasource,
            only_without_transformation=only_without_transformation,
            limit=limit,
            offset=offset,
        )
    )


@_readonly_tool
def bw_get_transfer_rules(
    system: str, transfer_structure: str
) -> list[TransferRule] | UnsupportedResult:
    """Field-level BW 3.x transfer rules for one transfer structure.

    Each rule says how a transfer-structure field becomes an InfoObject. A constant or a direct
    assignment is fully described by the rule itself; a conversion routine (``CONVROUT_G`` global /
    ``CONVROUT_L`` local) or a formula holds its logic elsewhere, so those are reported as the
    mechanism rather than as resolved logic.
    """
    return runtime().threex(system).get_transfer_rules(transfer_structure)


@_readonly_tool
def bw_list_update_rules(system: str, limit: int = 100) -> list[UpdateRule] | UnsupportedResult:
    """Active BW 3.x update rules: InfoSource -> target, the step a 7.x transformation replaced.

    ``target`` comes from ``RSUPDINFO.INFOCUBE``, which despite the column name also carries
    InfoObject targets for master-data flows.
    """
    limit, _ = _clamp_page(limit, 0)
    return runtime().threex(system).list_update_rules(limit=limit)


@_readonly_tool
def bw_list_extractor_enhancements(
    system: str, limit: int = 50
) -> EnhancementInventory | UnsupportedResult:
    """DataSources whose extract structure carries customer-namespace (appended) fields.

    The fields are metadata-confirmed evidence that an enhancement exists, joined to the delta
    method, extractor program and extraction method. What the exit code actually does lives in the
    source system's ABAP and needs a connector — the inventory says so rather than guessing, and it
    does not infer risk from DataSource naming.
    """
    limit, _ = _clamp_page(limit, 0)
    return runtime().sources(system).enhancement_inventory(limit=limit)


@_readonly_tool
def bw_get_extractor_exit_code(
    ecc_system: str | None = None, include_source: bool = False
) -> ExitInventory | ConnectorUnavailable:
    """The ABAP behind extractor enhancements, read from the source system over ADT.

    BW proves *that* a DataSource was enhanced but holds none of the logic. This reads the four
    extractor-exit slots of enhancement RSAP0001 from the source system and reports, per slot, the
    DataSources its CASE dispatches on, the tables it reads, and anti-patterns including per-record
    SELECTs — the questions scenario 9.6 asks and BW cannot answer. A slot whose include does not
    exist is reported as absent, which means no enhancement of that DataSource kind is implemented;
    a slot that could not be read is reported as unknown rather than absent.

    The connection is GET-only and takes no ADT locks. ``ecc_system`` names an ``ecc_systems``
    profile and may be omitted when exactly one is configured. Full ABAP is opt-in via
    ``include_source`` and is capped; the analysis always covers the whole include.
    """
    service = runtime().exit_analysis(ecc_system)
    if isinstance(service, ConnectorUnavailable):
        return service
    return service.inventory(include_source=include_source)


@_readonly_tool
def bw_get_provider_health(
    system: str, provider: str, object_type: str | None = None
) -> ProviderHealth | UnsupportedResult:
    """How much data a provider holds and how current it is.

    Volume comes from the HANA monitoring view per generated table, with active, inbound (activation
    queue) and changelog rows reported separately — summing them would hide changelog bloat.
    Currency comes from BW's per-provider request ledger: when it last loaded, whether that load
    succeeded, how many records arrived, and in which update mode. Data age is measured against the
    latest request in the system rather than today, so a restored copy is not read as stale.
    A provider whose generated tables exist but hold nothing is reported as unloaded, not as empty.
    """
    repo = runtime().health(system)
    unsupported = repo.require_health()
    if unsupported is not None:
        return unsupported
    return repo.get_health(provider, object_type)


@_readonly_tool
def bw_get_load_closure(
    system: str, chain_id: str | None = None, provider: str | None = None
) -> LoadClosure | UnsupportedResult:
    """Resolve what a chain loads, or which chains load a provider (with observed cadence).

    Pass exactly one of ``chain_id`` or ``provider``. A chain's loads are walked recursively through
    its nested sub-chains, since most loads live there rather than in the top-level step list.
    Provider lookups return each loading chain's observed cadence — including parent chains, whose
    schedule is what actually governs the load — so "when is this object's data current?" is
    answerable in one call.
    """
    named = [value for value in (chain_id, provider) if value]
    if len(named) != 1:
        return UnsupportedResult(
            missing=[],
            release=runtime().capability(system).bw_release,
            detail="pass exactly one of chain_id or provider",
        )
    service = runtime().load_closure(system)
    if chain_id:
        return service.chain_to_providers(chain_id)
    return service.provider_to_chains(str(provider))


# --- diagram rendering -------------------------------------------------------------------


@_readonly_tool
def bw_render_lineage(
    system: str,
    name: str,
    *,
    direction: LineageDirection = "both",
    depth: int = 4,
    image_format: DiagramFormat = "png",
    output_dir: str | None = None,
) -> ToolResult:
    """Render an object's data flow as an image (PNG by default, or SVG).

    Nodes are laid out left-to-right by dependency depth and colour-coded by BW object type;
    advisory edges (routine-derived, or resolved by naming convention) are dashed so a heuristic
    never looks like a declared fact, and a truncated graph says so on the canvas. Rendering is
    entirely local — diagram content never leaves the machine. Returns the image for inline display
    plus structured metadata; pass ``output_dir`` to also write a vector (SVG) copy.
    """
    depth = max(1, min(depth, _MAX_DIAGRAM_DEPTH))
    graph = runtime().lineage(system).get_lineage(name, direction=direction, depth=depth)
    if isinstance(graph, UnsupportedResult):
        return ToolResult(structured_content=graph.model_dump())

    subtitle = f"{direction}, depth {depth} — generated read-only from BW metadata"
    layout = build_layout(graph, subtitle=subtitle)
    svg = render_svg(layout)
    advisory = sum(1 for e in graph.edges if e.confidence == "advisory")
    caveats = list(graph.caveats)

    svg_path: str | None = None
    if output_dir:
        target = Path(output_dir).expanduser() / f"lineage-{_slug_for_file(name)}.svg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(svg, encoding="utf-8")
        svg_path = str(target)

    png_bytes = render_png(layout) if image_format == "png" else None
    if image_format == "png" and png_bytes is None:
        caveats.append(
            "PNG rendering needs the optional 'viz' extra (pillow); returned SVG markup instead"
        )
    result = DiagramResult(
        root=graph.root_id,
        direction=direction,
        depth=depth,
        image_format="png" if png_bytes else "svg",
        node_count=graph.node_count,
        edge_count=graph.edge_count,
        layer_count=layout.layer_count,
        advisory_edge_count=advisory,
        width=layout.width,
        height=layout.height,
        truncated=graph.truncated,
        svg_path=svg_path,
        png_available=png_available(),
        caveats=caveats,
    )
    content: list[Any] = (
        [Image(data=png_bytes, format="png")] if png_bytes else [svg]  # SVG travels as text
    )
    return ToolResult(content=content, structured_content=result.model_dump())


# --- risk-analyzer tools (mission Section 9) ---------------------------------------------


@_readonly_tool
def bw_check_load_latency(system: str, limit: int = 25) -> ScenarioReport | UnsupportedResult:
    """Scenario 9.1: full-update loads whose routines look up other objects (stale-data risk).

    Only loads with at least one resolvable lookup are findings; candidates whose routine reads did
    not resolve carry no checkable latency contract and are counted in the caveats instead.
    """
    limit, _ = _clamp_page(limit, 0)
    return runtime().analyzers(system).check_load_latency(limit=limit)


@_readonly_tool
def bw_check_schedule_risk(
    system: str, limit: int = _DEFAULT_PAGE
) -> ScenarioReport | UnsupportedResult:
    """Scenario 9.7: report schedules vs. feeding-chain p95 completion (needs a BI connector)."""
    limit, _ = _clamp_page(limit, 0)
    return runtime().analyzers(system).schedule_risk(limit=limit)


@_readonly_tool
def bw_find_layer_violations(
    system: str, max_dso_depth: int = 3, limit: int = _DEFAULT_PAGE
) -> ScenarioReport | UnsupportedResult:
    """Structural anti-patterns: CP->DSO, CP->InfoObject, deep DSO stacks, and write-back loops.

    Write-back loops are the severe ones: a transformation whose source and target are the same
    object, or two objects that each feed the other. Both make the loaded result depend on load
    order, so a failed request cannot simply be re-run, and a cycle has no correct order at all.
    """
    limit, _ = _clamp_page(limit, 0)
    return (
        runtime()
        .analyzers(system)
        .find_layer_violations(limit=limit, max_dso_depth=max(1, max_dso_depth))
    )


@_readonly_tool
def bw_find_unused_providers(
    system: str, limit: int = _DEFAULT_PAGE
) -> ScenarioReport | UnsupportedResult:
    """Providers nothing maintained depends on - decommission candidates, stated as candidates.

    A provider is reported only when three consumer routes all come up empty: it feeds no
    transformation, no Query-Designer query reads it, and it is no CompositeProvider part (a
    CompositeProvider consumes its parts through a generated calc view, not a transformation, so
    ignoring that route would flag every DSO beneath one). Ad-hoc ``!!`` queries are counted and
    reported but do not qualify as maintained consumers.

    Consumption from outside BW is not covered here - check ``bw_get_hana_crossings`` before acting.
    """
    limit, _ = _clamp_page(limit, 0)
    return runtime().analyzers(system).find_unused_providers(limit=limit)


@_readonly_tool
def bw_get_routine_register(
    system: str, limit: int = 50, offset: int = 0, parse_budget: int = 100
) -> RoutineRegister | UnsupportedResult:
    """Every transformation routine in the system, ranked by anti-pattern count then size.

    The portfolio view that per-transformation analysis cannot give: the list you work down before
    an upgrade, or when deciding where a rewrite pays for itself. Line counts and totals cover every
    routine; pattern detection covers the largest ``parse_budget`` routines, because parsing means
    reading source. An entry with ``analyzed=false`` has unknown patterns, not none.
    """
    limit, offset = _clamp_page(limit, offset)
    return (
        runtime()
        .routine_register(system)
        .build(limit=limit, offset=offset, parse_budget=parse_budget)
    )


@_readonly_tool
def bw_review_scenario(
    system: str, scenario: str, limit: int = 50
) -> ScenarioReport | UnsupportedResult:
    """Run one analysis by id: 9.1-9.8, "layer_violations", or "unused_providers"."""
    limit, _ = _clamp_page(limit, 0)
    return runtime().analyzers(system).run_scenario(scenario, limit=limit)


# --- documentation generation tool -------------------------------------------------------


@_readonly_tool
def bw_generate_docs(
    system: str,
    output_dir: str | None = None,
    *,
    limit: int = 15,
    catalog_cap: int | None = None,
    sections: list[str] | None = None,
    resume: bool = False,
) -> DocGenResult:
    """Render the full markdown knowledge base (mission Section 8) to a git-ignored directory.

    Writes an index, per-section catalogues and detail pages (chains, lineage with Mermaid,
    providers, transformations, queries, HANA, the eight risk scenarios), and a non-empty
    gaps-and-risks register. ``output_dir`` must be outside the tracked repo tree; it defaults to
    ``output/docs/<system>``. Returns a manifest of the files written.

    ``limit`` is how many per-object detail pages each section renders; ``catalog_cap`` is how many
    rows its index catalogue fetches. ``catalog_cap`` defaults to ``limit`` so that asking for more
    detail pages widens the catalogue too — otherwise the detail loop would slice a shorter
    catalogue list and silently return fewer pages than asked for. Pass a large ``limit`` (e.g.
    5000) for full coverage of a system; the page bound here is deliberately far higher than the
    row bound on the list tools, because these pages go to disk rather than into the reply.

    ``sections`` restricts which sections are written, from: inventory, load-coverage, chains,
    lineage, providers, transformations, queries, hana, scenarios. Use it to split a full-coverage
    run across several calls — the database closes the session before a whole-system run finishes,
    and because generation only ever adds files, separate calls compose into the same tree.
    ``resume`` skips detail pages already on disk, so a section cut off mid-run continues rather
    than rebuilding what it already produced.
    """
    limit = max(1, min(limit, _MAX_DOCGEN_PAGES))
    cap = limit if catalog_cap is None else max(1, min(catalog_cap, _MAX_DOCGEN_PAGES))
    target = output_dir or f"output/docs/{system}"
    return (
        runtime()
        .docgen(system)
        .generate(target, limit=limit, catalog_cap=cap, sections=sections, resume=resume)
    )


# --- prompts (analyst workflows composing the read-only tools) ---------------------------

register_prompts(mcp)


def _load_local_dotenv() -> None:
    """Load ``KEY=VALUE`` pairs from a local ``.env`` into the environment (never overriding).

    Lets an MCP client launch the server with only ``BW_PROFILES_PATH`` set while the connection
    secrets stay in a git-ignored ``.env``. Looked up in order: ``BW_DOTENV_PATH``, then a ``.env``
    beside ``BW_PROFILES_PATH``, then ``./.env`` (first that exists wins). Existing environment
    variables always take precedence (``setdefault``), so nothing already provided is clobbered.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("BW_DOTENV_PATH")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    profiles = os.environ.get("BW_PROFILES_PATH")
    if profiles:
        candidates.append(Path(profiles).expanduser().resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    for path in candidates:
        try:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return  # first existing .env wins


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    _load_local_dotenv()
    mcp.run()


if __name__ == "__main__":
    main()
