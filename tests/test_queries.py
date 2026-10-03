"""Tests for the BEx query repository (B7), offline against scripted fixtures.

Synthetic names only. Landscape:
    QUERY_SALES (COMPUID Q1UID) on provider SALES_CUBE
      elements: root(REP) -> E_RKF(SEL, restricts 1KYFNM=AMOUNT + CURRENCY via cust-exit var)  [COL]
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
    _LAYTP_TO_ROLE,
    _PROPERTY_COLUMNS,
    QueriesRepository,
    _decode_operator,
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
    # The source-system boundary: RSDS names the extract structure, RSDSSEGFD proves an enhancement.
    "datasource": "RSDS",
    "datasource_field": "RSDSSEGFD",
}

# COMPUID, COMPID, OWNER, TSTPNM, LASTUSED, OBJSTAT
_HEADER = ("Q1UID", "QUERY_SALES", "DEV", "DEVUSER", "20260101000000", "ACT")
# LAYTP codes on the edges. 'NIL' ("No Layout") and 'SOB' ("Selection Object") are here because
# both are in active use on a real 7.50 system - NIL is the most common code there - and both used
# to fall through to role='other' (D24). 'ZZZ' is not a documented domain value and stands for a
# code a future release could add: it must reach 'other' *and* keep its raw code.
#
# The E_SHEET and E_FILTER branches carry production's real shape, which the flat tree above does
# not: on a live 7.50 system a query root does not parent its placements directly. It parents a
# *query sheet* (DEFTP='SHT') which owns the axes, and a *selection object* (DEFTP='SOB') which is
# the global filter, and the placements hang off those. That indirection is the whole of D30 - the
# same LAYTP means different things under the two parents, so an edge cannot be read without knowing
# which one it hangs from. Measured across 400 production queries, 'AGG' is 26,748 free
# characteristics under the sheet and 28,040 filter characteristics under the selection object.
_XREF = {
    "Q1UID": [
        ("E_RKF", "COL", 1),
        ("E_CHAR", "ROW", 2),
        ("E_REUSE", "NIL", 3),
        ("E_SELOBJ", "SOB", 4),
        ("E_FUTURE", "ZZZ", 5),
        ("E_SHEET", "SHT", 6),
        ("E_FILTER", "SOB", 7),
        ("E_COND", "NIL", 8),
        ("E_EXC", "NIL", 9),
    ],
    "E_RKF": [],
    "E_CHAR": [],
    "E_REUSE": [],
    "E_SELOBJ": [],
    "E_FUTURE": [],
    # Under the sheet: AGG is a free characteristic and FLT is a member of the structure on an axis.
    "E_SHEET": [("E_FREE", "AGG", 1), ("E_MEMBER", "FLT", 2)],
    # Under the filter: AGG is a characteristic the query filters on.
    "E_FILTER": [("E_FILTCHAR", "AGG", 1)],
    "E_FREE": [],
    "E_MEMBER": [],
    "E_FILTCHAR": [],
    # A condition, which references its variable by uid rather than by name. The variable element
    # therefore has no MAPNAME and the variables list used to drop it while still counting the
    # element (D25).
    "E_COND": [("E_CONDVAR", "NIL", 1)],
    "E_EXC": [],
    "E_CONDVAR": [],
}
_DIR = {  # ELTUID -> (DEFTP, MAPNAME, REUSABLE, SUBDEFTP)
    # SUBDEFTP was added for D26. It is SAP's own declared element type, and it is the only thing
    # that distinguishes a condition or an exception from a characteristic placement - neither
    # restricts 1KYFNM, so the D28 heuristic calls both a characteristic. The blanks below are not
    # laziness: 1,581 active SEL elements on the reference system carry no SUBDEFTP, so the
    # heuristic is still the only route for those and the fixture has to keep exercising it.
    "Q1UID": ("REP", "QUERY_SALES", "X", "REP"),
    "E_RKF": ("SEL", "RKF_AMOUNT", "X", "RKF"),
    "E_CHAR": ("SEL", "", "", "CHA"),
    # Reusable, referenced by the query but not placed on an axis - the LAYTP='NIL' case (D24).
    "E_REUSE": ("CKF", "CKF_MARGIN", "X", "CKF"),
    # Blank SUBDEFTP: falls back to the D28 1KYFNM heuristic, as 1,581 live elements do.
    "E_SELOBJ": ("SEL", "SELOBJ_REGION", "X", ""),
    "E_FUTURE": ("SEL", "FUTURE_ELEMENT", "", ""),
    # The layout node. Used to decode to 'unknown' (D29) despite owning every placement below it.
    "E_SHEET": ("SHT", "", "", "SHT"),
    "E_FILTER": ("SOB", "", "", "SOB"),
    "E_FREE": ("SEL", "", "", "CHA"),
    # STM is deliberately unmapped, so a structure element still goes through the heuristic and
    # arrives as a restricted key figure. Reclassifying 12,357 live elements needs ground truth
    # nobody has collected yet, so the fix leaves this branch exactly as it was.
    "E_MEMBER": ("SEL", "", "", "STM"),
    "E_FILTCHAR": ("SEL", "", "", "CHA"),
    "E_COND": ("SEL", "COND_TOPN_UNITS", "X", "CON"),
    # An exception: same storage, same previous misclassification, and the counterpart case to
    # E_COND on every axis that matters - switched on, a declared operator, a literal threshold.
    "E_EXC": ("SEL", "EXC_MARGIN_ALERT", "X", "EXC"),
    # Blank MAPNAME: only RSZGLOBV.VARUNIID names this one (D25).
    "E_CONDVAR": ("VAR", "", "X", "VAR"),
}
_TXT = {  # ELTUID -> (TXTSH, TXTLG)
    "Q1UID": ("Sales Qry", "Sales query by material"),
    "E_RKF": ("Net amt", "Net amount RKF"),
}
_RANGE = {  # ELTUID -> [(IOBJNM, SIGN, OPT, LOW, HIGH, LOWFLAG, HIGHFLAG)]
    # A restricted key figure restricts 1KYFNM - that is what names the key figure it restricts, and
    # it is what distinguishes it from a plain characteristic placement, since BW types both 'SEL'
    # (D28). The fixture lacked it, which made it indistinguishable from E_CHAR below.
    "E_RKF": [
        ("1KYFNM", "I", "EQ", "AMOUNT", "", "1", "0"),
        ("CURRENCY", "I", "EQ", "USD_VAR", "", "3", "0"),  # LOW is a variable ref (flag 3)
    ],
    # No 1KYFNM: a characteristic sitting on the row axis, not a key figure.
    "E_CHAR": [("MATERIAL", "I", "EQ", "M100", "", "1", "0")],  # literal (flag 1)
    # E_FREE is deliberately absent: a free characteristic is placed on an axis *without* a value
    # restriction, so it has an RSZSELECT row and no RSZRANGE row at all. That asymmetry is what
    # makes RSZRANGE the wrong table to classify a selection from (D28).
    # A structure member, so key-figure-side: it restricts 1KYFNM like E_RKF does.
    "E_MEMBER": [("1KYFNM", "I", "EQ", "QUANTITY", "", "1", "0")],
    # The restriction the *filter* holds, stored on the filter's child rather than on the filter
    # itself. D31: the filter element reported restrictions=[] while this row sat one hop away.
    "E_FILTCHAR": [("REGION", "I", "EQ", "AUTH_REGION_VAR", "", "3", "0")],
}
# RSZSELECT: which InfoObject each selection is *about*. Distinct from _RANGE above, which says what
# values it is restricted to - a free characteristic has a row here and none there, so classifying a
# selection from the ranges alone leaves it unclassified (D28).
_SELECT = {
    "E_RKF": ["1KYFNM", "AMOUNT"],  # key-figure-side: selects on the key-figure dimension
    "E_CHAR": ["MATERIAL"],  # a characteristic on the row axis
    "E_FREE": ["PLANT"],  # a free characteristic: named here, unrestricted in _RANGE
    "E_MEMBER": ["1KYFNM", "QUANTITY"],  # a structure member, so key-figure-side
    "E_FILTCHAR": ["REGION"],  # the characteristic the query filters on
}
# A condition's and an exception's RSZSELECT row. Keyed under a pseudo-InfoObject rather than a
# business characteristic (D26) - and under **two different** pseudo-InfoObjects, which is D41: a
# condition files under '1CONDITION' and an exception under '1EXCEPTION'. The first version of the
# reader assumed one name, so every exception matched nothing and came back with no definition and
# active=False, while 71 of the reference system's 77 are switched on.
#
# ELTUID -> (IOBJNM, ACTIVE, CONTYPE, EXCABSREL).
#
# ACTIVE carries real information and is blank more often than one would assume: 45 of 134 condition
# rows on the reference system are off, and all 4 on the subject query are. E_COND is off and E_EXC
# is on, because a fixture that tested only one state would not show the difference.
_COND_SELECT: dict[str, tuple[str, str, str, str]] = {
    "E_COND": ("1CONDITION", "", "1", "0"),  # inactive condition (CONTYPE 1 = Condition)
    "E_EXC": ("1EXCEPTION", "X", "2", "2"),  # active exception, EXCABSREL 2 = All rows
}
# The definition rows, told apart by FACIOBJNM rather than by ENUM order.
# ELTUID -> [(IOBJNM, FACIOBJNM, OPT, LOW, HIGH, LOWFLAG, ALERTLEVEL)].
#
# The two kinds genuinely differ in shape, and reproducing that is the whole point of this fixture:
#
#   * a condition has exactly ONE '1VALUE' row with ALERTLEVEL '00' - measured, 127 of 127;
#   * an exception has one '1VALUE' row PER ALERT LEVEL, plus rows whose FACIOBJNM is a real
#     characteristic, which are the drilldown levels it is evaluated at.
#
# E_EXC below is modelled on a real production exception: three bands over (-inf..0), (0..100) and
# (100..+inf) at Bad 3, Critical 2 and Good 1, with a drilldown characteristic and scope 'All'.
_COND_RANGE: dict[str, list[tuple[str, str, str, str, str, str, str]]] = {
    # Ranks a structure member, Top N, cut-off supplied by a variable. 'TC' is the operator this
    # system actually uses and RSZ_OPERATOR_DOMAIN does not declare it, so it must arrive advisory.
    # LOWFLAG '2' = reference to another element, '3' = variable - both declared values.
    "E_COND": [
        ("1CONDITION", "1STRUC", "EQ", "E_MEMBER", "", "2", "00"),
        ("1CONDITION", "1VALUE", "TC", "E_CONDVAR", "", "3", "00"),
    ],
    "E_EXC": [
        ("1EXCEPTION", "1STRUC", "EQ", "E_RKF", "", "2", "00"),
        ("1EXCEPTION", "1VALUE", "BT", "-99999999999", "0", "5", "09"),  # Bad 3
        ("1EXCEPTION", "1VALUE", "BT", "0", "100", "5", "05"),  # Critical 2
        ("1EXCEPTION", "1VALUE", "BT", "100", "99999999999", "5", "01"),  # Good 1
        # A drilldown level, not a comparison: 'NA' is not a declared operator either.
        ("1EXCEPTION", "MATERIAL", "NA", "", "", "0", "00"),
    ],
}
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

# VNAM -> (VARTYP, VPROCTP, IOBJNM, VARINPUT, VARUNIID)
#
# VARUNIID holds the *element uid*, and it is the only thing that names a variable whose element has
# a blank MAPNAME - 801 of 2,188 on the reference system, 36.6% (D25). COND_TOPN is that case: it is
# reached through a query condition, which stores the variable by uid rather than by name.
_GLOBV = {
    "USD_VAR": ("1", "3", "CURRENCY", "", "E_VAR_USD"),  # VPROCTP 3 = customer exit
    "COND_TOPN": ("4", "5", "1FORMULA", "X", "E_CONDVAR"),  # VARTYP 4 = formula, 5 = user entry
}
_TRANS_BY_TARGET = {
    "SALES_CUBE": [("SALES_DSO", "ODSO", "TR1")],
    "SALES_DSO": [("DS_SALES", "RSDS", "TR0")],
}
#: Each object's own RSTLOGO code, for the type probe. SALES_CUBE appears only as a *target*, which
#: is exactly why a query's provider was reaching callers untyped.
_TYPE_CODE = {"SALES_CUBE": "CUBE", "SALES_DSO": "ODSO", "DS_SALES": "RSDS"}
# RSDS, keyed by DataSource: (EXSTRUCTURE, TYPE, DELTA). The extract structure the source system
# fills is what takes the walk one hop past the DataSource.
_RSDS = {"DS_SALES": ("EXTSTRU_SALES", "D", "ABR")}
#: Customer-namespace fields on the extract structure: metadata-confirmed enhancement evidence.
_RSDS_CUSTOM_FIELDS = {"DS_SALES": 4}


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
            # The condition reader asks a different question of the same table: which row is the
            # measure and which the threshold (FACIOBJNM), not what values a characteristic is
            # restricted to. Dispatching on the column list keeps the two apart, the way a database
            # would - a single shared shape here would let the repository read the wrong column.
            if "FACIOBJNM" in sql:
                # The IOBJNM filter is applied, not ignored. A reader that asked for only one of the
                # two pseudo-InfoObjects would then get nothing for the other kind - exactly the bug
                # D41 was, and a fixture that served both regardless would hide it.
                return [
                    (k, *r[1:])
                    for k, rs in _COND_RANGE.items()
                    if k in ids
                    for r in rs
                    if r[0] in ids
                ]
            return [(k, *r) for k, rs in _RANGE.items() if k in ids for r in rs]
        if "RSZSELECT" in sql:
            ids = _in_params(sql, params)
            if "ACTIVE" in sql:
                return [(k, *v[1:]) for k, v in _COND_SELECT.items() if k in ids and v[0] in ids]
            rows = [(k, o) for k, objs in _SELECT.items() if k in ids for o in objs]
            # Two callers, two column lists: one wants the InfoObject names alone (to find which
            # elements are variables), the other needs them keyed by element to classify a
            # selection and to name the characteristic it places (D28).
            return rows if "ELTUID, IOBJNM" in sql else [(o,) for _k, o in rows]
        if "RSZCALC" in sql:
            ids = _in_params(sql, params)
            return [(k, *row) for k, rows in _CALC.items() if k in ids for row in rows]
        if "RSZELTPROP" in sql:
            ids = _in_params(sql, params)
            return [(k, *row) for k, row in _PROP.items() if k in ids]
        if "RSZGLOBV" in sql:
            # Two routes into this table now: by VNAM for a variable known by name, and by VARUNIID
            # for one known only by its element uid (D25). Either may match.
            ids = {str(p).strip() for p in params}
            return [
                (k, v[0], v[1], v[2], v[3]) for k, v in _GLOBV.items() if k in ids or v[4] in ids
            ]
        if "RSBKDTP" in sql:
            return []
        if "RSDSSEGFD" in sql:  # customer-namespace field count for one DataSource
            return [(_RSDS_CUSTOM_FIELDS.get(str(params[-1]), 0),)]
        if "RSDS" in sql:  # the DataSource header: extract structure, type, delta
            row = _RSDS.get(str(params[0]))
            return [row] if row else []
        if "RSTRAN" in sql:
            # Two different reads hit RSTRAN and they return different shapes. The type probe asks
            # for one column and no TRANID; the lineage walk asks for the other endpoint plus the
            # TRANID. Serving one shape for both is how a fixture passes while the real system
            # behaves differently - the type probe would read a *name* out of the type column.
            if "TRANID" not in sql:
                return self._own_type(sql, params)
            return [(s, ty, tr) for s, ty, tr in _TRANS_BY_TARGET.get(str(params[-1]), [])]
        return []

    @staticmethod
    def _own_type(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        """``SELECT SOURCETYPE WHERE SOURCENAME = ?`` / the TARGET pair: an object's own TLOGO code.

        Answers only for the side the object really appears on, so an object that is never a source
        returns nothing for the SOURCETYPE probe - which is what makes the fallback order in
        ``_node_type_uncached`` meaningful rather than incidental.
        """
        # params[0], not params[-1]: this read is paginated, so the trailing bound values are the
        # LIMIT and OFFSET. Keying on the last one silently probed for the object named "0".
        name = str(params[0]).strip()
        code = _TYPE_CODE.get(name)
        if code is None:
            return []
        as_source = any(name == s for rows in _TRANS_BY_TARGET.values() for s, _t, _tr in rows)
        as_target = name in _TRANS_BY_TARGET
        if "SOURCETYPE" in sql:
            return [(code,)] if as_source else []
        if "TARGETTYPE" in sql:
            return [(code,)] if as_target else []
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
    # Both are DEFTP='SEL'; only what they restrict tells them apart (D28).
    assert by_uid["E_CHAR"].element_type == "characteristic"
    roles = {(e.parent_uid, e.child_uid): e.role for e in query.edges}
    assert roles[("Q1UID", "E_RKF")] == "columns"
    assert roles[("Q1UID", "E_CHAR")] == "rows"


def test_every_documented_laytp_code_decodes_to_something_other_than_other() -> None:
    """D24: 8 of the 17 documented LAYTP values fell through to 'other', silently.

    On the reference system that put 40,233 of 164,877 active element-tree edges (24.4%) into a
    bucket reading as "miscellaneous axis". The list below is the domain RSZLAYTP as DD07T
    documents it, read from the live dictionary rather than recalled.
    """
    documented = {
        "NIL": "No Layout",
        "ROW": "Row",
        "COL": "Column",
        "CEL": "Cell",
        "NAV": "Navigation",
        "AGG": "Aggregated",
        "FIX": "Filter",
        "MBR": "Structure element",
        "OPD": "Operand",
        "RNG": "Area",
        "REP": "Internal Use",
        "VAR": "Variable Sequence",
        "FLT": "Formatted Reporting - Order",
        "ATR": "Order of Attributes",
        "SHT": "Query Sheet",
        "SOB": "Selection Object",
        "QVR": "Variable Squence of Query Variables",
    }
    undecoded = sorted(code for code in documented if code not in _LAYTP_TO_ROLE)
    assert not undecoded, (
        f"{len(undecoded)} documented LAYTP code(s) would report as 'other': {undecoded}"
    )
    assert len(set(_LAYTP_TO_ROLE.values())) == len(documented), (
        "each documented code should carry its own meaning rather than share one"
    )


def test_an_unplaced_element_is_not_reported_as_an_axis() -> None:
    """``NIL`` means referenced-but-not-placed, which is not a kind of axis.

    Conflating it with a real placement is what made the element tree impossible to reconcile
    against Query Designer: a query's reusable definitions all arrive as NIL.
    """
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    # Guard against a vacuous pass: an unknown name returns an empty shell rather than raising, so
    # asserting over an empty edge list would succeed while testing nothing.
    assert query.edges, "no edges came back; the assertions below would pass vacuously"
    roles = {(e.parent_uid, e.child_uid): e.role for e in query.edges}
    assert roles[("Q1UID", "E_REUSE")] == "unplaced"
    assert roles[("Q1UID", "E_SELOBJ")] == "selection_object"
    placed = {"rows", "columns", "free", "filter"}
    assert roles[("Q1UID", "E_REUSE")] not in placed


def test_an_undocumented_laytp_code_stays_auditable_instead_of_vanishing() -> None:
    """An unrecognised code must be distinguishable from a documented miscellaneous one."""
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    assert query.edges, "no edges came back; the assertions below would pass vacuously"
    edge = next(e for e in query.edges if e.child_uid == "E_FUTURE")
    assert edge.role == "other"
    assert edge.role_code == "ZZZ", "the raw code is the only way to spot a decode gap later"
    # And a decoded edge keeps its code too, so the pair can always be checked against each other.
    col = next(e for e in query.edges if e.child_uid == "E_RKF")
    assert (col.role, col.role_code) == ("columns", "COL")


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
    assert [h.via for h in material.hops][-1] == "datasource"
    # The provider's boundary set is carried once on the result rather than restated per field.
    assert "DS_SALES" in lineage.provider_datasources


def test_the_provider_boundary_is_named_once_not_repeated_per_field() -> None:
    """The 6,500-hop defect: the fallback used to append every DataSource to every field's path.

    Two properties together are the fix. A fallback path carries *one* boundary hop rather than one
    per DataSource, and the names live on the result. Asserting only the first would pass on a
    version that dropped the names entirely, which is a different kind of wrong answer.
    """
    lineage = _repo().get_query_lineage("QUERY_SALES")
    assert not isinstance(lineage, UnsupportedResult)
    fallbacks = [p for p in lineage.paths if p.resolution == "provider"]
    assert fallbacks, "the fixture must exercise the fallback for this test to mean anything"
    for path in fallbacks:
        boundary = [h for h in path.hops if h.via == "datasource"]
        assert len(boundary) == 1, (
            "the provider's DataSources are alternatives, not a chain: one boundary hop, not one "
            f"hop each. {path.iobjnm} carried {len(boundary)}"
        )
        assert boundary[0].advisory is True, (
            "the provider's boundary is not this field's derivation"
        )
    assert lineage.provider_datasources, "the boundary set must still be reachable"


def test_lineage_service_resolves_query_to_provider_and_datasource() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="both", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "QUERY_SALES" in names
    assert "SALES_CUBE" in names
    assert "DS_SALES" in names
    assert any(e.kind == "query_provider" for e in graph.edges)


# --- past the DataSource: the source-system boundary --------------------------------------------
#
# The DataSource used to be a hard stop. ``source_extract`` and ``source_object`` were defined in
# the model and never produced, so an upstream walk answered "this came from a DataSource" and left
# the next question - extracted by what? - unanswered.


def test_the_querys_own_provider_is_typed_not_left_unknown() -> None:
    """RSZCOMPIC names the provider but carries no type, and no hop types it either.

    Every other node learns its type from the hop that discovered it, because RSTRAN carries the
    other endpoint's type code. A query's provider is the *target* of its inbound transformations,
    so it is never on the naming side - and it was reaching callers as ``unknown``, which on a
    diagram is the one grey box labelled "object" sitting where the subject of the whole graph
    should be.
    """
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=3)
    assert not isinstance(graph, UnsupportedResult)
    provider = next(n for n in graph.nodes if n.name == "SALES_CUBE")
    assert provider.object_type != "unknown"
    assert provider.object_type == "infocube"
    # And the canonical ref agrees, so a caller can join it against bw_describe_object.
    assert provider.ref is not None and provider.ref.object_type == "infocube"


def test_the_walk_continues_past_the_datasource_to_its_extract_structure() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    by_type = {n.name: n.object_type for n in graph.nodes}
    assert by_type.get("EXTSTRU_SALES") == "source_object"
    edge = next(e for e in graph.edges if e.kind == "source_extract")
    assert (edge.src, edge.dst) == ("EXTSTRU_SALES", "DS_SALES")
    assert edge.confidence == "exact", "RSDS declares this; it is not a naming-convention reading"


def test_the_boundary_node_stops_claiming_its_upstream_is_unresolved() -> None:
    """A node with a resolved parent in the same graph must not also say the graph ends there."""
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    boundary = next(n for n in graph.nodes if n.name == "DS_SALES")
    assert boundary.upstream_resolved is True
    assert boundary.source_system is not None
    assert boundary.source_system.object_name == "EXTSTRU_SALES"


def test_trace_to_source_reports_a_resolved_boundary_as_resolved() -> None:
    service = LineageService(ScriptedConnection(), _capability())
    trace = service.trace_to_source("QUERY_SALES", depth=4)
    assert not isinstance(trace, UnsupportedResult)
    assert "DS_SALES" in trace.datasources_reached
    assert "DS_SALES" not in trace.unresolved_boundaries, (
        "the extract structure was resolved, so listing the DataSource as an open boundary would "
        "leave a caller no way to tell which boundaries really are still open"
    )
    assert any("extract structure" in c for c in trace.caveats)


def test_the_boundary_edge_reports_enhancement_evidence_and_names_the_gap() -> None:
    """Customer-namespace fields prove an enhancement exists; the code itself is not in BW."""
    service = LineageService(ScriptedConnection(), _capability())
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    edge = next(e for e in graph.edges if e.kind == "source_extract")
    assert edge.note is not None
    assert "4 customer-namespace field(s)" in edge.note
    assert "bw_get_extractor_exit_code" in edge.note, "the gap must name the tool that closes it"
    assert "delta method ABR" in edge.note


def test_no_extract_structure_means_no_invented_boundary_node() -> None:
    """An unresolved boundary is a better answer than a synthesized extractor name."""

    class NoRsds(ScriptedConnection):
        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            if "RSDSSEGFD" not in sql and "RSDS" in sql:
                return []
            return super().execute_select(sql, parameters)

    graph = LineageService(NoRsds(), _capability()).get_lineage(
        "QUERY_SALES", direction="upstream", depth=4
    )
    assert not isinstance(graph, UnsupportedResult)
    assert not any(e.kind == "source_extract" for e in graph.edges)
    assert not any(n.object_type == "source_object" for n in graph.nodes)
    boundary = next(n for n in graph.nodes if n.name == "DS_SALES")
    assert boundary.upstream_resolved is False


def test_an_absent_datasource_table_leaves_the_boundary_where_it_was() -> None:
    service = LineageService(
        ScriptedConnection(), _capability(present=set(_TABLES) - {"datasource"})
    )
    graph = service.get_lineage("QUERY_SALES", direction="upstream", depth=4)
    assert not isinstance(graph, UnsupportedResult)
    assert not any(e.kind == "source_extract" for e in graph.edges)


# --- a node that is not a BW object is not asked BW questions ------------------------------------


def test_an_extract_structure_is_terminal_rather_than_expanded_as_a_provider() -> None:
    """It is source-system ABAP: no transformation targets it and no query reads it.

    Asked anyway, each boundary node cost a full provider expansion to conclude nothing - measured,
    about 200 statements on a production walk. It still belongs in the graph; only its expansion is
    meaningless.
    """

    class Counting(ScriptedConnection):
        def __init__(self) -> None:
            self.seen: list[str] = []

        def execute_select(
            self, sql: str, parameters: Sequence[Any] | None = None
        ) -> list[tuple[Any, ...]]:
            self.seen.append(str(list(parameters or [])))
            return super().execute_select(sql, parameters)

    connection = Counting()
    graph = LineageService(connection, _capability()).get_lineage(
        "QUERY_SALES", direction="upstream", depth=6
    )
    assert not isinstance(graph, UnsupportedResult)
    assert any(n.name == "EXTSTRU_SALES" for n in graph.nodes), "the node must still be present"
    asked_about_it = [s for s in connection.seen if "EXTSTRU_SALES" in s]
    assert asked_about_it == [], (
        f"the extract structure was expanded as a BW object: {asked_about_it}"
    )


# --- depth counts load layers, not hops ---------------------------------------------------------


def test_resolving_the_query_to_its_provider_does_not_spend_a_depth_level() -> None:
    """From a query root, ``depth`` must mean what it means from the provider it reads.

    Charged a level, ``depth=2`` from the query reached only the DSO where the same request on the
    provider reached the DataSource - so the same number meant different things depending on which
    object the caller happened to name.
    """
    service = LineageService(ScriptedConnection(), _capability())
    from_query = service.get_lineage("QUERY_SALES", direction="upstream", depth=2)
    from_provider = service.get_lineage("SALES_CUBE", direction="upstream", depth=2)
    assert not isinstance(from_query, UnsupportedResult)
    assert not isinstance(from_provider, UnsupportedResult)
    reached_from_query = {n.name for n in from_query.nodes} - {"QUERY_SALES"}
    assert reached_from_query >= {n.name for n in from_provider.nodes}


def test_crossing_the_source_boundary_does_not_spend_a_depth_level_either() -> None:
    """Charged, the extractor appeared only for DataSources that had a level left over."""
    service = LineageService(ScriptedConnection(), _capability())
    # Exactly enough depth to reach the DataSource: the extract structure must come with it.
    graph = service.get_lineage("SALES_CUBE", direction="upstream", depth=2)
    assert not isinstance(graph, UnsupportedResult)
    names = {n.name for n in graph.nodes}
    assert "DS_SALES" in names
    assert "EXTSTRU_SALES" in names


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


def _elements() -> list[Any]:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    return list(query.elements)


def _edges() -> list[Any]:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    return list(query.edges)


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


# --- D28-D31: the element and axis decode -------------------------------------------------
#
# These four were found together by S04 and share one cause: RSZ* type and layout codes were being
# read one column at a time, when their meaning depends on a second input. They are the first
# defects in this project where the answer was *wrong* rather than incomplete - "75 restricted key
# figures" and "28 free characteristics" were numbers a reader would have taken at face value.


def test_selection_is_a_key_figure_only_when_it_restricts_the_key_figure_dimension() -> None:
    """D28: BW types both restricted key figures and characteristic placements 'SEL'.

    Deciding on the type code alone reported 78 characteristics as restricted key figures on the
    subject query. What separates them is whether the selection restricts 1KYFNM.
    """
    by_uid = {e.eltuid: e for e in _elements()}
    assert by_uid["E_RKF"].element_type == "restricted_key_figure"  # restricts 1KYFNM
    assert by_uid["E_MEMBER"].element_type == "restricted_key_figure"  # ditto
    assert by_uid["E_CHAR"].element_type == "characteristic"  # restricts MATERIAL only
    assert by_uid["E_FREE"].element_type == "characteristic"
    assert by_uid["E_FILTCHAR"].element_type == "characteristic"


def test_selection_with_no_restriction_row_is_not_guessed_either_way() -> None:
    """An unclassifiable selection says so instead of defaulting to key figure.

    E_SELOBJ and E_FUTURE are SEL with no RSZSELECT row. Calling them restricted key figures is the
    old bug in miniature; calling them characteristics would be the same mistake pointing the other
    way. 'selection' is the honest answer and stays distinct from 'unknown', which means an
    unrecognised *code*.
    """
    by_uid = {e.eltuid: e for e in _elements()}
    assert by_uid["E_SELOBJ"].element_type == "selection"
    assert by_uid["E_FUTURE"].element_type == "selection"


def test_query_sheet_is_decoded_rather_than_falling_through_to_unknown() -> None:
    """D29: the node owning every axis was typed 'unknown', so the tree was unreachable by type."""
    by_uid = {e.eltuid: e for e in _elements()}
    assert by_uid["E_SHEET"].element_type == "query_sheet"
    assert by_uid["E_FILTER"].element_type == "filter"


def test_axis_needs_the_parent_because_one_laytp_means_two_things() -> None:
    """D30: 'AGG' is a free characteristic under the sheet and a filter characteristic under the
    selection object. Reporting the literal code left the subject query's 37 free characteristics
    and 9 filter characteristics indistinguishable, and its 28 column members labelled 'free'.
    """
    axes = {(e.parent_uid, e.child_uid): e.axis for e in _edges()}
    assert axes[("E_SHEET", "E_FREE")] == "free_characteristics"
    assert axes[("E_FILTER", "E_FILTCHAR")] == "filter"
    # Same code, different parent, different answer - which is the whole point.
    free_edge = next(e for e in _edges() if e.child_uid == "E_FREE")
    filt_edge = next(e for e in _edges() if e.child_uid == "E_FILTCHAR")
    assert free_edge.role_code == filt_edge.role_code == "AGG"
    assert free_edge.axis != filt_edge.axis
    # And 'FLT' under the sheet is a structure member, not a free characteristic as its literal
    # decode suggests.
    assert axes[("E_SHEET", "E_MEMBER")] == "structure_member"
    assert next(e for e in _edges() if e.child_uid == "E_MEMBER").role == "free"


def test_axis_keeps_the_literal_role_decode_beside_it() -> None:
    """``axis`` is derived and ``role`` is read; neither replaces the other.

    ``role`` is a faithful single-column decode against SAP's own domain and D24 rests on it, so the
    fix adds a derived field rather than overwriting a correct one. Both are present on every edge.
    """
    for edge in _edges():
        assert edge.role is not None
        assert edge.axis is not None
        assert edge.axis_basis is not None, "every edge says how its axis was arrived at"
        assert "LAYTP=" in edge.axis_basis


def test_unmapped_parent_and_laytp_pair_reports_unknown_and_names_itself() -> None:
    """A pair with no measured meaning is visible rather than folded into a neighbour."""
    future = next(e for e in _edges() if e.child_uid == "E_FUTURE")
    assert future.axis == "unknown"
    assert "LAYTP=ZZZ" in (future.axis_basis or "")
    assert "no measured meaning" in (future.axis_basis or "")
    # NIL is settled without consulting the pair map: referenced, not placed, under any parent.
    reuse = next(e for e in _edges() if e.child_uid == "E_REUSE")
    assert reuse.axis == "unplaced"


def test_filter_element_reports_the_restrictions_its_children_hold() -> None:
    """D31: the filter returned restrictions=[] while the query plainly filtered on something.

    On the subject query that hid nine filtered characteristics, six of them restricted by an
    authorisation variable - the same exposure the unsupported RSECVAL path already fails to report,
    so the two gaps compounded.
    """
    filter_element = next(e for e in _elements() if e.eltuid == "E_FILTER")
    names = {r.iobjnm for r in filter_element.restrictions}
    assert "REGION" in names, "the filter must surface what it filters on"
    region = next(r for r in filter_element.restrictions if r.iobjnm == "REGION")
    assert region.low_is_variable is True, "an authorisation variable must survive the rollup"
    # Copied, not moved: the child is still where the restriction is stored.
    child = next(e for e in _elements() if e.eltuid == "E_FILTCHAR")
    assert {r.iobjnm for r in child.restrictions} == {"REGION"}


def test_filter_rollup_does_not_duplicate_a_restriction() -> None:
    """A filter reached by more than one edge must not list the same restriction twice."""
    original = list(_XREF["E_FILTER"])
    _XREF["E_FILTER"] = [*original, ("E_FILTCHAR", "AGG", 2)]
    try:
        filter_element = next(e for e in _elements() if e.eltuid == "E_FILTER")
        regions = [r for r in filter_element.restrictions if r.iobjnm == "REGION"]
        assert len(regions) == 1
    finally:
        _XREF["E_FILTER"] = original


def test_unrestricted_characteristic_is_still_classified_and_named() -> None:
    """The mistake this fix nearly shipped with: classifying a selection from RSZRANGE.

    A free characteristic is a selection with *no* value restriction, so it has no RSZRANGE row at
    all - its identity lives only in RSZSELECT. Reading the ranges left 32 of the subject query's
    placements as unclassified 'selection' while the answer sat in the other table.
    """
    free = _element("E_FREE")
    assert free.element_type == "characteristic"
    assert free.iobjnm == "PLANT"
    # The distinction is real in the fixture, not just asserted: named in RSZSELECT, no range row.
    assert "E_FREE" in _SELECT
    assert not any(r.iobjnm == "PLANT" for r in free.restrictions)


def test_a_characteristic_placement_names_the_infoobject_it_places() -> None:
    """These elements have no MAPNAME, so without RSZSELECT they are anonymous rows.

    On the subject query only 31 of 107 elements carried any identifier, which is what forced the
    comparison harness to fall back to matching on description text.
    """
    by_uid = {e.eltuid: e for e in _elements()}
    assert by_uid["E_CHAR"].name is None, "no technical name of its own"
    assert by_uid["E_CHAR"].iobjnm == "MATERIAL", "but it does say what it places"
    assert by_uid["E_FILTCHAR"].iobjnm == "REGION"


def test_the_key_figure_dimension_is_not_reported_as_a_characteristic() -> None:
    """1KYFNM is BW's key-figure dimension, not something a user drills down by."""
    rkf = _element("E_RKF")
    assert rkf.element_type == "restricted_key_figure"
    assert rkf.iobjnm is None, "iobjnm is for characteristic placements only"
    member = _element("E_MEMBER")
    assert member.iobjnm is None


