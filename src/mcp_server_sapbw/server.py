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

import functools
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .connectors.base import ConnectorRegistry
from .connectors.bi import FileBiConnector
from .connectors.ecc import EccConnector
from .core.access import build_access_report
from .core.budget import (
    DEFAULT_MAX_QUERIES,
    DEFAULT_MAX_SECONDS,
    BudgetExceeded,
    query_budget,
)
from .core.cache import SqliteCache
from .core.capabilities import CapabilityResolver
from .core.connection import ReadOnlyConnection, ReadOnlyConnectionPool
from .core.dialect import record_tool_reads
from .core.identity import Environment, StorageIdentity
from .core.logging import configure as configure_logging
from .core.logging import get_logger
from .core.paths import cache_dir as default_cache_dir
from .core.paths import cache_file
from .core.profiles import ProfileManager
from .core.snapshots import SnapshotStore, snapshot_file
from .core.support import support_matrix
from .models.access import AccessReport
from .models.analysis import Analysis
from .models.capability import CapabilityRecord, CapabilityReport
from .models.chains import (
    Chain,
    ChainRuntimes,
    ChainSummary,
    LoadClosure,
    ScheduleMatrixEntry,
)
from .models.diagram import DiagramFormat, DiagramResult
from .models.ecc import ConnectorUnavailable, ExitInventory
from .models.errors import (
    BwError,
    ErrorCategory,
    ErrorCode,
    derive_failure_fields,
    error,
    from_exception,
)
from .models.findings import ScenarioReport
from .models.hana import CalcView, CalcViewLineage, HanaCrossingReport
from .models.health import ProviderHealth
from .models.lineage import ImpactAnalysis, LineageDirection, LineageGraph, TraceToSource
from .models.provenance import UnsupportedResult
from .models.providers import ObjectNotFound, Provider, ProviderType, SearchHit
from .models.queries import Query, QueryLineage, QueryOriginFilter, QuerySummary, QueryUsage
from .models.register import RoutineRegister
from .models.security import (
    AnalysisAuth,
    AnalysisAuthSummary,
    QueryAuthExposure,
    SecurityOverview,
)
from .models.snapshot import Snapshot, SnapshotDiff, SnapshotSummary
from .models.sources import EnhancementInventory, SourceTopology
from .models.support import SupportMatrix
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
from .repositories.security import SecurityRepository
from .repositories.sources import SourcesRepository
from .repositories.threex import ThreeXRepository
from .repositories.transformations import TransformationsRepository
from .services.analysis import AnalysisReaders, AnalysisService
from .services.analyzers import Analyzers
from .services.capability_report import build_report
from .services.diagram import build_layout, png_available, render_png, render_svg
from .services.docgen import DocGenerator, DocGenResult
from .services.exit_analysis import ExitAnalysisService
from .services.lineage import LineageService
from .services.load_closure import LoadClosureService
from .services.routine_register import RoutineRegisterService
from .services.snapshot import SnapshotService, families_for
from .services.snapshot import compare as compare_snapshots

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

# Response shaping. A wide provider or a busy lineage graph can dominate a model's context window,
# and the context spent on 150 field-provenance blocks is context unavailable for reasoning. Above
# these bounds the reply carries the shape plus the resource URI holding the complete record, so
# nothing becomes unreachable - it just stops being forced into every response.
_MAX_INLINE_FIELDS = 40
_MAX_INLINE_NODES = 60
_MAX_INLINE_EDGES = 90
# Applied inside a composed answer only; see _shape_query. A query's element tree and its
# field-lineage paths are the two payloads that dominated a live compound response.
_MAX_INLINE_ELEMENTS = 25
_MAX_INLINE_PATHS = 25

# A compound analysis walks the graph several times over, so its depth is bounded harder than a
# granular lineage call's: the cost of one extra hop is multiplied by the number of sections that
# traverse it, and a deep composed answer is usually worse than a shallow one plus a follow-up.
_MAX_ANALYSIS_DEPTH = 5

# How much of a result may be inlined before it is summarised. 'auto' decides per response.
DetailLevel = Literal["auto", "summary", "full"]

# Human-readable server identity reported over the protocol. Distinct from the key a client uses in
# its own config: most clients derive each tool's visible name by prefixing that key, and the result
# must stay a valid identifier under 64 characters, so a short key such as "sapbw" is recommended in
# the README rather than this display name.
SERVER_NAME = "SAP BW technical-discovery MCP"

mcp: FastMCP = FastMCP(name=SERVER_NAME, mask_error_details=True)

_LOG = get_logger("server")


# --- server-surface result models --------------------------------------------------------


class SystemStatus(BaseModel):
    """One configured profile and its discovery status (no host/credentials).

    Carries the identity as well as the alias. On an install serving several landscapes the alias is
    not distinguishing - everybody's production system is called ``prd`` - so an answer that named
    only the alias left a reader unable to tell which customer it was about.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["configured", "discovered"]
    release: str | None = None
    read_only_user: bool = True
    #: Which customer or landscape this profile belongs to. ``None`` is normal for a single-customer
    #: install and is what makes ``isolated_by_tenant`` false.
    tenant: str | None = None
    #: Declared by the operator, never inferred from an alias or a host name.
    environment: Environment = "unknown"
    #: Unambiguous across tenants, for use in a report: ``acme/prd (prod)``.
    label: str = ""
    #: True when this profile's stored data is separated from another customer's by a tenant.
    isolated_by_tenant: bool = False


class RefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str
    scope: str
    removed: int


class CacheStatus(BaseModel):
    """What extracted metadata is cached on disk for one profile.

    ``entries_by_type`` uses the same object-type names as the ``bw_refresh_cache`` scope, so a
    reader can see what is retained and purge exactly that.

    Snapshots are reported alongside the cache because they are the *other* thing at rest, and the
    larger of the two by object count: a snapshot names every provider, transformation and chain in
    the system. They differ from the cache in one way that matters to an operator: the cache expires
    on a TTL, a snapshot never does, because a baseline that vanished would defeat its purpose.
    """

    model_config = ConfigDict(extra="forbid")

    system: str
    #: The identity that decides where this profile's data lands. Reported so isolation is
    #: verifiable rather than asserted: on a machine serving several customers, ``storage_key`` is
    #: what proves two of them are not sharing a file.
    tenant: str | None = None
    environment: Environment = "unknown"
    storage_key: str = ""
    isolated_by_tenant: bool = False
    enabled: bool
    location: str | None = None  # the directory, never a host or credential
    exists: bool = False
    size_bytes: int = 0
    entries: int = 0
    entries_by_type: dict[str, int] = Field(default_factory=dict)
    structural_ttl_seconds: int | None = None
    runtime_ttl_seconds: int | None = None
    #: Stored snapshots for this profile, and their size. Retained until deleted, not by TTL.
    snapshots: int = 0
    snapshot_size_bytes: int = 0
    snapshot_location: str | None = None
    note: str | None = None


class BudgetResult(BaseModel):
    """A call that stopped at its per-call budget rather than running unbounded.

    Returned instead of raising, so the caller learns *why* the analysis stopped and what it cost.
    Narrow the request (a pattern, a lower depth, a smaller limit) or raise the budget via
    ``SAPBW_MAX_QUERIES_PER_CALL`` / ``SAPBW_MAX_SECONDS_PER_CALL``.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["budget_exceeded"] = "budget_exceeded"
    code: ErrorCode = "budget_exceeded"
    category: ErrorCategory | None = None
    remedy: str | None = None
    retryable: bool | None = None
    tool: str
    reason: str
    queries_spent: int
    elapsed_seconds: float
    budget: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive(self) -> "BudgetResult":
        self.category, self.retryable, self.remedy = derive_failure_fields(
            self.code, self.category, self.retryable, self.remedy
        )
        return self


class SnapshotListResult(BaseModel):
    """Stored snapshots as summaries. Payloads are large, so listing never returns them."""

    model_config = ConfigDict(extra="forbid")

    snapshots: list[SnapshotSummary] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


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


class AuthListResult(BaseModel):
    """Analysis authorisations by shape. Carries no concrete permission values by design."""

    model_config = ConfigDict(extra="forbid")

    items: list[AnalysisAuthSummary] = Field(default_factory=list)
    total_count: int
    limit: int
    offset: int
    #: True when the underlying value scan hit its row budget, so ``total_count`` is a lower bound.
    scan_truncated: bool = False


