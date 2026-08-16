"""Tests for the BEx query repository (B7), offline against scripted fixtures.

Synthetic names only. Landscape:
    QUERY_SALES (COMPUID Q1UID) on provider SALES_CUBE
      elements: root(REP) -> E_RKF(SEL, restricts CURRENCY via customer-exit var USD_VAR) [COL]
                          -> E_CHAR(SEL, restricts MATERIAL = 'M100' literal)             [ROW]
    provider trace: SALES_CUBE <- SALES_DSO <- DS_SALES (datasource)
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.queries import (
    _PROPERTY_COLUMNS,
    QueriesRepository,
    classify_origin,
)
from mcp_server_sapbw.services.lineage import LineageService

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
    "element_range": "RSZRANGE",
    "element_select": "RSZSELECT",
    "element_calc": "RSZCALC",
    "element_prop": "RSZELTPROP",
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
# RSZCALC per element, ordered by STEPNR:
#   (AGGRGEN, AGGREXC, AGGRCHA, AGGRCHA2, AGGRCHA3, AGGRCHA4, AGGRCHA5, AGGREXCLUDE)
# E_RKF counts distinct materials, so its value is NOT the sum of the underlying rows.
_CALC: dict[str, list[tuple[Any, ...]]] = {
    "E_RKF": [
        ("SUM", "", "", "", "", "", "", ""),
        ("", "CNT", "MATERIAL", "PLANT", "", "", "", ""),
    ],
}
# RSZELTPROP per element, written by column name and projected into _PROPERTY_COLUMNS order below.
# Naming the columns matters here: the row is 24 wide and a positional fixture silently tests the
# wrong column the moment the SELECT list changes.
#
# E_RKF translates to USD and inverts its sign; E_CHAR displays along a hierarchy chosen by a
# variable, aggregates locally as a last value, is hidden, and carries its own key date. Between
# them they cover every code family: a literal source, a runtime-resolved source, a non-summation
# local aggregation, a three-valued boolean, and BW's own defaults.
_PROP_BY_NAME: dict[str, dict[str, str]] = {
    "E_RKF": {
        "TCUR": "USD",
        "TCURFLAG": "1",
        "CTTNM": "STD_RATE",
        "NOSUMS": "U",
        "SIGNINV": "X",
    },
    "E_CHAR": {
        "HIENM": "HIER_VAR",
        "HIENMFLAG": "3",
        "STRT_LVL": "02",
        "HRY_ACTIVE": "X",
        "STRMEM_LAGGR": "12",
        "LAGGR_DIR": "1",
        "HIDDEN": "X",
        "KEYDATE": "20260101",
        "KEYDATEFLAG": "1",
    },
}
# Unset columns default the way BW does: a NUMC flag holds '0'/'00', a CHAR column holds blank.
_PROP_DEFAULTS = {"TCURFLAG": "0", "TCURDATEFLAG": "0", "TUOMFLAG": "0", "HIENMFLAG": "0"}
_PROP_NUMC = {"STRT_LVL": "00", "STRMEM_LAGGR": "00", "LAGGR_DIR": "0", "KEYDATEFLAG": "0"}


def _prop_row(values: dict[str, str]) -> tuple[Any, ...]:
    """Project a by-name fixture onto the repository's SELECT list, minus the leading ELTUID."""
    return tuple(
        values.get(column, _PROP_DEFAULTS.get(column, _PROP_NUMC.get(column, "")))
        for column in _PROPERTY_COLUMNS[1:]
    )


_PROP: dict[str, tuple[Any, ...]] = {
    eltuid: _prop_row(values) for eltuid, values in _PROP_BY_NAME.items()
}

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
        if "RSZCALC" in sql:
            ids = _in_params(sql, params)
            return [(k, *row) for k, rows in _CALC.items() if k in ids for row in rows]
        if "RSZELTPROP" in sql:
            ids = _in_params(sql, params)
            return [(k, *row) for k, row in _PROP.items() if k in ids]
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
        # list (4 cols): COMPUID, COMPID, OWNER, LASTUSED. Both a Query-Designer query and an
        # ad-hoc one, so origin classification and the SQL-level filter are both exercised.
        rows = [
            (_HEADER[0], _HEADER[1], _HEADER[2], _HEADER[4]),
            ("Q2UID", "!!1ADHOC", "ANALYST", _HEADER[4]),
        ]
        pattern = next((str(p) for p in params if str(p).startswith("!!")), None)
        if pattern is None:
            return rows
        wants_designed = "NOT LIKE" in sql
        return [r for r in rows if str(r[1]).startswith("!!") is not wants_designed]

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