def test_without_rszselect_a_selection_is_not_classified_by_guesswork() -> None:
    """Without the table, a selection is classified only where SAP declares the type (D26).

    This test previously asserted that *every* selection stays ``selection`` when ``RSZSELECT`` is
    gone, which was right while the 1KYFNM heuristic was the only route. It is now too strong:
    ``SUBDEFTP`` lives on ``RSZELTDIR``, which is present here, and reading SAP's own declared
    element type is the opposite of guessing. So the guarantee the test exists to protect is
    narrower and sharper than it was - an element with **no** basis is still not classified.
    """
    present = set(_TABLES) - {"element_select"}
    query = _repo(present).get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    kinds = {e.eltuid: e.element_type for e in query.elements}
    # Declared in SUBDEFTP, so still answerable on a release with no RSZSELECT at all.
    assert kinds["E_RKF"] == "restricted_key_figure"
    assert kinds["E_CHAR"] == "characteristic"
    assert kinds["E_COND"] == "condition"
    # Blank SUBDEFTP and no RSZSELECT: nothing to classify from, so nothing is claimed.
    assert kinds["E_SELOBJ"] == "selection"
    assert kinds["E_FUTURE"] == "selection"
    # iobjnm comes from RSZSELECT alone, so it is absent throughout regardless of the type.
    assert all(e.iobjnm is None for e in query.elements)