# --- runtime -----------------------------------------------------------------------------


class Runtime(Protocol):
    """The surface the tools depend on (implemented by ServerRuntime; faked in tests)."""

    def list_systems(self) -> list[SystemStatus]: ...
    def identity(self, system: str) -> StorageIdentity: ...
    def capability(self, system: str) -> CapabilityRecord: ...
    def access_report(self, system: str) -> AccessReport: ...
    def refresh_capabilities(self, system: str) -> CapabilityRecord: ...
    def refresh_cache(self, system: str, scope: str) -> RefreshResult: ...
    def cache_status(self, system: str) -> CacheStatus: ...
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
    def security(self, system: str) -> SecurityRepository: ...
    def query_auth_exposure(
        self, system: str, query: str
    ) -> QueryAuthExposure | UnsupportedResult: ...
    def routine_register(self, system: str) -> RoutineRegisterService: ...
    def analysis(self, system: str) -> AnalysisService: ...
    def snapshots(self, system: str) -> SnapshotService: ...
    def snapshot_store(self, system: str) -> SnapshotStore | None: ...
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
        cache_dir: Path | None = None,
    ) -> None:
        # Default to the platform's per-user cache location rather than a path relative to whatever
        # directory the MCP client happened to launch the process in (see core/paths.py).
        cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._profiles = profile_manager
        self._pool = pool
        self._resolver = resolver
        self._cache_dir = cache_dir
        self._capabilities: dict[str, CapabilityRecord] = {}
        # system -> (capability fingerprint, open cache). Retired when the fingerprint changes.
        self._caches: dict[str, tuple[str, SqliteCache]] = {}
        # system -> open snapshot store. Deliberately NOT keyed on the capability fingerprint: a
        # snapshot is a record of what was true then, and a capability refresh must not retire it.
        self._snapshot_stores: dict[str, SnapshotStore] = {}

    @classmethod
    def from_env(cls) -> "ServerRuntime":
        return cls(ProfileManager(), ReadOnlyConnectionPool(), CapabilityResolver())

    def _connection(self, system: str) -> ReadOnlyConnection:
        """The pooled read-only connection for a profile.

        Typed concretely rather than as ``object``. It was the latter, which forced every one of the
        nine call sites that pass it to a repository to carry a ``type: ignore`` - so the annotation
        was costing nine suppressions to avoid one import.
        """
        return self._pool.acquire(self._profiles.get(system))

    def identity(self, system: str) -> StorageIdentity:
        """Who a profile belongs to: the tenant, the alias, and the declared environment."""
        return self._profiles.get(system).identity

    def capability(self, system: str, *, refresh: bool = False) -> CapabilityRecord:
        record = self._capabilities.get(system)
        if refresh or record is None or record.is_expired():
            record = self._resolver.resolve(self._profiles.get(system), self._connection(system))
            self._capabilities[system] = record
        return record

    def refresh_capabilities(self, system: str) -> CapabilityRecord:
        return self.capability(system, refresh=True)

    def access_report(self, system: str) -> AccessReport:
        """Which deployment mode is in force for a profile, and what it cannot answer.

        Built here rather than in the tool because it needs the profile, and a ``Profile`` holds
        the password as a ``SecretStr`` - keeping it inside the runtime means no tool function ever
        has a secret one attribute dereference away from its return value.
        """
        profile = self._profiles.get(system)
        return build_access_report(
            self.capability(system),
            declared_mode=profile.access_mode,
            read_only_asserted=profile.read_only_user,
            matrix=support_matrix(),
        )

    def _cache(self, system: str) -> SqliteCache | None:
        """The per-profile metadata cache, tied to the current capability fingerprint.

        One SQLite connection is kept per system for the process's lifetime. A capability refresh
        changes the fingerprint, which retires the instance so entries extracted under the old
        release picture are not reused. A cache that cannot be opened (read-only filesystem, locked
        file) is never fatal: the repositories simply run uncached.

        Returns ``None`` when the profile sets ``cache_enabled: false``. Structural extracts include
        ABAP routine source and query definitions, so an organisation that will not accept customer
        metadata at rest can switch it off per system and pay the re-read cost instead.
        """
        profile = self._profiles.get(system)
        if not profile.cache_enabled:
            return None
        record = self.capability(system)
        # The server version is part of the fingerprint, not just the discovery timestamp. An
        # upgrade that widens an extract - a new optional field on a cached model - would otherwise
        # keep validating against the old cached JSON, and the new field would read as absent for
        # the whole TTL. The customer would see a feature they installed reporting nothing, with no
        # error to explain it. Retiring the cache on upgrade costs one re-read and cannot mislead.
        fingerprint = f"{__version__}|{record.discovered_at.isoformat()}"
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
                # Through `cache_file`, not assembled here. Assembling it here is what let a profile
                # alias containing `..` write customer metadata outside the cache root: the
                # sanitising helper existed and nothing called it.
                cache_file(system, tenant=profile.tenant, directory=self._cache_dir),
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

    def cache_status(self, system: str) -> CacheStatus:
        """Report what is cached on disk for a profile, without reading any cached value."""
        profile = self._profiles.get(system)
        identity = profile.identity

        def stamped(status: CacheStatus) -> CacheStatus:
            """Attach the identity to whichever branch answered.

            Applied on every branch, including the ones that keep nothing: an operator verifying
            two customers are not sharing a file needs the identity even when the answer is
            "nothing is stored here".
            """
            return status.model_copy(
                update={
                    "tenant": identity.tenant,
                    "environment": identity.environment,
                    "storage_key": identity.key,
                    "isolated_by_tenant": identity.isolated_by_tenant,
                }
            )

        if not profile.cache_enabled:
            return stamped(
                CacheStatus(
                    system=system,
                    enabled=False,
                    note="cache_enabled is false for this profile: no metadata is written to disk",
                )
            )
        cache = self._cache(system)
        if cache is None:
            return stamped(
                CacheStatus(
                    system=system,
                    enabled=True,
                    location=str(self._cache_dir),
                    note="the cache could not be opened; the server is running uncached",
                )
            )
        counts = cache.entry_counts()
        path = cache_file(system, tenant=identity.tenant, directory=self._cache_dir)
        # Only report the snapshot file if one already exists. Opening the store to answer a status
        # question would create an empty database and make the report the cause of the thing it
        # reports.
        snapshot_path = snapshot_file(system, self._cache_dir, tenant=identity.tenant)
        snapshots = self._snapshot_stores.get(system)
        if snapshots is None and snapshot_path.exists():
            snapshots = self.snapshot_store(system)
        return stamped(
            CacheStatus(
                system=system,
                enabled=True,
                location=str(self._cache_dir),
                exists=path.exists(),
                size_bytes=path.stat().st_size if path.exists() else 0,
                entries=sum(counts.values()),
                entries_by_type=counts,
                structural_ttl_seconds=cache.structural_ttl,
                runtime_ttl_seconds=cache.runtime_ttl,
                snapshots=snapshots.count(system=system) if snapshots is not None else 0,
                snapshot_size_bytes=snapshots.size_bytes() if snapshots is not None else 0,
                snapshot_location=str(self._cache_dir) if snapshot_path.exists() else None,
            )
        )

    def chains(self, system: str) -> ChainsRepository:
        return ChainsRepository(
            self._connection(system),
            self.capability(system),
            self._cache(system),
        )

    def providers(self, system: str) -> ProvidersRepository:
        return ProvidersRepository(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def search(self, system: str) -> SearchRepository:
        return SearchRepository(
            self._connection(system),
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
            self._connection(system),
            self.capability(system),
            self._cache(system),
        )

    def _ecc_connector(
        self, name: str | None = None, *, bw_system: str | None = None
    ) -> EccConnector:
        """Build the ECC connector: a named profile, the one declaring ``bw_system``, or none.

        Resolution order, most explicit first:

        1. ``name`` - the caller named a profile.
        2. ``bw_system`` - exactly one profile declares ``serves: [<that system>]``.
        3. the sole configured profile, when there is only one.

        Anything else returns an unconfigured connector, so a connector-gated scenario reports why
        rather than reading the wrong system. What changed here: several ECC profiles and no name
        previously *always* fell through to unconfigured, which left scenario 9.6 permanently
        unpopulated even though a working connector was present. The mapping is now declared in the
        profile rather than guessed or given up on.
        """
        names = self._profiles.ecc_names()
        chosen = name
        if chosen is None and bw_system is not None:
            serving = [n for n in names if bw_system in self._profiles.get_ecc(n).serves]
            # Exactly one, or it is ambiguous again and declining is still the right answer.
            chosen = serving[0] if len(serving) == 1 else None
        if chosen is None and len(names) == 1:
            chosen = names[0]
        if chosen is None:
            return EccConnector()
        return EccConnector(self._profiles.get_ecc(chosen))

    def _registry(self, bw_system: str | None = None) -> ConnectorRegistry:
        """Connector registry for the analyzers, for a given BW system.

        ``bw_system`` lets the ECC connector be resolved from the profile that declares it serves
        that system, which is what makes the connector-gated scenarios populate on a landscape with
        several source systems configured.

        Each connector appears only when configured; otherwise the connector-gated scenarios report
        "not configured" with the reason rather than guessing. The BI connector is vendor-neutral —
        it reads an inventory exported from whatever platform the organisation runs — so 9.7 and 9.8
        populate for Tableau, Power BI, SAC, Looker or Qlik through the same path.
        """
        configured: list[Any] = []
        ecc = self._ecc_connector(bw_system=bw_system)
        if ecc.is_configured():
            configured.append(ecc)
        inventory = self._profiles.bi_inventory_path()
        if inventory:
            bi = FileBiConnector(inventory)
            if bi.is_configured():
                configured.append(bi)
            else:
                _LOG.warning("BI inventory not usable: %s", bi.status().detail)
        return ConnectorRegistry(configured)

    def analyzers(self, system: str) -> Analyzers:
        return Analyzers(
            self._connection(system),
            self.capability(system),
            self._cache(system),
            registry=self._registry(system),
        )

    def docgen(self, system: str) -> DocGenerator:
        return DocGenerator(
            self._connection(system),
            self.capability(system),
            self._cache(system),
            registry=self._registry(system),
        )

    def snapshots(self, system: str) -> SnapshotService:
        # No extract cache: a snapshot is a point-in-time reading, and serving one from a cache
        # populated hours ago would date the very thing being measured.
        return SnapshotService(self._connection(system), self.capability(system))

    def snapshot_store(self, system: str) -> SnapshotStore | None:
        """The per-profile snapshot store, or ``None`` when the profile keeps nothing at rest.

        A snapshot names every provider, transformation and chain in a system, so it honours the
        same `cache_enabled` switch as the extract cache. With it off, snapshots still work for
        an immediate comparison; they just are not kept.
        """
        profile = self._profiles.get(system)
        if not profile.cache_enabled:
            return None
        existing = self._snapshot_stores.get(system)
        if existing is not None:
            return existing
        try:
            store = SnapshotStore(snapshot_file(system, self._cache_dir, tenant=profile.tenant))
        except (sqlite3.Error, OSError):
            return None
        self._snapshot_stores[system] = store
        return store

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
            self._connection(system),
            self.capability(system),
            self._cache(system),
        )

    def sources(self, system: str) -> SourcesRepository:
        return SourcesRepository(
            self._connection(system),
            self.capability(system),
            self._cache(system),
        )

    def threex(self, system: str) -> ThreeXRepository:
        return ThreeXRepository(
            self._connection(system),
            self.capability(system),
            self._cache(system),
        )

    def security(self, system: str) -> SecurityRepository:
        """Security repository — built **without** a cache, deliberately.

        Permission data is not structural metadata: persisting it widens the blast radius of the
        cache file, and a stale answer to "who can see this" is worse than a slow one.
        """
        return SecurityRepository(
            self._connection(system),
            self.capability(system),
            None,
        )

    def query_auth_exposure(self, system: str, query: str) -> QueryAuthExposure | UnsupportedResult:
        """Join a query's InfoObjects against the authorisation-relevant characteristics.

        Lives on the runtime rather than in either repository because it spans both: the query
        subsystem knows which characteristics a report touches, and the security subsystem knows
        which of those make a result user-specific.
        """
        security = self.security(system)
        unsupported = security.require_security()
        if unsupported is not None:
            return unsupported
        lineage = self.queries(system).get_query_lineage(query)
        if isinstance(lineage, UnsupportedResult):
            return lineage
        definition = self.queries(system).get_query(query)
        auth_variables: list[str] = []
        if not isinstance(definition, UnsupportedResult):
            auth_variables = sorted(
                variable.name
                for variable in definition.variables
                if variable.processing_type == "authorization"
            )
        exposure = security.query_exposure(
            compuid=lineage.compuid,
            compid=lineage.compid,
            providers=list(lineage.providers),
            characteristics=[path.iobjnm for path in lineage.paths],
        )
        if auth_variables:
            exposure = exposure.model_copy(
                update={
                    "authorization_variables": auth_variables,
                    "user_specific_result": True,
                    "caveats": [
                        *exposure.caveats,
                        f"{len(auth_variables)} variable(s) are filled from the user's "
                        "authorisations at runtime (RSZGLOBV.VPROCTP=6): like customer-exit "
                        "variables these are a metadata dead end, named but not resolvable.",
                    ],
                }
            )
        return exposure

    def routine_register(self, system: str) -> RoutineRegisterService:
        return RoutineRegisterService(
            self._connection(system), self.capability(system), self._cache(system)
        )

    def analysis(self, system: str) -> AnalysisService:
        """The compound-analysis service, with every reader it composes built once.

        Assembled here rather than inside the service for two reasons that both belong to the
        runtime. The security repository must be built without a cache, and that rule is enforced
        in one place; and ``LineageService`` and ``TransformationsRepository`` memoise per instance,
        so an analysis rebuilding them per section would discard the memo that pays for it.
        """
        connection, capability, cache = (
            self._connection(system),
            self.capability(system),
            self._cache(system),
        )
        return AnalysisService(
            AnalysisReaders(
                system=system,
                capability=capability,
                providers=ProvidersRepository(connection, capability, cache),
                lineage=LineageService(connection, capability, cache),
                transformations=TransformationsRepository(connection, capability, cache),
                queries=QueriesRepository(connection, capability, cache),
                chains=ChainsRepository(connection, capability, cache),
                load_closure=LoadClosureService(connection, capability, cache),
                health=HealthRepository(connection, capability, cache),
                hana=HanaRepository(connection, capability, cache),
                query_auth_exposure=lambda query: self.query_auth_exposure(system, query),
            )
        )

    def close(self) -> None:
        """Release database sessions and cache handles. Called on shutdown; safe to call twice."""
        for _fingerprint, cache in list(self._caches.values()):
            with suppress(Exception):
                cache.close()
        self._caches.clear()
        for store in list(self._snapshot_stores.values()):
            with suppress(Exception):
                store.close()
        self._snapshot_stores.clear()
        with suppress(Exception):
            self._pool.close_all()
        _LOG.info("runtime closed: sessions and cache handles released")

    def list_systems(self) -> list[SystemStatus]:
        result: list[SystemStatus] = []
        for name in self._profiles.names():
            profile = self._profiles.get(name)
            record = self._capabilities.get(name)
            identity = profile.identity
            result.append(
                SystemStatus(
                    name=name,
                    status="discovered" if record is not None else "configured",
                    release=record.bw_release if record is not None else None,
                    read_only_user=profile.read_only_user,
                    tenant=identity.tenant,
                    environment=identity.environment,
                    label=identity.label,
                    isolated_by_tenant=identity.isolated_by_tenant,
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
    """Register a read-only tool, asserting the MCP naming constraint at registration time.

    Every tool runs inside a :func:`query_budget`, so no single call can issue unbounded statements
    or run indefinitely against a customer system. Hitting the bound returns a structured
    :class:`BudgetResult` naming what was spent, which is an honest partial answer rather than a
    hang or a silent truncation.
    """
    name = getattr(func, "__name__", "")
    if not _TOOL_NAME_RE.match(name) or len(name) > _MAX_TOOL_NAME:
        raise ValueError(f"tool name {name!r} violates MCP naming (^[a-zA-Z][a-zA-Z0-9_]*$, <=40)")

    @functools.wraps(func)
    def budgeted(*args: Any, **kwargs: Any) -> Any:
        with (
            query_budget(
                max_queries=_budget_limits()[0], max_seconds=_budget_limits()[1]
            ) as budget,
            # Inert in production (one bool check); switched on only by the test session, so the
            # support matrix can measure which capabilities each tool needs rather than a
            # hand-written mapping drifting the first time a tool gains a reader.
            record_tool_reads(name),
        ):
            try:
                return func(*args, **kwargs)
            except BudgetExceeded as exc:
                _LOG.warning("tool=%s stopped on budget: %s", name, exc.reason)
                return BudgetResult(
                    tool=name,
                    reason=exc.reason,
                    queries_spent=exc.queries,
                    elapsed_seconds=exc.elapsed_seconds,
                    budget=cast("dict[str, Any]", budget.snapshot()),
                )
            except Exception as exc:  # deliberately broad; see below
                # Anything else becomes a structured failure rather than an opaque MCP error.
                # Eleven exception classes could previously escape here, so a locked-down user
                # hitting the read-only guard, a mistyped profile name and a dropped HANA session
                # were indistinguishable to a program. from_exception forwards a message only for
                # the families scrubbed at their raise site, so this path cannot leak a host name.
                failure = from_exception(exc, tool=name)
                _LOG.warning("tool=%s failed: code=%s (%s)", name, failure.code, type(exc).__name__)
                return failure

    # Widen the declared return type to include the failure branches, once, here. Every tool can
    # return a BudgetResult or a BwError, and a schema that does not say so is a schema a client
    # will reject at validation time - which is how this was found. Doing it in the decorator keeps
    # 46 signatures honest without 46 edits, and means a new tool cannot forget.
    declared = budgeted.__annotations__.get("return")
    if declared is not None:
        budgeted.__annotations__["return"] = declared | BudgetResult | BwError

    return mcp.tool(annotations={"readOnlyHint": True})(budgeted)


def _budget_limits() -> tuple[int, float]:
    """Per-call budget from the environment, so an operator can tune it without a code change."""
    queries = os.environ.get("SAPBW_MAX_QUERIES_PER_CALL")
    seconds = os.environ.get("SAPBW_MAX_SECONDS_PER_CALL")
    try:
        max_queries = int(queries) if queries else DEFAULT_MAX_QUERIES
    except ValueError:
        max_queries = DEFAULT_MAX_QUERIES
    try:
        max_seconds = float(seconds) if seconds else DEFAULT_MAX_SECONDS
    except ValueError:
        max_seconds = DEFAULT_MAX_SECONDS
    return max_queries, max_seconds


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
def bw_capability_report(system: str) -> CapabilityReport:
    """Which questions this server can actually answer on this system, and which it cannot.

    Two facts have to meet before an answer exists: the server must implement a reader, and the
    connected release must have the object. ``bw_system_profile`` reports the second;
    ``docs/capability-contract.md`` reports the first; neither alone tells you whether a given
    question resolves here. This crosses them and states the verdict per capability:

    * ``usable`` - implemented here, present there.
    * ``absent_on_system`` - implemented, but this release does not carry the object, so the
      features built on it return an unsupported result rather than a thin answer.
    * ``not_implemented`` - the object is on your system, but no reader exists yet. Fixable here.
    * ``not_applicable`` - neither.

    The distinction matters because the two failure modes look identical from the outside and only
    one of them is a gap in this server.
    """
    return build_report(runtime().capability(system))


@_readonly_tool
def bw_access_report(system: str) -> AccessReport:
    """Which deployment mode this connection is in, and the exact grants to change it.

    The provisioning question, answered from evidence rather than from the profile's claim: *what
    is this user allowed to read, what does that cost, and what would we have to grant?*

    Two postures are supported and both are legitimate. ``technical_read`` is SELECT across the
    ABAP schema and the SYS catalog, and answers everything implemented here. ``least_privilege``
    is an explicit allow-list, and answers less - but *which* questions it gives up is stated per
    grant group here instead of being discovered one failed tool at a time.

    The distinction this rests on is one the server could not previously make. A refused read and
    an object a release does not have both end in no rows, so discovery reported them
    identically - and a denied dictionary read therefore surfaced as "this system has no Advanced
    DSOs", with tools going on to report "not available on BW 7.50". That points a customer at a
    BW upgrade for something one ``GRANT SELECT`` fixes. Probes now record ``denied`` separately
    from ``absent``, and this report is where that shows up:

    * ``grants_required`` - ready-to-run statements for what was refused.
    * ``blocked_tools`` - tools reading at least one refused object, from the support matrix's
      measured attribution, so it cannot drift from what the tools really read. A lower bound.
    * ``undetermined_object_models`` - variants reading ``False`` in ``bw_system_profile`` for lack
      of evidence rather than for lack of the object.
    * ``mode_mismatch`` - the profile's declared mode disagrees with the evidence, which is a
      provisioning fault worth naming rather than absorbing.

    A group reported ``granted`` was probed, not audited: this reports what was exercised on this
    connection, not a permission review.
    """
    return runtime().access_report(system)


@_readonly_tool
def bw_support_matrix(
    tool: str | None = None, release: str | None = None, system: str | None = None
) -> SupportMatrix | BwError:
    """Which tool works on which BW release. **Answerable without connecting to anything.**

    Every other support answer here needs a profile first. This one is built from data shipped in
    the package, so it answers the question a customer has before installing anything: *I run BW
    7.4 (or BW/4HANA) - which of your tools will work on my landscape?*

    Keyed by **tool**, because that is the unit the question is asked in; nobody asks whether
    RSPCPROCESSLOG is present. ``requires`` bridges to the capability contract and is **measured**
    by attributing each read to the tool that caused it, so it cannot drift the way a hand-written
    mapping would. It is a lower bound: everything listed really is read, and a code path no test
    reaches contributes nothing.

    Read the verdicts literally. There is deliberately no ``supported``:

    * ``verified`` - every capability it needs was read through a feature on that release.
    * ``expected`` - implemented and read, but not everything was verified on that release.
    * ``unverified`` - nobody has run it against that release. **Not a prediction.** Only BW 7.50
      has been verified here, so every other release reports this for every tool. Which metadata
      objects a release carries is what the capability resolver discovers at connect time, and
      asserting it from a version number would be guesswork dressed as a support statement.
    * ``needs_connector`` - the BW half works; the answer is completed by a system outside BW.
    * ``unknown`` - this build could not measure what the tool needs, which is not the same as the
      tool needing nothing.

    Filter with ``tool`` or ``release``. Naming a ``system`` sharpens the result with that system's
    discovery record - turning ``unverified`` into a statement about your own release - but it is
    an option, not a precondition.
    """
    matrix = support_matrix()
    if matrix is None:
        return error(
            "internal_error",
            "the support-matrix data file is missing from this installation; regenerate it with "
            "python scripts/support_matrix.py",
        )
    if release is not None and not any(r.release == release for r in matrix.releases):
        return error(
            "invalid_argument",
            f"unknown release {release!r}; this build has an opinion about "
            f"{', '.join(r.release for r in matrix.releases)}",
        )
    if tool is not None and matrix.tool(tool) is None:
        return error("object_not_found", f"no registered tool named {tool!r}", id=tool)
    return _narrow_matrix(matrix, tool=tool, release=release, system=system)


def _narrow_matrix(
    matrix: SupportMatrix, *, tool: str | None, release: str | None, system: str | None
) -> SupportMatrix:
    """Apply the filters, and fold in a live discovery record when a system was named.

    The live part is what turns ``unverified`` into something actionable: the shipped matrix cannot
    know what a customer's release carries, but their own capability record does. Crossing the two
    reports presence per required capability without claiming the tool was ever *run* there - which
    is why the verdict stays ``unverified`` and the finding lands in a caveat instead.
    """
    tools = [t for t in matrix.tools if tool is None or t.tool == tool]
    releases = [r for r in matrix.releases if release is None or r.release == release]
    kept = {r.release for r in releases}
    tools = [
        t.model_copy(update={"releases": {k: v for k, v in t.releases.items() if k in kept}})
        for t in tools
    ]
    totals = {k: v for k, v in matrix.totals.items() if k in kept}
    caveats = list(matrix.caveats)

    if system is not None:
        try:
            record = runtime().capability(system)
        except Exception as exc:
            caveats.append(
                f"a discovery record for {system!r} could not be read ({type(exc).__name__}), so "
                "this is the offline matrix only."
            )
        else:
            blocked = {
                t.tool: sorted(c for c in t.requires if not record.is_available(c))
                for t in tools
                if t.requires
            }
            missing = {name: caps for name, caps in blocked.items() if caps}
            caveats.append(
                f"crossed with {system} (release {record.bw_release}): "
                f"{len(tools) - len(missing)} of {len(tools)} tools have every capability they "
                "need present on that system. Presence is not the same as having been run "
                "there, so the release verdict above is unchanged."
            )
            if missing:
                caveats.append(
                    "on this system these tools are missing at least one capability they read: "
                    + "; ".join(
                        f"{name} ({', '.join(caps)})" for name, caps in sorted(missing.items())
                    )
                )
    return matrix.model_copy(
        update={"tools": tools, "releases": releases, "totals": totals, "caveats": caveats}
    )


@_readonly_tool
def bw_create_snapshot(
    system: str, families: list[str] | None = None, keep: bool = True
) -> Snapshot | BwError:
    """Capture a system's structural metadata as fingerprints, for comparing later.

    BW answers neither "what changed since last week" nor "what differs between QA and production":
    a transport log says what moved, not what the result was, and says nothing about a change made
    outside transport. A snapshot is the missing baseline.

    It holds a fingerprint per object rather than a copy of the metadata, so a system's ~24,000
    objects fit in a few hundred kilobytes and comparison becomes a set operation. **Only structural
    facts are fingerprinted** - timestamps, last-changed-by, last-used dates and record counts are
    never read, because including them would report every object as changed on every run.

    `families` defaults to providers, transformations, chains, datasources and dtps - the
    dataflow. Add `queries` and `infoobjects` when the question needs them; they are an order of
    magnitude larger. A family this release cannot report is listed as unavailable, not empty,
    because a later diff would read empty as deleted.

    With ``keep`` the snapshot is stored under the per-user cache root for later comparison; a
    profile with ``cache_enabled: false`` keeps nothing, and two snapshots can still be compared in
    the same session.
    """
    try:
        selected = families_for(families)
    except ValueError as exc:
        return error("invalid_argument", str(exc), system=system)
    captured = runtime().snapshots(system).capture(system=system, families=selected)
    if isinstance(captured, Snapshot) and keep:
        store = runtime().snapshot_store(system)
        if store is not None:
            store.put(captured)
        else:
            captured.caveats.append(
                "not stored: this profile keeps no metadata at rest (cache_enabled: false), so "
                "there is nothing to compare against later. Capture both sides in one session, or "
                "enable the store."
            )
    return cast("Snapshot | BwError", captured)


@_readonly_tool
def bw_list_snapshots(system: str | None = None, limit: int = 50) -> SnapshotListResult:
    """Stored snapshots, newest first, as summaries rather than payloads."""
    store = runtime().snapshot_store(system) if system else None
    if system and store is None:
        return SnapshotListResult(
            snapshots=[],
            caveats=[
                f"profile {system!r} keeps no metadata at rest (cache_enabled: false), so no "
                "snapshot has been stored for it."
            ],
        )
    if store is None:
        return SnapshotListResult(
            snapshots=[],
            caveats=["name a system: snapshots are stored per profile."],
        )
    return SnapshotListResult(snapshots=store.list(system=system, limit=max(1, min(limit, 500))))


@_readonly_tool
def bw_compare_snapshots(
    system: str, left: str, right: str | None = None
) -> SnapshotDiff | BwError:
    """Diff two stored snapshots of the same system: what was added, removed or changed.

    ``left`` is the baseline. Omit ``right`` to compare the baseline against a freshly captured
    reading of the system as it is now - the "what changed since then" question.

    Read ``comparable`` first. False means the two sides can report different things, so an absence
    on one side is not evidence of a difference; the diff is still returned, restricted to what both
    sides can see, and ``comparability`` says what was excluded and why. A ``changed`` object names
    the individual facts that differ, with both values.
    """
    store = runtime().snapshot_store(system)
    if store is None:
        return error(
            "connector_not_configured",
            f"profile {system!r} keeps no metadata at rest, so no snapshot is stored for it",
            system=system,
        )
    baseline = store.get(left)
    if baseline is None:
        return error("object_not_found", f"no stored snapshot {left!r}", system=system, id=left)
    if right is None:
        current = (
            runtime().snapshots(system).capture(system=system, families=baseline.scope.families)
        )
        if not isinstance(current, Snapshot):
            return cast("BwError", current)
    else:
        found = store.get(right)
        if found is None:
            return error(
                "object_not_found", f"no stored snapshot {right!r}", system=system, id=right
            )
        current = found
    return compare_snapshots(baseline, current)


@_readonly_tool
def bw_compare_systems(
    left_system: str, right_system: str, families: list[str] | None = None
) -> SnapshotDiff | BwError:
    """Compare two systems as they are now: DEV against QA, QA against production.

    Captures both sides and diffs them, so nothing has to be stored first. Three corrections are
    applied before anything is called a difference, and all three are reported:

    * **Capability parity.** A metadata table present on one system and absent on the other would
      surface as thousands of removed objects. Families only one side can report are excluded.
    * **Environment-specific names.** A DataSource endpoint carries its logical system, which BDLS
      rewrites per environment - comparing raw would make every DataSource look replaced. The
      logical system is kept as a *fact* instead, so a real difference shows as one changed fact.
    * **Volatile facts.** Timestamps and counters are never fingerprinted, so a difference is
      structural.
    """
    try:
        selected = families_for(families)
    except ValueError as exc:
        return error("invalid_argument", str(exc), left=left_system, right=right_system)
    left = runtime().snapshots(left_system).capture(system=left_system, families=selected)
    if not isinstance(left, Snapshot):
        return cast("BwError", left)
    right = runtime().snapshots(right_system).capture(system=right_system, families=selected)
    if not isinstance(right, Snapshot):
        return cast("BwError", right)
    return compare_snapshots(left, right)


@_readonly_tool
def bw_cache_status(system: str) -> CacheStatus:
    """What extracted metadata is currently cached on local disk, and where.

    Answers the compliance question directly: structural extracts include ABAP routine source and
    query definitions, so an operator needs to see what is at rest without opening the file. Reports
    the location, per-object-type entry counts and size. Set ``cache_enabled: false`` on the profile
    to keep nothing at rest, or call ``bw_refresh_cache`` to purge.
    """
    return runtime().cache_status(system)


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
    system: str,
    name: str,
    object_type: ProviderType | None = None,
    *,
    detail: DetailLevel = "auto",
) -> Provider | ObjectNotFound | UnsupportedResult:
    """Universal deep-dive for any provider/InfoObject: definition, fields, parts, description.

    Auto-detects the object type when ``object_type`` is omitted. The description is labelled
    stored vs generated (origin) with a quality flag. ``ObjectNotFound`` when the name matches no
    object; ``UnsupportedResult`` when the requested type's tables are absent on this release.

    ``detail`` controls the field list, which dominates the response for wide providers (a real DSO
    can carry 150+ fields, each with its own provenance):

    * ``auto`` (default) — every field up to a threshold, then key fields only plus a note and the
      ``bw://{system}/provider/{name}`` resource URI for the complete record. Nothing is lost.
    * ``summary`` — always key fields only.
    * ``full`` — always every field.
    """
    provider = runtime().providers(system).describe(name, object_type)
    if isinstance(provider, Provider):
        return _shape_provider(provider, system=system, detail=detail)
    return provider


def _shape_provider(provider: Provider, *, system: str, detail: DetailLevel) -> Provider:
    """Trim a provider's field list when it would dominate the reply, pointing at the resource."""
    total = len(provider.fields)
    if detail == "full" or (detail == "auto" and total <= _MAX_INLINE_FIELDS):
        return provider
    keys = {key.upper() for key in provider.key_field_names}
    kept = [f for f in provider.fields if f.is_key or f.name.upper() in keys]
    shaped = provider.model_copy(
        update={
            "fields": kept,
            "caveats": [
                *provider.caveats,
                f"field list summarised: {len(kept)} of {total} shown (the semantic key). "
                f"Read {resource_uri(system, 'provider', provider.name)} for every field, "
                "or call again with detail='full'.",
            ],
        }
    )
    return shaped


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
    system: str,
    name: str,
    direction: LineageDirection = "both",
    depth: int = 3,
    *,
    detail: DetailLevel = "auto",
) -> LineageGraph | UnsupportedResult:
    """Directed lineage graph around an object (upstream/downstream/both) to a depth.

    Nodes + edges JSON, including advisory routine-derived edges (a target's routines' reads). Large
    graphs are truncated at a node cap with ``truncated=true``.

    ``detail='auto'`` (default) keeps the whole graph while it is small, and above that keeps the
    nodes and edges nearest the root, saying so in ``caveats``. Use ``full`` for the entire graph,
    or lower ``depth`` — a smaller depth is usually a better answer than a truncated big graph,
    and ``bw_render_lineage`` draws the shape without spending context on JSON.
    """
    graph = runtime().lineage(system).get_lineage(name, direction=direction, depth=depth)
    if isinstance(graph, LineageGraph):
        return _shape_graph(graph, detail=detail)
    return graph