def test_lineage_service_resolves_query_to_provider_and_datasource() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="both", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "QUERY_SALES" in names
    assert "SALES_CUBE" in names
    assert "DS_SALES" in names
    assert any(e.kind == "query_provider" for e in graph.edges)


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


# --- origin: designed report vs ad-hoc BEx-Analyzer navigation ---------------------------------


def _summaries(**kwargs: object) -> list[Any]:
    result = _repo().list_queries(**kwargs)  # type: ignore[arg-type]
    assert not isinstance(result, UnsupportedResult)
    return result[0]


def test_designed_query_is_classified_as_designed() -> None:
    designed = next(s for s in _summaries() if s.compid == "QUERY_SALES")
    assert designed.origin == "designed"


def test_double_bang_prefix_is_classified_as_ad_hoc() -> None:
    """SAP generates the '!!' name for a query created straight in the BEx Analyzer."""
    ad_hoc = next(s for s in _summaries() if s.compid == "!!1ADHOC")
    assert ad_hoc.origin == "ad_hoc"


def test_origin_defaults_to_unfiltered() -> None:
    assert len(_summaries()) == 2


def test_designed_filter_excludes_ad_hoc() -> None:
    names = {s.compid for s in _summaries(origin="designed")}
    assert names == {"QUERY_SALES"}


def test_ad_hoc_filter_returns_only_ad_hoc() -> None:
    names = {s.compid for s in _summaries(origin="ad_hoc")}
    assert names == {"!!1ADHOC"}


def test_classify_origin_is_a_pure_name_reading() -> None:
    assert classify_origin("!!ANY") == "ad_hoc"
    assert classify_origin("NORMAL") == "designed"
    # A single "!" is not the marker, and a missing name is not evidence of ad-hoc creation.
    assert classify_origin("!ONE") == "designed"
    assert classify_origin(None) == "designed"


def test_unsupported_without_query_dir() -> None:
    result = _repo(present={"element_dir"}).get_query("QUERY_SALES")
    assert isinstance(result, UnsupportedResult)


# --- aggregation on query elements (RSZCALC) ---------------------------------------------------


def _element(eltuid: str) -> Any:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    return next(e for e in query.elements if e.eltuid == eltuid)


def test_calc_step_count_is_populated() -> None:
    """The field existed but nothing filled it, because RSZCALC was never read."""
    assert _element("E_RKF").calc_step_count == 2
    assert _element("E_CHAR").calc_step_count == 0


def test_exception_aggregation_is_attached_to_the_element() -> None:
    exc = _element("E_RKF").exception_aggregation
    assert exc is not None
    assert exc.behaviour.code == "CNT"
    assert exc.behaviour.label == "Counter (all values)"
    assert [r.name for r in exc.reference_characteristics] == ["MATERIAL", "PLANT"]
    assert exc.reproducible_by_summation is False


def test_standard_aggregation_comes_from_the_first_step() -> None:
    standard = _element("E_RKF").standard_aggregation
    assert standard is not None
    assert standard.code == "SUM"
    assert standard.label == "Summation"


def test_element_without_calc_rows_has_no_aggregation() -> None:
    assert _element("E_CHAR").exception_aggregation is None
    assert _element("E_CHAR").standard_aggregation is None


def test_query_caveat_warns_that_totals_will_not_match() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    joined = " ".join(query.caveats)
    assert "exception aggregation" in joined
    assert "CNT" in joined
    assert "not reproduced by adding the underlying rows up" in joined


# --- element properties (RSZELTPROP) -----------------------------------------------------------


@contextmanager
def _property_override(eltuid: str, column: str, value: str) -> Iterator[None]:
    """Replace one RSZELTPROP column by name, so a test never depends on a tuple index."""
    index = _PROPERTY_COLUMNS.index(column) - 1  # the fixture rows omit the leading ELTUID
    original = dict(_PROP)
    _PROP[eltuid] = tuple(value if i == index else v for i, v in enumerate(original[eltuid]))
    try:
        yield
    finally:
        _PROP.clear()
        _PROP.update(original)


def test_currency_translation_is_read_with_its_translation_type() -> None:
    props = _element("E_RKF").properties
    assert props is not None
    currency = props.currency_translation
    assert currency is not None
    assert (currency.target_currency, currency.translation_type) == ("USD", "STD_RATE")
    assert currency.target_source is not None
    assert currency.target_source.value_holds == "literal"
    assert currency.target_source.runtime_resolved is False


def test_sign_inversion_and_total_suppression_are_decoded() -> None:
    props = _element("E_RKF").properties
    assert props is not None
    assert props.sign_inverted is True
    assert props.total_suppressed is True
    assert props.total_suppression == "Suppress the total unconditionally"