# --- D26: conditions and exceptions ---------------------------------------------------------


def _conditions() -> dict[str, Any]:
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    return {c.eltuid: c for c in query.conditions}


def test_a_condition_is_no_longer_counted_as_a_characteristic() -> None:
    """D26: the defect was a wrong count, not only a silence.

    A condition restricts no InfoObject, so the D28 ``1KYFNM`` split had nowhere to put it and it
    landed in the characteristic bucket. On the subject production query that made 4 of the 45
    reported characteristics conditions - verified against the server's own sealed output, which
    typed all four as ``characteristic``. ``RSZELTDIR.SUBDEFTP`` states the type outright.
    """
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    kinds = {e.eltuid: e.element_type for e in query.elements}
    assert kinds["E_COND"] == "condition"
    assert kinds["E_EXC"] == "exception"
    # And they are gone from the characteristic count, which is the half that was wrong.
    assert "E_COND" not in {u for u, k in kinds.items() if k == "characteristic"}
    # A condition is not a characteristic placement, so it names no InfoObject.
    assert next(e for e in query.elements if e.eltuid == "E_COND").iobjnm is None


def test_the_structure_element_subtype_is_deliberately_left_on_the_heuristic() -> None:
    """``STM`` is declared but not remapped, and that is a decision rather than an oversight.

    SAP calls 12,357 live elements structure elements where the heuristic calls 12,201 of them
    restricted key figures. Which one a reader should see depends on what Query Designer shows, and
    no ground truth has been collected for it - so reclassifying them on my own authority would be
    exactly the unverified change this project keeps refusing to make.
    """
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    kinds = {e.eltuid: e.element_type for e in query.elements}
    assert kinds["E_MEMBER"] == "restricted_key_figure"