def _shape_graph(graph: LineageGraph, *, detail: DetailLevel) -> LineageGraph:
    """Keep the neighbourhood of the root when a graph would dominate the reply.

    Node and edge *counts* stay exact — the caller still learns the true size — and the trimming
    keeps edges whose endpoints are both retained, so what is returned remains a valid subgraph
    rather than a set of dangling references.
    """
    nodes, edges = len(graph.nodes), len(graph.edges)
    if detail == "full" or (
        detail == "auto" and nodes <= _MAX_INLINE_NODES and edges <= _MAX_INLINE_EDGES
    ):
        return graph
    kept_nodes = graph.nodes[:_MAX_INLINE_NODES]
    kept_ids = {node.id for node in kept_nodes}
    kept_edges = [e for e in graph.edges if e.src in kept_ids and e.dst in kept_ids]
    kept_edges = kept_edges[:_MAX_INLINE_EDGES]
    return graph.model_copy(
        update={
            "nodes": kept_nodes,
            "edges": kept_edges,
            "caveats": [
                *graph.caveats,
                f"response summarised: {len(kept_nodes)} of {nodes} nodes and "
                f"{len(kept_edges)} of {edges} edges shown, nearest the root "
                f"('{graph.root_id}'). node_count/edge_count remain exact. Call again with "
                "detail='full' for the whole graph, lower the depth, or use bw_render_lineage.",
            ],
        }
    )


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
    ecc_system: str | None = None,
    include_source: bool = False,
    datasources: list[str] | None = None,
) -> ExitInventory | ConnectorUnavailable:
    """The ABAP behind extractor enhancements, read from the source system over ADT.

    BW proves *that* a DataSource was enhanced but holds none of the logic. This reads the four
    extractor-exit slots of enhancement RSAP0001 from the source system and reports, per slot, the
    DataSources its CASE dispatches on, the tables it reads, and anti-patterns including per-record
    SELECTs — the questions scenario 9.6 asks and BW cannot answer. A slot whose include does not
    exist is reported as absent, which means no enhancement of that DataSource kind is implemented;
    a slot that could not be read is reported as unknown rather than absent.

    **When the include looks empty, do not conclude the enhancement is trivial.** A common pattern
    builds a program name from the DataSource and calls it (``PERFORM ... IN PROGRAM (name)``),
    which ABAP resolves at runtime, so the logic is unreachable from the include. That dispatch is
    detected and reported as ``dynamic_dispatch``, and passing ``datasources`` resolves each
    ``<prefix><DATASOURCE>`` program and analyses it — one program per DataSource, so its table
    reads and per-record SELECTs attribute exactly. Use the output of
    ``bw_get_enhancement_inventory`` for that list. Each candidate is one request against the source
    system, bounded by the profile's ``max_satellite_fetches``; when the bound binds, the shortfall
    is reported as a caveat.

    The connection is GET-only and takes no ADT locks. ``ecc_system`` names an ``ecc_systems``
    profile and may be omitted when exactly one is configured. Full ABAP is opt-in via
    ``include_source`` and is capped; the analysis always covers the whole include.
    """
    service = runtime().exit_analysis(ecc_system)
    if isinstance(service, ConnectorUnavailable):
        return service
    return service.inventory(include_source=include_source, datasources=datasources)


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