def test_local_aggregation_uses_the_numeric_domain() -> None:
    """STRMEM_LAGGR is domain RRLAGGR ('00'-'13'), not the three-letter exception codes."""
    props = _element("E_CHAR").properties
    assert props is not None
    aggregation = props.local_aggregation
    assert aggregation is not None
    assert (aggregation.code, aggregation.label) == ("12", "Last value")
    assert aggregation.is_summation is False
    assert props.local_aggregation_direction == "Calculate along the rows"


def test_summation_local_aggregation_is_not_reported_as_altering_the_value() -> None:
    """'01' is summation, so it changes nothing about how the figure relates to its rows."""
    with _property_override("E_CHAR", "STRMEM_LAGGR", "01"):
        props = _element("E_CHAR").properties
        assert props is not None
        assert props.local_aggregation is not None
        assert props.local_aggregation.is_summation is True
        assert not any("aggregates locally" in r for r in props.changes_the_number)


def test_runtime_resolved_hierarchy_is_flagged_not_reported_as_a_name() -> None:
    props = _element("E_CHAR").properties
    assert props is not None
    hierarchy = props.display_hierarchy
    assert hierarchy is not None
    assert hierarchy.source is not None
    assert hierarchy.source.runtime_resolved is True
    assert hierarchy.source.value_holds == "variable_name"
    assert hierarchy.start_level == 2
    assert hierarchy.active is True


def test_hidden_element_is_decoded_from_its_own_domain() -> None:
    props = _element("E_CHAR").properties
    assert props is not None
    assert props.hidden is True
    assert props.display == "Hide"


def test_own_key_date_is_reported_as_changing_the_number() -> None:
    props = _element("E_CHAR").properties
    assert props is not None
    assert props.key_date == "20260101"
    assert props.key_date_source is not None
    assert any("key date of its own" in r for r in props.changes_the_number)


def test_changes_the_number_names_only_the_settings_that_do() -> None:
    rkf = _element("E_RKF").properties
    assert rkf is not None
    reasons = " ".join(rkf.changes_the_number)
    assert "translated to USD" in reasons
    assert "sign is inverted" in reasons
    # Total suppression hides a figure; it does not change the ones that are shown.
    assert "suppress" not in reasons.lower()


def test_query_caveat_names_the_elements_that_alter_their_value() -> None:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    caveat = next(c for c in query.caveats if "alter their own value" in c)
    assert "translated to USD" in caveat
    assert "RKF_AMOUNT" in caveat  # named by MAPNAME where it has one


def test_flag_set_without_a_stored_value_is_stated_honestly() -> None:
    """852 elements declare a currency target on the reference system; only 817 store one."""
    with _property_override("E_RKF", "TCUR", ""):
        props = _element("E_RKF").properties
        assert props is not None
        reasons = " ".join(props.changes_the_number)
        assert "declares" in reasons and "does not record" in reasons
        assert "runtime" not in reasons  # a fixed value is not resolved at runtime


def test_absent_property_table_is_stated_not_treated_as_nothing_configured() -> None:
    query = _repo(present=set(_TABLES) - {"element_prop"}).get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert all(e.properties is None for e in query.elements)
    assert any("were not read" in c and "unknown rather than absent" in c for c in query.caveats)


def test_no_aggregation_caveat_when_everything_sums() -> None:
    """A query whose elements all sum normally must not carry a scary caveat."""
    original = dict(_CALC)
    _CALC.clear()
    _CALC["E_RKF"] = [("SUM", "SUM", "MATERIAL", "", "", "", "", "")]
    try:
        query = _repo().get_query("QUERY_SALES")
        assert not isinstance(query, UnsupportedResult)
        assert not any("exception aggregation" in c for c in query.caveats)
        exc = next(e for e in query.elements if e.eltuid == "E_RKF").exception_aggregation
        assert exc is not None
        assert exc.reproducible_by_summation is True
    finally:
        _CALC.clear()
        _CALC.update(original)


def test_disagreeing_steps_are_reported_not_silently_resolved() -> None:
    original = dict(_CALC)
    _CALC.clear()
    _CALC["E_RKF"] = [
        ("", "CNT", "MATERIAL", "", "", "", "", ""),
        ("", "LAS", "CALDAY", "", "", "", "", ""),
    ]
    try:
        exc = _element("E_RKF").exception_aggregation
        assert exc is not None
        assert exc.behaviour.code == "CNT"  # first step wins
        assert "different exception aggregations" in (exc.note or "")
    finally:
        _CALC.clear()
        _CALC.update(original)


def test_missing_calc_table_degrades_quietly() -> None:
    repo = QueriesRepository(
        ScriptedConnection(), _capability(present=set(_TABLES) - {"element_calc"})
    )
    query = repo.get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert all(e.exception_aggregation is None for e in query.elements)
    assert all(e.calc_step_count == 0 for e in query.elements)