def test_a_switched_off_condition_says_so() -> None:
    """``active`` is the first field to read: a condition that is off changes nothing a reader sees.

    All 4 conditions on the subject query are inactive, and the flag genuinely varies - 45 of 134
    condition rows on the reference system are blank. Reporting "4 Top N conditions" without it
    would describe a query that behaves quite differently from the one that exists.
    """
    conditions = _conditions()
    assert conditions["E_COND"].active is False
    assert conditions["E_EXC"].active is True


def test_a_condition_reports_the_operator_and_never_invents_one() -> None:
    """The ranking operators are not declared fixed values, and the decode has to admit that.

    ``TC`` is used 107 times on the reference system and ``RSZ_OPERATOR_DOMAIN`` does not list it,
    so its label comes from the customer's Query Designer export (4 of 4) and its confidence stays
    ``advisory``. A declared threshold operator decodes from the dictionary. ``BC`` is the case that
    matters most: symmetry makes "Bottom N" obvious, and precisely because it is merely obvious it
    must arrive with the raw code and no label.
    """
    conditions = _conditions()
    topn = conditions["E_COND"].operator
    assert topn is not None
    assert (topn.code, topn.label, topn.confidence) == ("TC", "Top N (count)", "advisory")

    # A declared threshold operator decodes from the dictionary. On an exception it sits on the
    # alert band, not on the record, because an exception has no single operator (D41).
    band = conditions["E_EXC"].alert_levels[0].operator
    assert band is not None
    assert band.code == "BT"
    assert band.confidence == "dictionary"
    assert band.label is not None and "Between" in band.label

    undeclared = _decode_operator("BC")
    assert undeclared is not None
    assert (undeclared.code, undeclared.label, undeclared.confidence) == ("BC", None, "advisory")