# --- security / analysis-authorisation tools ---------------------------------------------
#
# These read a different class of data from every other tool: RSECVAL holds permission *values*, not
# structure. Three deliberate constraints, enforced in the repository rather than by convention:
# nothing here is cached at any tier; concrete values come only from bw_get_analysis_auth; and an
# unreadable RSEC* table is reported as a gap, never taken to mean no authorisations exist.


@_readonly_tool
def bw_security_overview(system: str, limit: int = 200) -> SecurityOverview | UnsupportedResult:
    """Row-level security posture: catch-all authorisations, unrestricted users, coverage gaps.

    Contains no concrete permission values. The finding to look for is
    ``uncovered_characteristics``: a characteristic flagged authorisation-relevant that no
    authorisation covers blocks every query touching it for any user without a catch-all — a live
    configuration fault that is invisible unless both sides are compared.
    """
    limit, _ = _clamp_page(limit, 0)
    return runtime().security(system).overview(limit=limit)


@_readonly_tool
def bw_list_analysis_auths(
    system: str,
    *,
    include_generated: bool = True,
    limit: int = _DEFAULT_PAGE,
    offset: int = 0,
) -> AuthListResult | UnsupportedResult:
    """Analysis authorisations by shape: which characteristics, how many ranges, catch-all or not.

    Deliberately excludes concrete values, so a landscape-wide question cannot incidentally place a
    permission dump into context. ``origin`` distinguishes generated authorisations (maintained by a
    program or DAP, so a manual edit is overwritten on the next run) from hand-maintained ones.
    Set ``include_generated=False`` to see only what a person maintains.
    """
    limit, offset = _clamp_page(limit, offset)
    result = (
        runtime()
        .security(system)
        .list_authorisations(limit=limit, offset=offset, include_generated=include_generated)
    )
    if isinstance(result, UnsupportedResult):
        return result
    items, total, scan_truncated = result
    return AuthListResult(
        items=items,
        total_count=total,
        limit=limit,
        offset=offset,
        scan_truncated=scan_truncated,
    )


