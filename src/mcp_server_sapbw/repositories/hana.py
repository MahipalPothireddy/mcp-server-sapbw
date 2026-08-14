"""HANA-layer repository (B8).

Reads calc views from the HANA catalog (``SYS.VIEWS`` in the ``_SYS_BIC`` schema) and their
dependencies from ``SYS.OBJECT_DEPENDENCIES`` (DEPENDENCY_TYPE=1 = direct, validated live in B8),
resolving BW-generated ``/BIC/`` / ``/BI0/`` base tables back to BW objects (advisory, reusing the
routine parser's resolver). Builds the bidirectional BW<->HANA crossing table. The SYS catalog
views are OBJVERS-free and schema-qualified as ``SYS`` via the capability record.

BW also generates a HANA view per InfoProvider in the ABAP schema, named
``0BW:BIA:<PROVIDER>`` with internal nodes suffixed ``:<node>`` / ``.<node>`` (e.g.
``0BW:BIA:<CP>:J1.CALC.1``). Those views are what appear on the BW side of a ``bw_reads_hana``
crossing, so parsing the provider out of the name (:func:`_bw_view_provider`) and confirming its
type against the provider header tables (:meth:`HanaRepository.resolve_bw_view_providers`) is what
maps a calc view to the **CompositeProvider that consumes it** — the boundary hop BW's own
where-used lists do not show. The provider name is parsed from the naming convention; its *type* is
verified by lookup, not guessed.
"""

from __future__ import annotations

from typing import Any, Literal

from ..core.dialect import quote_ident
from ..models.hana import (
    BaseTableRef,
    BwProviderView,
    CalcView,
    CalcViewLineage,
    CalcViewType,
    CrossingDirection,
    HanaCrossing,
    HanaCrossingReport,
)
from ..models.provenance import UnsupportedResult
from ..models.providers import classify_cube_type
from ..services.routine_parser import _resolve_bw_table
from .base import Repository

# _SYS_BIC view types we treat as calc views (HIERARCHY views are BW hierarchy runtime, excluded).
_CALC_VIEW_TYPES = ("CALC", "JOIN", "OLAP")
_VIEW_TYPE_MAP: dict[str, CalcViewType] = {
    "CALC": "calc",
    "JOIN": "join",
    "OLAP": "olap",
    "HIERARCHY": "hierarchy",
}
_CALC_SCHEMA = "_SYS_BIC"
_DIRECT = 1  # SYS.OBJECT_DEPENDENCIES.DEPENDENCY_TYPE = direct dependency
_MAX_BASE_TABLES = 500
_MAX_CONSUMING = 5000
_MAX_PROVIDER_CONSUMERS = 200  # BW provider views resolved per calc-view lineage call