def test_a_condition_threshold_says_whether_it_is_a_number_or_a_variable() -> None:
    """The figure a condition cuts at is usually not in metadata at all.

    ``LOWFLAG`` is a declared value-source flag, so this is answerable exactly rather than guessed
    from the shape of the string in ``LOW``. When it names a variable the cut-off resolves per
    execution, which ``runtime_resolved`` states so the value is not read as a constant.
    """
    conditions = _conditions()
    by_variable = conditions["E_COND"]
    assert by_variable.threshold == "E_CONDVAR"
    assert by_variable.threshold_source is not None
    assert by_variable.threshold_source.value_holds == "variable_name"
    assert by_variable.threshold_source.runtime_resolved is True

    # An exception has no single threshold at all - it has bands. Asserting it here would be
    # asserting a shape the data does not have, which is the mistake D41 encoded in code.
    exception = conditions["E_EXC"]
    assert exception.threshold is None
    assert exception.threshold_source is None
    assert exception.operator is None
    assert len(exception.alert_levels) == 3


def test_a_condition_names_the_key_figure_it_ranks() -> None:
    """Which figure is ranked is the other half of the answer, and it is an element reference.

    The two ``RSZRANGE`` rows are told apart by ``FACIOBJNM``, not by ``ENUM`` order, so the measure
    row is identified by what it is for rather than by where it happens to sit.
    """
    conditions = _conditions()
    assert conditions["E_COND"].measure_eltuid == "E_MEMBER"
    ranked = conditions["E_EXC"]
    assert ranked.measure_eltuid == "E_RKF"
    # The element tree already carries that element's text, so the reference resolves to a name.
    assert ranked.measure_description == "Net amount RKF"