@_readonly_tool
def bw_get_analysis_auth(system: str, name: str) -> AnalysisAuth | UnsupportedResult:
    """One analysis authorisation in full, including its value ranges and assigned users.

    **This is the only tool that returns concrete permission data**, and the result says so
    (``contains_data_values``). Special values are decoded rather than passed through: ``:`` grants
    *aggregated* access only (a total, but not the rows behind it - often misread as no access),
    ``#`` is the unassigned member, ``*`` is everything. A range driven by a variable resolves per
    user at runtime and is flagged, because metadata cannot state its effective scope.
    """
    return runtime().security(system).get_authorisation(name)


@_readonly_tool
def bw_get_query_auth_exposure(system: str, query: str) -> QueryAuthExposure | UnsupportedResult:
    """Whether a query returns different data per user, and on which characteristics.

    The question behind most BW audit findings: two people comparing figures from one report can
    both be right if it is restricted on an authorisation-relevant characteristic. Reports the
    characteristics in play and any authorisation-filled variables; it does **not** resolve what any
    individual sees, which needs a per-user value join.
    """
    return runtime().query_auth_exposure(system, query)


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
    """Structural anti-patterns: CP->DSO, CP->InfoObject, deep DSO stacks, circular dependencies.

    Circular dependencies are the severe ones: a transformation whose source and target are the same
    object, or a loop of any length where each object feeds the next. Both make the loaded result
    depend on load order, so a failed request cannot simply be re-run, and a loop has no correct
    order at all. Detected at any length, and each finding names every object involved.
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


# --- compound analysis tools -------------------------------------------------------------
#
# Each of these composes six or seven of the granular tools above into one answer, in the shape
# `models.analysis.Analysis` describes. They exist **alongside** the granular tools, not instead of
# them: a caller who knows exactly what they want should still ask for exactly that, and every
# section of a composed answer names the granular tool that reproduces it so any part can be checked
# on its own.
#
# What they add beyond saving round trips:
#
# * `steps` - an audit row per reader, with its status and the physical tables it read. A section
#   that is absent says whether it did not apply or the release could not report it.
# * `limitations` - what cannot be concluded, with a machine-readable reason, so "nothing found" and
#   "could not look" are never the same answer.
# * `confidence` - coverage and evidence basis as separate components, never one number.
# * a partial answer when the per-call budget runs out, rather than a BudgetResult and nothing else.


#: What a compound tool returns. Named because all five share it and each one also needs it as the
#: argument type of the shaper.
_AnalysisResult = Analysis | ObjectNotFound | UnsupportedResult


@_readonly_tool
def bw_analyze_object(
    system: str, name: str, depth: int = 2, *, detail: DetailLevel = "auto"
) -> _AnalysisResult:
    """Everything about one provider or InfoObject in a single auditable answer.

    Composes: definition and description, lineage both ways, downstream blast radius including
    routine-embedded consumers, volume and currency, the chains that load it, the reports that read
    it, and the calc views that read its generated table.

    Read ``confidence`` and ``limitations`` before the payloads. A section the release cannot report
    is named in ``steps`` with status ``unsupported`` rather than coming back empty, because "this
    object has no consumers" and "consumers cannot be read here" are different answers and only one
    of them is about the object.

    ``detail`` bounds the embedded field list and lineage graphs exactly as it does on
    ``bw_describe_object`` and ``bw_get_lineage``; counts stay exact either way.
    """
    result = runtime().analysis(system).analyze_object(name, depth=_clamp_depth(depth))
    return _shape_analysis(result, system=system, detail=detail)


@_readonly_tool
def bw_analyze_query(system: str, query: str, *, detail: DetailLevel = "auto") -> _AnalysisResult:
    """Everything about one BEx report: what it reads, who sees what, and when its data is current.

    Composes: the query definition and element tree, field-level lineage toward the DataSource,
    usage and decommission signal, authorisation exposure (whether two users legitimately see
    different numbers), and the cadence of the chain feeding its provider.

    ``query`` may be the technical name (COMPID) or the COMPUID. Customer-exit variables come back
    as a metadata dead end: they can be named, but their values resolve in ABAP at runtime.
    """
    return _shape_analysis(
        runtime().analysis(system).analyze_query(query), system=system, detail=detail
    )


@_readonly_tool
def bw_analyze_process_chain(system: str, chain_id: str, days: int = 90) -> _AnalysisResult:
    """Everything about one process chain: structure, reliability, what it loads, where it is weak.

    Composes: the chain with its nested sub-chains resolved, runtime statistics over the retained
    log window, and the providers it actually loads walked recursively through those sub-chains -
    which is where most loads live rather than in the top-level step list.

    Durations reflect contention where chains overlap, and observed overlaps are reported as a risk
    rather than the p95 being presented as a fixed property of the chain.
    """
    return runtime().analysis(system).analyze_process_chain(chain_id, days=max(1, min(days, 365)))


@_readonly_tool
def bw_assess_change_impact(
    system: str, name: str, depth: int = 3, *, detail: DetailLevel = "auto"
) -> _AnalysisResult:
    """What a change to this object reaches, and what to verify before transporting it.

    Composes the blast radius from three directions that each miss something the others catch:
    declared transformations downstream, routines that read the object (invisible to BW's own
    where-used list), reports that read it, and calc views that read its generated table - the last
    of which BW will not warn about at all, because the view reads the generated table directly.

    ``next_actions`` is the pre-transport checklist, generated from what was actually found rather
    than from a template, and it opens with taking a snapshot: comparing one afterwards is the only
    way to prove what the change altered, which a transport log does not say.
    """
    result = runtime().analysis(system).assess_change_impact(name, depth=_clamp_depth(depth))
    return _shape_analysis(result, system=system, detail=detail)


@_readonly_tool
def bw_troubleshoot_missing_data(
    system: str, target: str, *, detail: DetailLevel = "auto"
) -> _AnalysisResult:
    """Diagnose a report or provider showing wrong or missing data, layer by layer.

    Walks the layers in the order they actually explain incidents, which is not the order they are
    usually looked at. First what the object reads, then **whether the data arrived** (the request
    ledger: a failed or stale load explains missing rows directly), then whether the load that
    should have delivered it ran, and only then the transformation logic. Most incidents are
    answered by the second or third question, and opening with routine source spends the call on the
    least likely cause.

    For a query it also checks authorisation exposure, because a report restricted on an
    authorisation-relevant characteristic returns different rows per user - which presents exactly
    as missing data with no load fault anywhere.

    ``risks`` comes back ordered most severe first: work down it. Each entry names the object to
    inspect and cites the record it was derived from.
    """
    return _shape_analysis(
        runtime().analysis(system).troubleshoot_missing_data(target), system=system, detail=detail
    )


def _clamp_depth(depth: int) -> int:
    """A composed answer fans out per hop, so depth is bounded harder than on a granular tool."""
    return max(1, min(depth, _MAX_ANALYSIS_DEPTH))


def _shape_analysis(
    result: _AnalysisResult, *, system: str, detail: DetailLevel
) -> _AnalysisResult:
    """Apply the same field and graph bounds a granular tool applies, to each embedded payload.

    Measured against a live system: one object analysis came back at 79 KiB and a query analysis at
    90 KiB, dominated by a 171-field provider and two full lineage graphs. Context spent on that is
    context unavailable for reasoning, and a compound tool embeds several payloads at once - so the
    shaping matters more here than on the granular tools it composes, not less.

    The bounds are the *same* ones, deliberately: a caller who has learned what ``detail`` does to
    ``bw_describe_object`` should not have to learn a second rule. Counts stay exact, and a trimmed
    payload keeps its own caveat naming the resource URI holding the whole record.
    """
    if not isinstance(result, Analysis) or detail == "full":
        return result
    updates: dict[str, Any] = {}
    if result.definition is not None:
        updates["definition"] = _shape_provider(result.definition, system=system, detail=detail)
    if result.query is not None:
        updates["query"] = _shape_query(result.query, system=system, detail=detail)
    if result.query_lineage is not None:
        updates["query_lineage"] = _shape_query_lineage(
            result.query_lineage, system=system, detail=detail
        )
    if result.lineage is not None:
        updates["lineage"] = _shape_graph(result.lineage, detail=detail)
    if result.impact is not None:
        updates["impact"] = result.impact.model_copy(
            update={"graph": _shape_graph(result.impact.graph, detail=detail)}
        )
    if result.trace is not None:
        updates["trace"] = result.trace.model_copy(
            update={"graph": _shape_graph(result.trace.graph, detail=detail)}
        )
    return result.model_copy(update=updates) if updates else result


def _shape_query(query: Query, *, system: str, detail: DetailLevel) -> Query:
    """Bound a query's element tree, which dominates a composed answer.

    Measured live: a 39-element query analysis came back at 90 KiB, most of it the element tree and
    its edges. Applied only inside a composed answer - ``bw_get_query`` still returns the whole
    definition, because asking for one query on its own *is* a request for exactly that, while a
    composed answer has five other payloads competing for the same reply.
    """
    total = len(query.elements)
    if detail == "full" or (detail == "auto" and total <= _MAX_INLINE_ELEMENTS):
        return query
    kept = query.elements[:_MAX_INLINE_ELEMENTS]
    kept_ids = {element.eltuid for element in kept}
    return query.model_copy(
        update={
            "elements": kept,
            # Both endpoints must be retained, so the trimmed tree stays a valid subgraph rather
            # than a set of edges pointing at elements the reply no longer carries.
            "edges": [
                e for e in query.edges if e.parent_uid in kept_ids and e.child_uid in kept_ids
            ],
            "caveats": [
                *query.caveats,
                f"element tree summarised: {len(kept)} of {total} elements shown, with the edges "
                "between them. Read "
                f"{resource_uri(system, 'query', query.compid or query.compuid)} for the "
                "whole definition, or call bw_get_query, or call again with detail='full'.",
            ],
        }
    )


def _shape_query_lineage(
    lineage: QueryLineage, *, system: str, detail: DetailLevel
) -> QueryLineage:
    """Bound the field-path list, keeping the paths that actually resolved to a field.

    Which paths are kept is not arbitrary. A ``field`` resolution is the field's own derivation; a
    ``provider`` resolution is a fallback that looks like field lineage and is not. Keeping the
    resolved ones first means a trimmed answer loses the least informative paths rather than an
    arbitrary slice, and the counts of what was dropped stay in the caveat.
    """
    total = len(lineage.paths)
    if detail == "full" or (detail == "auto" and total <= _MAX_INLINE_PATHS):
        return lineage
    ranked = sorted(
        lineage.paths, key=lambda p: {"field": 0, "provider": 1, "none": 2}[p.resolution]
    )
    kept = ranked[:_MAX_INLINE_PATHS]
    dropped = total - len(kept)
    return lineage.model_copy(
        update={
            "paths": kept,
            "caveats": [
                *lineage.caveats,
                f"field paths summarised: {len(kept)} of {total} shown, resolved paths first, so "
                f"the {dropped} omitted are the least specific. Call bw_get_query_lineage for "
                "every path, or call again with detail='full'.",
            ],
        }
    )


# --- documentation generation tool -------------------------------------------------------


def _default_docs_dir(system: str) -> str:
    """Where a generated knowledge base lands when the caller names no directory.

    Scoped by tenant, because a documentation tree is the most obviously customer-specific thing
    this server writes: two landscapes both called ``prd`` would otherwise render into one and
    interleave, and generation only ever *adds* files, so the result would be a tree describing two
    systems at once with nothing saying so.

    Readability wins over collision-freedom here, unlike the cache and snapshot stores: a person
    opens this tree and reads it, and ``_safe_output_dir`` already refuses to write into a
    git-tracked location. Each segment is still sanitised so an alias cannot traverse.
    """
    identity = runtime().identity(system)
    parts = [_slug_for_file(part) for part in (identity.tenant, identity.system) if part]
    return "/".join(["output", "docs", *parts])


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
    target = output_dir or _default_docs_dir(system)
    return (
        runtime()
        .docgen(system)
        .generate(target, limit=limit, catalog_cap=cap, sections=sections, resume=resume)
    )


# --- resources (URI-addressable read-only context) ---------------------------------------
#
# Mission Section 4. Two purposes:
#
# 1. A client can pull one object into context by URI without spending a tool round-trip.
# 2. They are where a summarised tool response points. A wide provider has 150+ fields and a
#    lineage graph can carry a hundred edges; dumping that inline burns the model's context on
#    every call. The tool returns the shape plus a `resource_uri`, and the full record stays one
#    fetch away — nothing is lost, it is just no longer forced into every reply.
#
# Resources return JSON (FastMCP serialises non-str returns), and every one of them goes through
# the same repositories, budgets and read-only guard as the tools.
#
# **Identifiers must be percent-encoded.** A URI template expands a single path segment, and BW
# technical names contain slashes - a namespaced object is `/IRM/IP_O02`. Unencoded, the segment
# splits and the URI resolves to nothing; measured on the reference system that is 484 objects,
# including a fifth of the cube-table objects and a fifth of the active chains. Every URI this
# server emits goes through `resource_uri`, which encodes; a caller building one by hand has to do
# the same, so each template below says so.


def _resource_payload(value: Any) -> Any:
    """Serialise a model (or a structured error) for a resource read."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _readonly_resource(uri: str) -> Callable[[Callable[..., Any]], Any]:
    """Register a resource with the same budget and failure envelope the tools get.

    Without this a resource read that failed reached the client as ``Error reading resource
    'bw://...'`` and nothing more: no code, no category, no remedy, no retryable flag. The identical
    failure through the equivalent tool returns a structured :class:`BwError` a program can branch
    on. One surface answering usefully and the other opaquely is not a distinction a caller should
    have to know about, so both now go through ``from_exception``.

    Host names were never at risk here - ``mask_error_details=True`` on the FastMCP instance stops
    an exception message reaching the client - but relying on that meant relying on a framework
    default to satisfy a project rule. The envelope makes it explicit, and it applies the per-call
    budget the same way, so a resource read cannot run unbounded either.
    """

    def decorate(func: Callable[..., Any]) -> Any:
        @functools.wraps(func)
        def guarded(**kwargs: Any) -> Any:
            with query_budget(
                max_queries=_budget_limits()[0], max_seconds=_budget_limits()[1]
            ) as budget:
                try:
                    return _resource_payload(func(**kwargs))
                except BudgetExceeded as exc:
                    _LOG.warning("resource=%s stopped on budget: %s", uri, exc.reason)
                    return BudgetResult(
                        tool=uri,
                        reason=exc.reason,
                        queries_spent=exc.queries,
                        elapsed_seconds=exc.elapsed_seconds,
                        budget=cast("dict[str, Any]", budget.snapshot()),
                    ).model_dump(mode="json")
                except Exception as exc:
                    failure = from_exception(exc, tool=uri)
                    _LOG.warning(
                        "resource=%s failed: code=%s (%s)",
                        uri,
                        failure.code,
                        type(exc).__name__,
                    )
                    return failure.model_dump(mode="json")

        return mcp.resource(uri, mime_type="application/json")(guarded)

    return decorate


