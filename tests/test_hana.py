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
from mcp_server_sapbw.repositories.hana import HanaRepository, _bw_view_provider

ABAP = "SAPABAP1"
_SYS_TABLES = {"object_dependencies": "OBJECT_DEPENDENCIES", "hana_views": "VIEWS"}
# Provider header tables, used to confirm the provider parsed out of a 0BW:BIA: view name.
_ABAP_TABLES = {
    "composite_header": "RSOHCPR",
    "adso_header": "RSOADSO",
    "dso_header": "RSDODSO",
    "cube_header": "RSDCUBE",
}
_TABLES = {**_SYS_TABLES, **_ABAP_TABLES}

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
# BW-generated per-provider views (0BW:BIA:<PROVIDER>[:node][.node]) reading a calc view. The two
# SALES_CP nodes are the same provider seen through different internal calc nodes.
_CP_NODE = "0BW:BIA:SALES_CP:J1.CALC.1"
_CP_NODE_CONV = "0BW:BIA:SALES_CP:J1.CALC.1.CONV0000"
_ADSO_VIEW = "0BW:BIA:SALES_ADSO"
_GHOST_VIEW = "0BW:BIA:GONE_PROV"  # parses, but matches no provider header row
# bw_reads_hana: (base calc view, dependent object, dependent type)
_BW_READS = [
    ("CV_SALES", "SALES_COMPAT_VIEW", "VIEW"),
    ("CV_SALES", _CP_NODE, "VIEW"),
    ("CV_SALES", _CP_NODE_CONV, "VIEW"),
    ("CV_SALES", _ADSO_VIEW, "VIEW"),
    ("CV_SALES", _GHOST_VIEW, "VIEW"),
]
# Provider header contents: which names exist, per header table.
_PROVIDERS: dict[str, list[str]] = {
    "RSOHCPR": ["SALES_CP"],
    "RSOADSO": ["SALES_ADSO"],
    "RSDODSO": ["SALES_DSO"],
    "RSDCUBE": ["SALES_MP"],
}
_CUBETYPE = {"SALES_MP": "M"}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if '"VIEWS"' in sql:  # list query FROM "SYS"."VIEWS" (OBJECT_DEPENDENCIES only in subquery)
            return self._views(sql)
        if "OBJECT_DEPENDENCIES" in sql:
            return self._objdep(sql, params)
        for table, names in _PROVIDERS.items():
            if table in sql:  # provider-name confirmation (IN (...) batch)
                wanted = {str(p) for p in params}
                hits = [n for n in names if n in wanted]
                if table == "RSDCUBE":
                    return [(n, _CUBETYPE.get(n, "B")) for n in hits]
                return [(n,) for n in hits]
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
        if "LIKE" in sql and "BASE_OBJECT_NAME = ?" in sql:  # BW provider views on one calc view
            view = str(params[1])
            return [(d,) for b, d, _t in _BW_READS if b == view and d.startswith("0BW:BIA:")]
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
                schema_name=("SYS" if logical in _SYS_TABLES else ABAP)
                if logical in present
                else None,
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
    # "0MATERIAL", not "MATERIAL": a /BI0/ table name drops the SAP object's leading 0.
    assert {"SALES", "0MATERIAL"} <= set(lineage.resolved_bw_objects)


def test_get_hana_crossings_both_directions() -> None:
    report = _repo().get_hana_crossings()
    assert not isinstance(report, UnsupportedResult)
    assert report.hana_reads_bw_count == 3
    assert report.bw_reads_hana_count == len(_BW_READS)
    directions = {c.direction for c in report.crossings}
    assert directions == {"hana_reads_bw", "bw_reads_hana"}
    hr = next(c for c in report.crossings if c.direction == "hana_reads_bw")
    assert hr.hana_object.startswith("CV_")
    assert hr.bw_object_resolved in {"SALES", "MATERIAL"}
    assert hr.resolution == "bic_table"
    br = next(
        c for c in report.crossings if c.bw_object == "SALES_COMPAT_VIEW"
    )  # a plain compat view
    assert br.hana_object == "CV_SALES"
    assert br.resolution == "unresolved"
    assert br.bw_object_resolved is None