def test_the_second_declared_column_confirms_the_condition_kind() -> None:
    """``CONTYPE`` says the same thing ``SUBDEFTP`` does, and two declared columns agreeing is why
    this decode is not resting on one reading of four rows."""
    conditions = _conditions()
    condition_type = conditions["E_COND"].condition_type
    assert condition_type is not None
    assert (condition_type.code, condition_type.label) == ("1", "Condition")
    exception_type = conditions["E_EXC"].condition_type
    assert exception_type is not None
    assert (exception_type.code, exception_type.label) == ("2", "Exception")


def test_a_condition_carries_provenance_for_all_three_tables_it_came_from() -> None:
    """One record assembled from three tables cites three, so any fact in it stays traceable."""
    conditions = _conditions()
    tables = {p.source_table for p in conditions["E_COND"].provenance}
    assert tables == {"RSZELTDIR", "RSZSELECT", "RSZRANGE"}


# --- D41: the exception shape, which the first version of the condition reader got wrong ---------


def test_an_exception_is_read_under_its_own_pseudo_infoobject() -> None:
    """D41: a condition files under ``1CONDITION`` and an exception under ``1EXCEPTION``.

    The first version of this reader hardcoded ``1CONDITION``, so every exception matched nothing:
    no operator, no threshold, no measure, and ``active=False`` from the missing-row default. That
    last part is the damaging half - **71 of the reference system's 77 exception rows are switched
    on**, and all of them were being reported off. The fixture applies the ``IOBJNM`` filter, so a
    regression to one name makes this fail rather than pass quietly.
    """
    conditions = _conditions()
    exception = conditions["E_EXC"]
    assert exception.kind == "exception"
    assert exception.active is True, "reading the wrong pseudo-InfoObject reports a live one as off"
    assert exception.measure_eltuid == "E_RKF"
    assert exception.condition_type is not None
    assert exception.condition_type.label == "Exception"


