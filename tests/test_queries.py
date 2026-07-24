"""Tests for the BEx query repository (B7), offline against scripted fixtures.

Synthetic names only. Landscape:
    QUERY_SALES (COMPUID Q1UID) on provider SALES_CUBE
      elements: root(REP) -> E_RKF(SEL, restricts CURRENCY via customer-exit var USD_VAR) [COL]
                          -> E_CHAR(SEL, restricts MATERIAL = 'M100' literal)             [ROW]
    provider trace: SALES_CUBE <- SALES_DSO <- DS_SALES (datasource)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.queries import QueriesRepository

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
    "element_range": "RSZRANGE",
    "element_select": "RSZSELECT",
    "global_variable": "RSZGLOBV",
    "transformation": "RSTRAN",
    "dtp": "RSBKDTP",
}

# COMPUID, COMPID, OWNER, TSTPNM, LASTUSED, OBJSTAT
_HEADER = ("Q1UID", "QUERY_SALES", "DEV", "DEVUSER", "20260101000000", "ACT")
_XREF = {"Q1UID": [("E_RKF", "COL", 1), ("E_CHAR", "ROW", 2)], "E_RKF": [], "E_CHAR": []}
_DIR = {  # ELTUID -> (DEFTP, MAPNAME, REUSABLE)
    "Q1UID": ("REP", "QUERY_SALES", "X"),
    "E_RKF": ("SEL", "RKF_AMOUNT", "X"),
    "E_CHAR": ("SEL", "", ""),
}
_TXT = {  # ELTUID -> (TXTSH, TXTLG)
    "Q1UID": ("Sales Qry", "Sales query by material"),
    "E_RKF": ("Net amt", "Net amount RKF"),
}
_RANGE = {  # ELTUID -> [(IOBJNM, SIGN, OPT, LOW, HIGH, LOWFLAG, HIGHFLAG)]
    "E_RKF": [("CURRENCY", "I", "EQ", "USD_VAR", "", "3", "0")],  # LOW is a variable ref (flag 3)
    "E_CHAR": [("MATERIAL", "I", "EQ", "M100", "", "1", "0")],  # literal (flag 1)
}
_SELECT = {"E_RKF": ["AMOUNT"], "E_CHAR": ["MATERIAL"]}
_GLOBV = {"USD_VAR": ("1", "3", "CURRENCY", "")}  # VPROCTP 3 = customer exit
_TRANS_BY_TARGET = {
    "SALES_CUBE": [("SALES_DSO", "ODSO", "TR1")],
    "SALES_DSO": [("DS_SALES", "RSDS", "TR0")],
}


def _in_params(sql: str, params: list[Any]) -> set[str]:
    return {str(p) for p in params}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        if "RSZCOMPDIR" in sql:
            return self._compdir(sql, params)
        if "RSZCOMPIC" in sql:
            return self._compic(sql, params)
        if "RSZELTXREF" in sql:
            return [(c, lay, pos) for c, lay, pos in _XREF.get(str(params[-1]), [])]
        if "RSZELTDIR" in sql:
            ids = _in_params(sql, params)
            return [(k, *v) for k, v in _DIR.items() if k in ids]
        if "RSZELTTXT" in sql:
            ids = _in_params(sql, params[1:])  # first param is LANGU
            return [(k, sh, lg) for k, (sh, lg) in _TXT.items() if k in ids]
        if "RSZRANGE" in sql:
            ids = _in_params(sql, params)
            return [(k, *r) for k, rs in _RANGE.items() if k in ids for r in rs]
        if "RSZSELECT" in sql:
            ids = _in_params(sql, params)
            return [(o,) for k, objs in _SELECT.items() if k in ids for o in objs]
        if "RSZGLOBV" in sql:
            ids = _in_params(sql, params)
            return [(k, *v) for k, v in _GLOBV.items() if k in ids]
        if "RSBKDTP" in sql:
            return []
        if "RSTRAN" in sql:  # lineage trace: upstream by TARGETNAME
            return [(s, ty, tr) for s, ty, tr in _TRANS_BY_TARGET.get(str(params[-1]), [])]
        return []

    @staticmethod
    def _compdir(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "TSTPNM" in sql:  # header (6 cols)
            ident = str(params[-1])
            return [_HEADER] if ident in (_HEADER[0], _HEADER[1]) else []
        # list (4 cols): COMPUID, COMPID, OWNER, LASTUSED
        return [(_HEADER[0], _HEADER[1], _HEADER[2], _HEADER[4])]

    @staticmethod
    def _compic(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "INFOCUBE = ?" in sql:  # compuids for provider
            return [("Q1UID",)] if str(params[-1]) == "SALES_CUBE" else []
        if "COMPUID IN" in sql:  # providers_for (list)
            return [("Q1UID", "SALES_CUBE", "X")]
        return [("SALES_CUBE", "X")]  # providers_list (COMPUID = ?)


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in present else None,
                present=logical in present,
                schema_name=SCHEMA if logical in present else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _repo(present: set[str] | None = None) -> QueriesRepository:
    return QueriesRepository(ScriptedConnection(), _capability(present))


def test_get_query_header_and_description_via_compuid_join() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert query.compid == "QUERY_SALES"
    # description comes from RSZELTTXT where ELTUID == COMPUID (the query is itself an element)
    assert query.description == "Sales query by material"
    assert query.provider == "SALES_CUBE"
    assert query.active is True


def test_get_query_element_tree_and_types() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    by_uid = {e.eltuid: e for e in query.elements}
    assert by_uid["Q1UID"].element_type == "query"
    assert by_uid["E_RKF"].element_type == "restricted_key_figure"
    roles = {(e.parent_uid, e.child_uid): e.role for e in query.edges}
    assert roles[("Q1UID", "E_RKF")] == "columns"
    assert roles[("Q1UID", "E_CHAR")] == "rows"


def test_get_query_restrictions_variable_vs_literal() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    rkf = next(e for e in query.elements if e.eltuid == "E_RKF")
    curr = next(r for r in rkf.restrictions if r.iobjnm == "CURRENCY")
    assert curr.low_is_variable is True  # LOWFLAG = 3
    assert curr.low == "USD_VAR"
    char = next(e for e in query.elements if e.eltuid == "E_CHAR")
    mat = next(r for r in char.restrictions if r.iobjnm == "MATERIAL")
    assert mat.low_is_variable is False
    assert mat.low == "M100"


def test_get_query_customer_exit_variable_flagged() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    var = next(v for v in query.variables if v.name == "USD_VAR")
    assert var.processing_type == "customer_exit"
    assert var.is_customer_exit is True
    assert var.iobjnm == "CURRENCY"


def test_get_query_usage() -> None:
    usage = _repo().get_query_usage("QUERY_SALES")
    assert not isinstance(usage, UnsupportedResult)
    assert usage.last_used is not None
    assert usage.last_used.year == 2026


def test_get_query_lineage_reaches_datasource_and_flags_exit_var() -> None:
    lineage = _repo().get_query_lineage("QUERY_SALES")
    assert not isinstance(lineage, UnsupportedResult)
    assert "SALES_CUBE" in lineage.providers
    assert "USD_VAR" in lineage.customer_exit_variables
    iobjs = {p.iobjnm for p in lineage.paths}
    assert {"MATERIAL", "AMOUNT", "CURRENCY"} <= iobjs
    material = next(p for p in lineage.paths if p.iobjnm == "MATERIAL")
    assert material.reaches_datasource is True
    ds_hops = [h for h in material.hops if h.via == "datasource"]
    assert any(h.object_name == "DS_SALES" for h in ds_hops)


def test_list_queries_and_provider_filter() -> None:
    repo = _repo()
    result = repo.list_queries()
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    assert summaries[0].compid == "QUERY_SALES"
    filtered = repo.list_queries(provider="SALES_CUBE")
    assert not isinstance(filtered, UnsupportedResult)
    assert filtered[1] == 1


def test_unsupported_without_query_dir() -> None:
    result = _repo(present={"element_dir"}).get_query("QUERY_SALES")
    assert isinstance(result, UnsupportedResult)