# --- BW provider views (0BW:BIA:) --------------------------------------------------------


def test_bw_view_provider_parsing() -> None:
    assert _bw_view_provider("0BW:BIA:SALES_CP") == "SALES_CP"
    assert _bw_view_provider(_CP_NODE) == "SALES_CP"  # ':J1.CALC.1' internal node
    assert _bw_view_provider(_CP_NODE_CONV) == "SALES_CP"  # '.CONV0000' suffix
    assert _bw_view_provider("0BW:BIA:SALES_CP.0BW:BIA:SALES_CP") == "SALES_CP"  # self-qualified
    assert _bw_view_provider("0bw:bia:sales_cp") == "SALES_CP"  # case-insensitive prefix
    assert _bw_view_provider("0bw:pruning:helper") is None  # internal helper, names no provider
    assert _bw_view_provider("SALES_COMPAT_VIEW") is None
    assert _bw_view_provider("0BW:BIA:") is None


def test_crossing_resolves_composite_provider_behind_a_calc_view() -> None:
    """The calc-view -> CompositeProvider hop: BW's own where-used lists do not report it."""
    report = _repo().get_hana_crossings()
    assert not isinstance(report, UnsupportedResult)
    node = next(c for c in report.crossings if c.bw_object == _CP_NODE)
    assert node.hana_object == "CV_SALES"
    assert node.bw_object_resolved == "SALES_CP"
    assert node.bw_object_kind == "compositeprovider"  # confirmed against RSOHCPR, not guessed
    assert node.resolution == "bw_provider_view"


def test_crossing_resolves_adso_provider_view() -> None:
    report = _repo().get_hana_crossings()
    assert not isinstance(report, UnsupportedResult)
    adso = next(c for c in report.crossings if c.bw_object == _ADSO_VIEW)
    assert (adso.bw_object_resolved, adso.bw_object_kind) == ("SALES_ADSO", "adso")


def test_crossing_names_an_unconfirmed_provider_without_asserting_its_type() -> None:
    report = _repo().get_hana_crossings()
    assert not isinstance(report, UnsupportedResult)
    ghost = next(c for c in report.crossings if c.bw_object == _GHOST_VIEW)
    assert ghost.bw_object_resolved == "GONE_PROV"  # named
    assert ghost.bw_object_kind is None  # but not claimed to exist
    assert ghost.resolution == "bw_provider_view"


def test_resolve_bw_view_providers_flags_verification() -> None:
    resolved = _repo().resolve_bw_view_providers([_CP_NODE, _GHOST_VIEW, "SALES_COMPAT_VIEW"])
    assert set(resolved) == {_CP_NODE, _GHOST_VIEW}  # non-BW views are not returned
    assert resolved[_CP_NODE].verified is True
    assert resolved[_GHOST_VIEW].verified is False
    assert resolved[_CP_NODE].provenance.source_table == "OBJECT_DEPENDENCIES"


def test_calc_view_lineage_lists_consuming_providers_once_each() -> None:
    lineage = _repo().get_calc_view_lineage("CV_SALES")
    assert not isinstance(lineage, UnsupportedResult)
    # SALES_CP appears via two internal calc nodes but must be listed once.
    assert [c.provider for c in lineage.consuming_bw_providers] == [
        "GONE_PROV",
        "SALES_ADSO",
        "SALES_CP",
    ]
    cp = next(c for c in lineage.consuming_bw_providers if c.provider == "SALES_CP")
    assert cp.resolved_kind == "compositeprovider"
    assert any("0BW:BIA:" in caveat for caveat in lineage.caveats)


def test_calc_view_lineage_without_consumers_stays_quiet() -> None:
    lineage = _repo().get_calc_view_lineage("CV_FIN")
    assert not isinstance(lineage, UnsupportedResult)
    assert lineage.consuming_bw_providers == []
    assert not any("0BW:BIA:" in caveat for caveat in lineage.caveats)


def test_unsupported_without_object_dependencies() -> None:
    result = _repo(present={"hana_views"}).get_calc_view_lineage("CV_SALES")
    assert isinstance(result, UnsupportedResult)