def test_an_exception_reports_every_alert_band_not_just_one() -> None:
    """An exception is a set of bands, and the level is what gives a band its meaning.

    ``ALERTLEVEL`` decodes against its own declared domain ``RSRA_ALERT_LEVEL`` - nine values, Good
    1-3, Critical 1-3, Bad 1-3, all nine of which occur on the reference system. The first reader
    collapsed to a single threshold, which on the observed row-count histogram
    ``{2: 18, 3: 19, 4: 32, 5: 2, 6: 6}`` would have dropped bands from **59 of 77** exceptions.
    """
    exception = _conditions()["E_EXC"]
    bands = {level.level.code: level for level in exception.alert_levels}
    assert set(bands) == {"09", "05", "01"}
    assert bands["09"].level.label == "Bad 3"
    assert bands["05"].level.label == "Critical 2"
    assert bands["01"].level.label == "Good 1"
    assert all(b.level.confidence == "dictionary" for b in exception.alert_levels)
    # Each band keeps its own bounds; a band is a range, not a single cut-off.
    assert (bands["05"].low, bands["05"].high) == ("0", "100")
    assert (bands["01"].low, bands["01"].high) == ("100", "99999999999")
    assert all(b.provenance.source_table == "RSZRANGE" for b in exception.alert_levels)


