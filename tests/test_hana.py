"""Tests for the HANA-layer repository (B8), offline against scripted SYS.* fixtures.

Synthetic names only; the /BIC/ and /BI0/ base tables are built by concatenation so this .py file
stays clean for the customer-metadata scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.hana import HanaRepository

ABAP = "SAPABAP1"
_TABLES = {"object_dependencies": "OBJECT_DEPENDENCIES", "hana_views": "VIEWS"}

_BIC_DSO = "/BIC/" + "ASALES00"  # -> resolves to DSO SALES
_BI0_IOBJ = "/BI0/" + "PMATERIAL"  # -> resolves to InfoObject MATERIAL

_CALC_VIEWS = [("CV_SALES", "CALC"), ("CV_FIN", "CALC"), ("CV_LEGACY", "JOIN")]
_CONSUMING = {"CV_SALES", "CV_FIN"}
# hana_reads_bw: (dependent calc view, base object, base type)
_HANA_READS = [
    ("CV_SALES", _BIC_DSO, "TABLE"),
    ("CV_SALES", _BI0_IOBJ, "TABLE"),
    ("CV_FIN", _BIC_DSO, "TABLE"),
]
# bw_reads_hana: (base calc view, dependent object, dependent type)
_BW_READS = [("CV_SALES", "SALES_COMPAT_VIEW", "VIEW")]


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if '"VIEWS"' in sql:  # list query FROM "SYS"."VIEWS" (OBJECT_DEPENDENCIES only in subquery)
            return self._views(sql)
        if "OBJECT_DEPENDENCIES" in sql:
            return self._objdep(sql, params)
        return []

    @staticmethod
    def _views(sql: str) -> list[tuple[Any, ...]]:
        consuming_only = "DEPENDENT_OBJECT_NAME" in sql  # the bw-consuming subquery marker
        views = [v for v in _CALC_VIEWS if (not consuming_only or v[0] in _CONSUMING)]
        if "TOTAL_COUNT" in sql:
            return [(len(views),)]
        return views

    @staticmethod
    def _objdep(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        count = "TOTAL_COUNT" in sql
        if "DEPENDENT_OBJECT_TYPE" in sql:  # bw_reads_hana
            rows = [(b, d, t) for b, d, t in _BW_READS]
            return [(len(rows),)] if count else rows
        if "LIKE" in sql and "BASE_OBJECT_TYPE" in sql:  # hana_reads_bw
            rows = [(d, b, t) for d, b, t in _HANA_READS]
            return [(len(rows),)] if count else rows
        if "LIKE" in sql:  # consuming_names (SELECT DEPENDENT_OBJECT_NAME only)
            names = sorted(_CONSUMING)
            return [(len(names),)] if count else [(n,) for n in names]
        if "DEPENDENT_OBJECT_NAME = ?" in sql:  # calc-view lineage (base tables of a view)
            view = str(params[1])
            return [("SAPABAP1", b, t) for d, b, t in _HANA_READS if d == view]
        return [(0,)] if count else []


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=ABAP,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name="SYS" if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _repo(present: set[str] | None = None) -> HanaRepository:
    return HanaRepository(ScriptedConnection(), _capability(present))


def test_list_calc_views_marks_bw_consuming() -> None:
    result = _repo().list_calc_views()
    assert not isinstance(result, UnsupportedResult)
    views, total = result
    assert total == 3
    by_name = {v.name: v for v in views}
    assert by_name["CV_SALES"].view_type == "calc"
    assert by_name["CV_LEGACY"].view_type == "join"
    assert by_name["CV_SALES"].is_bw_consuming is True
    assert by_name["CV_LEGACY"].is_bw_consuming is False


def test_list_calc_views_bw_consuming_only() -> None:
    result = _repo().list_calc_views(bw_consuming_only=True)
    assert not isinstance(result, UnsupportedResult)
    views, total = result
    assert {v.name for v in views} == {"CV_SALES", "CV_FIN"}
    assert total == 2
    assert all(v.is_bw_consuming for v in views)


def test_get_calc_view_lineage_resolves_bw_objects() -> None:
    lineage = _repo().get_calc_view_lineage("CV_SALES")
    assert not isinstance(lineage, UnsupportedResult)
    tables = {b.table for b in lineage.base_tables}
    assert {_BIC_DSO, _BI0_IOBJ} <= tables
    dso = next(b for b in lineage.base_tables if b.table == _BIC_DSO)
    assert dso.is_bw_generated is True
    assert dso.resolved_object == "SALES"
    assert dso.resolved_kind == "dso"
    assert {"SALES", "MATERIAL"} <= set(lineage.resolved_bw_objects)


def test_get_hana_crossings_both_directions() -> None:
    report = _repo().get_hana_crossings()
    assert not isinstance(report, UnsupportedResult)
    assert report.hana_reads_bw_count == 3
    assert report.bw_reads_hana_count == 1
    directions = {c.direction for c in report.crossings}
    assert directions == {"hana_reads_bw", "bw_reads_hana"}
    hr = next(c for c in report.crossings if c.direction == "hana_reads_bw")
    assert hr.hana_object.startswith("CV_")
    assert hr.bw_object_resolved in {"SALES", "MATERIAL"}
    br = next(c for c in report.crossings if c.direction == "bw_reads_hana")
    assert br.hana_object == "CV_SALES"
    assert br.bw_object == "SALES_COMPAT_VIEW"


def test_unsupported_without_object_dependencies() -> None:
    result = _repo(present={"hana_views"}).get_calc_view_lineage("CV_SALES")
    assert isinstance(result, UnsupportedResult)