def resource_uri(system: str, kind: str, identifier: str) -> str:
    """Build a ``bw://`` URI whose identifier survives being a BW technical name.

    **This exists because BW names contain slashes.** A namespaced object is called ``/IRM/IP_O02``,
    and a URI template expands one path segment, so an unencoded name splits the segment and the URI
    resolves to nothing. Measured on the reference system: 484 objects are affected, including 29 of
    143 cube-table objects (20%) and 57 of 280 active chains (20%).

    That mattered in practice, not in theory. A summarised response cites the resource holding the
    full record, and for a fifth of the chains and cubes on a real system that citation was a URI
    the client could not read - a pointer into nothing, in the one place a caller is told to go for
    what was left out.

    Encoding with nothing safe is what makes the identifier opaque to the template. FastMCP decodes
    it before the resource function runs, so the reader still receives ``/IRM/IP_O02``.
    """
    return f"bw://{quote(system, safe='')}/{kind}/{quote(identifier, safe='')}"


@_readonly_resource("bw://{system}/profile")
def resource_profile(system: str) -> Any:
    """Release, ABAP schema, object-model variants and table availability for a system."""
    return runtime().capability(system)


@_readonly_resource("bw://{system}/catalog")
def resource_catalog(system: str) -> Any:
    """Object counts per type — the system's shape at a glance."""
    record = runtime().capability(system)
    return {
        "system": system,
        "bw_release": record.bw_release,
        "object_models": dict(record.object_models),
        "tables": {
            name: {"resolved_name": status.resolved_name, "rows": status.row_estimate}
            for name, status in record.tables.items()
            if status.present
        },
    }


