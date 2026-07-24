"""HANA-layer repository (B8).

Reads calc views from the HANA catalog (``SYS.VIEWS`` in the ``_SYS_BIC`` schema) and their
dependencies from ``SYS.OBJECT_DEPENDENCIES`` (DEPENDENCY_TYPE=1 = direct, validated live in B8),
resolving BW-generated ``/BIC/`` / ``/BI0/`` base tables back to BW objects (advisory, reusing the
routine parser's resolver). Builds the bidirectional BW<->HANA crossing table. The SYS catalog
views are OBJVERS-free and schema-qualified as ``SYS`` via the capability record.
"""

from __future__ import annotations

from typing import Any

from ..core.dialect import quote_ident
from ..models.hana import (
    BaseTableRef,
    CalcView,
    CalcViewLineage,
    CalcViewType,
    CrossingDirection,
    HanaCrossing,
    HanaCrossingReport,
)
from ..models.provenance import UnsupportedResult
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


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_bw_generated(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(("/BIC/", "/BI0/"))


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
        caveats = ["/BIC/ and /BI0/ base-table resolution to BW objects is advisory (naming-based)"]
        if truncated:
            caveats.append(f"base-table list capped at {_MAX_BASE_TABLES}")
        return CalcViewLineage(
            view_name=view_name,
            base_tables=base_tables,
            resolved_bw_objects=resolved,
            truncated=truncated,
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
        crossings = [
            self._crossing("hana_reads_bw", str(dep), str(base_obj), str(base_type))
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
        crossings = [
            self._crossing("bw_reads_hana", str(base_obj), str(dep), str(dep_type))
            for base_obj, dep, dep_type in rows
        ]
        return crossings, total, total > offset + limit

    def _crossing(
        self, direction: CrossingDirection, hana_object: str, bw_object: str, object_type: str
    ) -> HanaCrossing:
        resolved, kind = (
            _resolve_bw_table(bw_object) if _is_bw_generated(bw_object) else (None, None)
        )
        return HanaCrossing(
            direction=direction,
            hana_object=hana_object,
            bw_object=bw_object,
            bw_object_resolved=resolved,
            bw_object_kind=kind,
            object_type=_clean(object_type),
            provenance=self.provenance(
                "object_dependencies", {"HANA": hana_object, "BW": bw_object}
            ),
        )

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