def test_a_condition_has_no_alert_bands_and_that_is_measured_not_assumed() -> None:
    """All 254 condition range rows on the reference system carry ``ALERTLEVEL='00'``.

    So an empty band list on a condition is a fact about conditions, not a gap. This is why the two
    kinds keep different shapes instead of being flattened into one: flattening would have to invent
    a level for conditions or drop the levels from exceptions.
    """
    conditions = _conditions()
    assert conditions["E_COND"].alert_levels == []
    assert conditions["E_COND"].threshold == "E_CONDVAR"
    assert conditions["E_COND"].evaluation_scope is None
    assert conditions["E_COND"].drilldown_characteristics == []


def test_an_exception_names_the_characteristics_it_is_evaluated_at() -> None:
    """Rows whose ``FACIOBJNM`` is a real characteristic are drilldown levels, not comparisons.

    They carry ``OPT='NA'``, which is not a declared operator either. The first reader looked only
    at ``1STRUC`` and ``1VALUE`` and dropped them, leaving "which rows get coloured" unanswerable.
    """
    exception = _conditions()["E_EXC"]
    assert exception.drilldown_characteristics == ["MATERIAL"]


def test_an_exception_says_whether_it_colours_totals_or_every_row() -> None:
    """``EXCABSREL`` is declared, and it changes what a reader actually sees on screen."""
    scope = _conditions()["E_EXC"].evaluation_scope
    assert scope is not None
    assert (scope.code, scope.label, scope.confidence) == ("2", "All", "dictionary")


def test_without_the_definition_tables_a_condition_is_still_reported_as_one() -> None:
    """Degradation has to keep the part that is known and drop only the part that is not.

    On a release with no ``RSZSELECT``/``RSZRANGE``, the element is still declared a condition by
    ``SUBDEFTP``, so "this query has a condition" is answerable while its operator and threshold are
    not. Reporting nothing would lose a fact the system does hold; reporting ``active=True`` would
    invent one, so it degrades to inactive.
    """
    present = set(_TABLES) - {"element_select", "element_range"}
    query = _repo(present).get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    conditions = {c.eltuid: c for c in query.conditions}
    assert set(conditions) == {"E_COND", "E_EXC"}
    assert conditions["E_COND"].kind == "condition"
    assert conditions["E_EXC"].kind == "exception"
    assert conditions["E_COND"].operator is None
    assert conditions["E_COND"].threshold is None
    assert conditions["E_COND"].active is False


# --- D25: variables reachable only by element uid ------------------------------------------


def test_a_variable_with_no_mapname_is_found_through_its_element_uid() -> None:
    """D25: keying the variables list on MAPNAME alone loses a large share of them.

    Measured on the reference system, 801 of 2,188 active variable elements (36.6%) carry a blank
    MAPNAME - a variable reached through a query condition is one such case. All 801 are nameable
    through RSZGLOBV.VARUNIID, which holds the element uid and is populated on every row, so the
    join recovers them all and loses nothing.
    """
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    names = {v.name for v in query.variables}
    assert "COND_TOPN" in names, "the element carries no MAPNAME; only VARUNIID names it"
    topn = next(v for v in query.variables if v.name == "COND_TOPN")
    assert topn.kind == "formula"
    assert topn.processing_type == "user_entry"
    assert topn.input_ready is True
    # The element really is unnamed, so this is not passing by the MAPNAME route.
    element = next(e for e in query.elements if e.eltuid == "E_CONDVAR")
    assert element.element_type == "variable"
    assert element.name is None


def test_a_variable_found_by_both_routes_is_listed_once() -> None:
    """USD_VAR is reachable by name from a restriction; it must not appear twice."""
    query = _repo().get_query("QUERY_SALES")
    assert not isinstance(query, UnsupportedResult)
    names = [v.name for v in query.variables]
    assert names.count("USD_VAR") == 1
    assert len(names) == len(set(names))


# --- the catalogue must read one version of each query ----------------------------------------


def test_the_query_catalogue_reads_only_the_active_version() -> None:
    """``RSZCOMPDIR`` holds a row per *version*, so an unfiltered catalogue counts each query once
    per version it exists in.

    Measured on the reference system before this was fixed: **2,373 rows for 1,069 active queries**
    (A 1,069, M 890, D 279, B 135). Three consequences, all of them visible to a user: the total
    over-reported by 2.2x, the generated index listed a query up to four times with a different
    "last used" date against each, and a documentation run rebuilt the same page once per duplicate.

    ``OBJSTAT = 'ACT'`` does not substitute for the version filter. It is the activation state of a
    row, and the modified and delivered versions carry it too - which is exactly why the omission
    survived: the catalogue looked filtered.
    """

    class VersionedConnection(ScriptedConnection):
        """Four versions of one query, with the version filter applied as a database would."""

        @staticmethod
        def _compdir(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
            if "TSTPNM" in sql:  # the header read, unrelated to this
                return ScriptedConnection._compdir(sql, params)
            every_version = [
                ("Q1UID", "SALES_QUERY", "OWNER1", _HEADER[4]),  # A - the live definition
                ("Q1UID_M", "SALES_QUERY", "OWNER1", _HEADER[4]),  # M - being edited
                ("Q1UID_D", "SALES_QUERY", "OWNER1", _HEADER[4]),  # D - shipped by SAP
                ("Q1UID_B", "SALES_QUERY", "OWNER1", _HEADER[4]),  # B - backup
            ]
            return every_version[:1] if "OBJVERS = 'A'" in sql else every_version

    result = QueriesRepository(VersionedConnection(), _capability()).list_queries(limit=50)
    assert not isinstance(result, UnsupportedResult)
    summaries, _total = result
    assert [s.compuid for s in summaries] == ["Q1UID"], (
        "the catalogue listed more than one version of the same query; it must read OBJVERS = 'A'"
    )