@_readonly_resource("bw://{system}/chain/{chain_id}")
def resource_chain(system: str, chain_id: str) -> Any:
    """One process chain: processes, event-linked edges, nested sub-chains resolved.

    Percent-encode the chain id: 57 of the 280 active chains on the reference system are namespaced
    (``/CPMB/ADMINTASK_MAKEDIM``), and an unencoded slash splits the URI segment.
    """
    return runtime().chains(system).get_chain(chain_id)


@_readonly_resource("bw://{system}/provider/{name}")
def resource_provider(system: str, name: str) -> Any:
    """One InfoProvider or InfoObject in full, including every field.

    Percent-encode the name: a namespaced provider is ``/IRM/IP_O02``, and an unencoded slash splits
    the URI segment so the read resolves to nothing.
    """
    return runtime().providers(system).describe(name)


@_readonly_resource("bw://{system}/transformation/{tran_id}")
def resource_transformation(system: str, tran_id: str) -> Any:
    """One transformation: header, field mappings, rule types, routine references.

    A TRANID is a generated identifier and carries no slash on the reference system, but
    percent-encode it anyway rather than relying on that.
    """
    return runtime().transformations(system).get_transformation(tran_id)


@_readonly_resource("bw://{system}/query/{query_id}")
def resource_query(system: str, query_id: str) -> Any:
    """One BEx query: element tree, restrictions, calculated key figures, variables.

    Accepts the COMPID or the COMPUID. Percent-encode it: a COMPID can be namespaced
    (``/IMO/V_MMIM01_Q0001``), and an ad-hoc query's name begins ``!!``.
    """
    return runtime().queries(system).get_query(query_id)


@_readonly_resource("bw://{system}/calcview/{view_name}")
def resource_calcview(system: str, view_name: str) -> Any:
    """One calc view: base tables resolved to BW objects, and the providers consuming it.

    Percent-encode the view name: a generated BW view is ``0BW:BIA:<PROVIDER>`` and a modelled one
    carries its package path, so both bring characters a URI segment has to escape.
    """
    return runtime().hana(system).get_calc_view_lineage(view_name)


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
    configure_logging()  # stderr only: stdout carries the MCP protocol
    try:
        mcp.run()
    finally:
        _shutdown()


def _shutdown() -> None:
    """Release database sessions and cache handles on exit rather than relying on process death."""
    holder = _runtime_holder.get("runtime")
    closer = getattr(holder, "close", None)
    if callable(closer):
        with suppress(Exception):
            closer()


if __name__ == "__main__":
    main()