# BW-generated per-InfoProvider HANA view: '0BW:BIA:<PROVIDER>' plus optional ':node' / '.node'.
_BW_VIEW_PREFIX = "0BW:BIA:"
# Provider header tables probed to confirm a parsed provider name, most specific first.
# (logical table, id column, kind) — 'cube_header' is refined by CUBETYPE.
_PROVIDER_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("composite_header", "HCPRNM", "compositeprovider"),
    ("adso_header", "ADSONM", "adso"),
    ("dso_header", "ODSOBJECT", "dso"),
    ("cube_header", "INFOCUBE", "infocube"),
)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_bw_generated(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(("/BIC/", "/BI0/"))


def _bw_view_provider(name: str) -> str | None:
    """Provider name inside a BW-generated view name (``0BW:BIA:<PROVIDER>[:node][.node]``).

    Returns ``None`` for anything that is not a ``0BW:BIA:`` view (including BW's internal helper
    views such as the lower-cased pruning views, which name no provider).
    """
    upper = name.strip().upper()
    if not upper.startswith(_BW_VIEW_PREFIX):
        return None
    rest = upper[len(_BW_VIEW_PREFIX) :]
    for separator in (":", "."):
        rest = rest.partition(separator)[0]
    return rest.strip() or None


class HanaRepository(Repository):
    """Calc-view catalog, calc-view -> base-table lineage, and BW<->HANA crossings."""

    # --- listing -------------------------------------------------------------------------

    def list_calc_views(
        self, *, bw_consuming_only: bool = False, limit: int = 100, offset: int = 0
    ) -> tuple[list[CalcView], int] | UnsupportedResult:
        unsupported = self.require("hana_views")
        if unsupported is not None:
            return unsupported

        types = ", ".join(f"'{t}'" for t in _CALC_VIEW_TYPES)
        where = ["SCHEMA_NAME = ?", f"VIEW_TYPE IN ({types})"]
        params: list[Any] = [_CALC_SCHEMA]
        consuming_filter = self._consuming_subquery()
        if bw_consuming_only and consuming_filter:
            where.append(f"VIEW_NAME IN ({consuming_filter})")
            params.append(self.capability.abap_schema)

        base = self.dialect.build_select(
            columns=["VIEW_NAME", "VIEW_TYPE"],
            from_logical="hana_views",
            where=where,
            params=params,
            order_by=["VIEW_NAME"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))

        consuming = set() if bw_consuming_only else self._consuming_names()
        views: list[CalcView] = []
        for name_raw, view_type in rows:
            name = _clean(name_raw)
            if name is None:
                continue
            views.append(
                CalcView(
                    name=name,
                    view_type=_VIEW_TYPE_MAP.get(str(view_type).strip(), "other"),
                    is_bw_consuming=True if bw_consuming_only else name in consuming,
                    provenance=self.provenance(
                        "hana_views", {"SCHEMA_NAME": _CALC_SCHEMA, "VIEW_NAME": name}
                    ),
                )
            )
        return views, total

    # --- calc-view lineage ---------------------------------------------------------------

    def get_calc_view_lineage(self, view_name: str) -> CalcViewLineage | UnsupportedResult:
        """Base tables and consuming providers for a calc view. Cached (scope ``calc_view``)."""
        return self.cached_model(
            "calc_view",
            view_name,
            model=CalcViewLineage,
            build=lambda: self._get_calc_view_lineage_uncached(view_name),
            # An unknown view resolves to nothing; a view activated later must not read as empty.
            cache_when=lambda lineage: bool(lineage.base_tables or lineage.consuming_bw_providers),
        )

    def _get_calc_view_lineage_uncached(
        self, view_name: str
    ) -> CalcViewLineage | UnsupportedResult:
        unsupported = self.require("object_dependencies")
        if unsupported is not None:
            return unsupported
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["BASE_SCHEMA_NAME", "BASE_OBJECT_NAME", "BASE_OBJECT_TYPE"],
                    from_logical="object_dependencies",
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "DEPENDENT_OBJECT_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                    ],
                    params=[_CALC_SCHEMA, view_name, _DIRECT],
                ),
                limit=_MAX_BASE_TABLES + 1,
            )
        )
        truncated = len(rows) > _MAX_BASE_TABLES
        base_tables: list[BaseTableRef] = []
        resolved: list[str] = []
        seen: set[str] = set()
        for base_schema, base_object, base_type in rows[:_MAX_BASE_TABLES]:
            table = _clean(base_object)
            if table is None or table in seen:
                continue
            seen.add(table)
            is_bw = _is_bw_generated(table)
            obj, kind = _resolve_bw_table(table) if is_bw else (None, None)
            if obj and obj not in resolved:
                resolved.append(obj)
            base_tables.append(
                BaseTableRef(
                    table=table,
                    schema_name=_clean(base_schema),
                    object_type=_clean(base_type),
                    is_bw_generated=is_bw,
                    resolved_object=obj,
                    resolved_kind=kind,
                    provenance=self.provenance(
                        "object_dependencies",
                        {"DEPENDENT_OBJECT_NAME": view_name, "BASE_OBJECT_NAME": table},
                    ),
                )
            )
        consumers, consumers_truncated = self._consuming_bw_providers(view_name)
        caveats = ["/BIC/ and /BI0/ base-table resolution to BW objects is advisory (naming-based)"]
        if consumers:
            caveats.append(
                "consuming BW providers are read from the '0BW:BIA:<PROVIDER>' generated views; "
                "the provider name is parsed from the view name and its type confirmed against the "
                "provider header tables (unverified entries are flagged)"
            )
        if truncated:
            caveats.append(f"base-table list capped at {_MAX_BASE_TABLES}")
        if consumers_truncated:
            caveats.append(f"consuming-provider list capped at {_MAX_PROVIDER_CONSUMERS}")
        return CalcViewLineage(
            view_name=view_name,
            base_tables=base_tables,
            resolved_bw_objects=resolved,
            consuming_bw_providers=consumers,
            truncated=truncated or consumers_truncated,
            caveats=caveats,
            provenance=self.provenance(
                "object_dependencies",
                {"DEPENDENT_SCHEMA_NAME": _CALC_SCHEMA, "DEPENDENT_OBJECT_NAME": view_name},
            ),
        )

    # --- crossings -----------------------------------------------------------------------

    def get_hana_crossings(
        self, *, calc_view: str | None = None, limit: int = 100, offset: int = 0
    ) -> HanaCrossingReport | UnsupportedResult:
        unsupported = self.require("object_dependencies")
        if unsupported is not None:
            return unsupported
        abap = self.capability.abap_schema

        hana_reads = self._hana_reads_bw(abap, calc_view, limit, offset)
        bw_reads = self._bw_reads_hana(abap, calc_view, limit, offset)
        crossings = hana_reads[0] + bw_reads[0]
        caveats = ["only direct dependencies (DEPENDENCY_TYPE=1) are included"]
        if hana_reads[2] or bw_reads[2]:
            caveats.append("crossing list truncated per direction; totals are exact")
        return HanaCrossingReport(
            crossings=crossings,
            total_count=hana_reads[1] + bw_reads[1],
            hana_reads_bw_count=hana_reads[1],
            bw_reads_hana_count=bw_reads[1],
            truncated=hana_reads[2] or bw_reads[2],
            caveats=caveats,
        )

    def _hana_reads_bw(
        self, abap: str, calc_view: str | None, limit: int, offset: int
    ) -> tuple[list[HanaCrossing], int, bool]:
        where = [
            "DEPENDENT_SCHEMA_NAME = ?",
            "BASE_SCHEMA_NAME = ?",
            "DEPENDENCY_TYPE = ?",
            "(BASE_OBJECT_NAME LIKE '/BIC/%' OR BASE_OBJECT_NAME LIKE '/BI0/%')",
        ]
        params: list[Any] = [_CALC_SCHEMA, abap, _DIRECT]
        if calc_view:
            where.append("DEPENDENT_OBJECT_NAME = ?")
            params.append(calc_view)
        base = self.dialect.build_select(
            columns=["DEPENDENT_OBJECT_NAME", "BASE_OBJECT_NAME", "BASE_OBJECT_TYPE"],
            from_logical="object_dependencies",
            where=where,
            params=params,
            order_by=["DEPENDENT_OBJECT_NAME", "BASE_OBJECT_NAME"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        # This direction is filtered to /BIC/ and /BI0/ base tables in SQL, so no BW provider views.
        crossings = [
            self._crossing("hana_reads_bw", str(dep), str(base_obj), str(base_type), {})
            for dep, base_obj, base_type in rows
        ]
        return crossings, total, total > offset + limit

    def _bw_reads_hana(
        self, abap: str, calc_view: str | None, limit: int, offset: int
    ) -> tuple[list[HanaCrossing], int, bool]:
        where = ["BASE_SCHEMA_NAME = ?", "DEPENDENT_SCHEMA_NAME = ?", "DEPENDENCY_TYPE = ?"]
        params: list[Any] = [_CALC_SCHEMA, abap, _DIRECT]
        if calc_view:
            where.append("BASE_OBJECT_NAME = ?")
            params.append(calc_view)
        base = self.dialect.build_select(
            columns=["BASE_OBJECT_NAME", "DEPENDENT_OBJECT_NAME", "DEPENDENT_OBJECT_TYPE"],
            from_logical="object_dependencies",
            where=where,
            params=params,
            order_by=["BASE_OBJECT_NAME", "DEPENDENT_OBJECT_NAME"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        # The BW side here is a BW-generated object; '0BW:BIA:<PROVIDER>' views resolve to the
        # consuming InfoProvider (this is the calc-view -> CompositeProvider hop).
        provider_views = self.resolve_bw_view_providers([str(dep) for _, dep, _ in rows])
        crossings = [
            self._crossing("bw_reads_hana", str(base_obj), str(dep), str(dep_type), provider_views)
            for base_obj, dep, dep_type in rows
        ]
        return crossings, total, total > offset + limit

    def _crossing(
        self,
        direction: CrossingDirection,
        hana_object: str,
        bw_object: str,
        object_type: str,
        provider_views: dict[str, BwProviderView],
    ) -> HanaCrossing:
        resolution: Literal["bic_table", "bw_provider_view", "unresolved"] = "unresolved"
        resolved: str | None = None
        kind: str | None = None
        if _is_bw_generated(bw_object):
            resolved, kind = _resolve_bw_table(bw_object)
            if resolved is not None:
                resolution = "bic_table"
        elif (view := provider_views.get(bw_object)) is not None:
            resolved, kind = view.provider, view.resolved_kind
            resolution = "bw_provider_view"
        return HanaCrossing(
            direction=direction,
            hana_object=hana_object,
            bw_object=bw_object,
            bw_object_resolved=resolved,
            bw_object_kind=kind,
            resolution=resolution,
            object_type=_clean(object_type),
            provenance=self.provenance(
                "object_dependencies", {"HANA": hana_object, "BW": bw_object}
            ),
        )

    # --- BW provider views (0BW:BIA:) ----------------------------------------------------

    def _consuming_bw_providers(self, view_name: str) -> tuple[list[BwProviderView], bool]:
        """InfoProviders whose generated views read this calc view (the BW side of the boundary)."""
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DEPENDENT_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    where=[
                        "BASE_SCHEMA_NAME = ?",
                        "BASE_OBJECT_NAME = ?",
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                        f"DEPENDENT_OBJECT_NAME LIKE '{_BW_VIEW_PREFIX}%'",
                    ],
                    params=[
                        _CALC_SCHEMA,
                        view_name,
                        self.capability.abap_schema,
                        _DIRECT,
                    ],
                    order_by=["DEPENDENT_OBJECT_NAME"],
                ),
                limit=_MAX_PROVIDER_CONSUMERS + 1,
            )
        )
        truncated = len(rows) > _MAX_PROVIDER_CONSUMERS
        names = [str(r[0]) for r in rows[:_MAX_PROVIDER_CONSUMERS]]
        resolved = self.resolve_bw_view_providers(names)
        # One entry per provider: the internal ':J1.CALC.n' nodes all name the same provider.
        by_provider: dict[str, BwProviderView] = {}
        for name in names:
            view = resolved.get(name)
            if view is not None:
                by_provider.setdefault(view.provider, view)
        return sorted(by_provider.values(), key=lambda v: v.provider), truncated

    def resolve_bw_view_providers(self, view_names: list[str]) -> dict[str, BwProviderView]:
        """Resolve ``0BW:BIA:`` view names to the InfoProviders that own them.

        The provider name is parsed from the view name; its **type** is then confirmed against the
        provider header tables present on this release, so the returned kind is verified metadata
        rather than a naming guess. Views whose provider name matches no header row are still
        returned, with ``resolved_kind=None`` and ``verified=False``.
        """
        parsed: dict[str, str] = {}
        for view in view_names:
            provider = _bw_view_provider(view)
            if provider is not None:
                parsed[view] = provider
        if not parsed:
            return {}
        kinds = self._provider_kinds(sorted(set(parsed.values())))
        resolved: dict[str, BwProviderView] = {}
        for view, provider in parsed.items():
            kind = kinds.get(provider)
            resolved[view] = BwProviderView(
                view_name=view,
                provider=provider,
                resolved_kind=kind,
                verified=kind is not None,
                provenance=self.provenance(
                    "object_dependencies", {"BW_VIEW": view, "PROVIDER": provider}
                ),
            )
        return resolved

    def _provider_kinds(self, providers: list[str]) -> dict[str, str]:
        """Confirm provider names against the header tables, returning name -> provider kind."""
        remaining = list(providers)
        kinds: dict[str, str] = {}
        for logical, id_column, kind in _PROVIDER_SOURCES:
            if not remaining or not self.capability.is_available(logical):
                continue
            is_cube = logical == "cube_header"
            columns = [id_column, "CUBETYPE"] if is_cube else [id_column]
            placeholders = ", ".join("?" for _ in remaining)
            rows = self.select(
                self.dialect.build_select(
                    columns=columns,
                    from_logical=logical,
                    where=[f"{id_column} IN ({placeholders})"],
                    params=list(remaining),
                )
            )
            for row in rows:
                name = _clean(row[0])
                if name is None or name in kinds:
                    continue
                kinds[name] = classify_cube_type(row[1]) if is_cube else kind
            remaining = [name for name in remaining if name not in kinds]
        return kinds

    # --- helpers -------------------------------------------------------------------------

    def _consuming_subquery(self) -> str | None:
        """A ``SELECT DEPENDENT_OBJECT_NAME ...`` fragment naming BW-consuming calc views."""
        status = self.capability.table("object_dependencies")
        if status is None or not status.present or status.resolved_name is None:
            return None
        ref = f"{quote_ident('SYS')}.{quote_ident(status.resolved_name)}"
        return (
            f"SELECT DEPENDENT_OBJECT_NAME FROM {ref} "
            f"WHERE DEPENDENT_SCHEMA_NAME = '{_CALC_SCHEMA}' AND BASE_SCHEMA_NAME = ? "
            f"AND DEPENDENCY_TYPE = {_DIRECT} "
            "AND (BASE_OBJECT_NAME LIKE '/BIC/%' OR BASE_OBJECT_NAME LIKE '/BI0/%')"
        )

    def _consuming_names(self) -> set[str]:
        if not self.capability.is_available("object_dependencies"):
            return set()
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DEPENDENT_OBJECT_NAME"],
                    from_logical="object_dependencies",
                    where=[
                        "DEPENDENT_SCHEMA_NAME = ?",
                        "BASE_SCHEMA_NAME = ?",
                        "DEPENDENCY_TYPE = ?",
                        "(BASE_OBJECT_NAME LIKE '/BIC/%' OR BASE_OBJECT_NAME LIKE '/BI0/%')",
                    ],
                    params=[_CALC_SCHEMA, self.capability.abap_schema, _DIRECT],
                ),
                limit=_MAX_CONSUMING,
            )
        )
        return {str(r[0]).strip() for r in rows if _clean(r[0])}

    def _count(self, base: Any) -> int:
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0
