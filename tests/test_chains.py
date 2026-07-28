"""Tests for the repository base and chains repository (B3), offline against scripted fixtures.

Synthetic chain/process names only (no Z*/Y* customer-namespace names, no /BIC/).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.chains import (
    ChainsRepository,
    _classify_frequency,
    _parse_bw_timestamp,
    _parse_dats_tims,
    _percentile,
)
from tests.sqllike import escape_for_sql, matches_like

SCHEMA = "TESTSCHEMA"
_CHAIN_TABLES = {
    "chain_attr": "RSPCCHAINATTR",
    "chain_edges": "RSPCCHAIN",
    "chain_text": "RSPCCHAINT",
    "log_chain": "RSPCLOGCHAIN",
    "process_log": "RSPCPROCESSLOG",
}


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    present = present if present is not None else set(_CHAIN_TABLES)
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
            for logical, physical in _CHAIN_TABLES.items()
        },
    )


def _ts(text: str) -> Decimal:
    """Build an RSPCPROCESSLOG-style decimal timestamp from 'YYYYMMDDHHMMSS.fff'."""
    return Decimal(text)


def _filtered_chains(sql: str, params: list[Any]) -> list[str]:
    """Active chain ids, honouring a ``CHAIN_ID LIKE`` filter the way HANA would.

    Parameter order follows the built WHERE clause: OBJSTAT (when active_only) then the LIKE value.
    """
    active = [c for c in sorted(_ATTR) if _ATTR[c][1] == "ACT"]
    if "CHAIN_ID LIKE" not in sql:
        return active
    pattern = str(params[1 if "OBJSTAT = ?" in sql else 0])
    return [c for c in active if matches_like(pattern, c, escape_for_sql(sql))]


# Fixture landscape (synthetic).
# MASTER_CHAIN: TRIGGER -> (green) CHAIN:SUBCHAIN_1 -> (green) LOADING:LOAD_A
_EDGES = {
    "MASTER_CHAIN": [
        # (TYPE, VARIANTE, LNR, EVENTP_START, EVENTP_GREEN, EVENTP_RED)
        ("TRIGGER", "START", 1, "", "p1", ""),
        ("CHAIN", "SUBCHAIN_1", 1, "p1", "p2", ""),
        ("LOADING", "LOAD_A", 1, "p2", "", ""),
    ],
    "SUBCHAIN_1": [
        ("TRIGGER", "START", 1, "", "s1", ""),
        ("LOADING", "LOAD_B", 1, "s1", "", ""),
    ],
}
_ATTR = {  # CHAIN_ID -> (APPLNM, OBJSTAT)
    "MASTER_CHAIN": ("SALESAREA", "ACT"),
    "SUBCHAIN_1": ("SALESAREA", "ACT"),
    # Sibling of SUBCHAIN_1 that only stays out of a 'SUBCHAIN_1' filter when '_' is escaped.
    "SUBCHAINX1": ("SALESAREA", "ACT"),
    "DAILY_LOAD": ("FINANCE", "ACT"),
}
_TEXT = {  # CHAIN_ID -> TXTLG
    "MASTER_CHAIN": "Master orchestration chain",
    "DAILY_LOAD": "Daily finance load",
}
# DAILY_LOAD runs: (LOG_ID, DATUM, ZEIT, ANALYZED_STATUS)
_RUNS = {
    "DAILY_LOAD": [
        ("L1", "20260720", "020000", "G"),
        ("L2", "20260721", "020000", "G"),
        ("L3", "20260722", "020000", "R"),
    ]
}
# steps per LOG_ID: (TYPE, VARIANTE, INSTANCE, STATE, START, END)
_STEPS = {
    "L1": [
        ("TRIGGER", "START", "1", "G", _ts("20260720020000.000"), _ts("20260720020001.000")),
        (
            "LOADING",
            "LOAD_A",
            "1",
            "G",
            _ts("20260720020001.000"),
            _ts("20260720021001.000"),
        ),  # 600s
    ],
    # L2 starts 02:00:30, before L1's end (02:10:01) -> one observed overlap
    "L2": [
        (
            "LOADING",
            "LOAD_A",
            "1",
            "G",
            _ts("20260721020030.000"),
            _ts("20260721020530.000"),
        ),  # 300s
    ],
    "L3": [
        (
            "LOADING",
            "LOAD_A",
            "1",
            "R",
            _ts("20260722020000.000"),
            _ts("20260722020200.000"),
        ),  # 120s
    ],
}


class ScriptedConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Sequence[Any] | None]] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.queries.append((sql, parameters))
        params = list(parameters or [])
        if "TOTAL_COUNT" in sql:
            if "RSPCCHAINATTR" in sql:
                return [(len(_filtered_chains(sql, params)),)]
            return [(0,)]
        if "RSPCCHAINATTR" in sql:
            active = _filtered_chains(sql, params)
            if "CHAIN_ID = ?" in sql:  # single header
                cid = params[-1]
                return [(_ATTR[cid][0], _ATTR[cid][1])] if cid in _ATTR else []
            if "APPLNM" in sql:  # list_chains page
                return [(c, _ATTR[c][0], _ATTR[c][1]) for c in active]
            return [(c,) for c in active]  # schedule matrix (CHAIN_ID only)
        if "RSPCCHAINT" in sql:
            wanted = {str(p) for p in params[1:]}  # first param is LANGU
            return [(c, _TEXT[c]) for c in _TEXT if c in wanted]
        if "RSPCLOGCHAIN" in sql:
            if "GROUP BY" in sql:  # run summary
                summary: list[tuple[Any, ...]] = []
                for cid, runs in _RUNS.items():
                    dats = [r[1] for r in runs]
                    summary.append((cid, len(runs), min(dats), max(dats)))
                return summary
            if "ANALYZED_STATUS" in sql:  # runtimes runs for one chain
                cid = params[0]
                return [(r[0], r[1], r[2], r[3]) for r in _RUNS.get(cid, [])]
            return [  # median start times (CHAIN_ID, ZEIT)
                (cid, r[2]) for cid, runs in _RUNS.items() for r in runs
            ]
        if "RSPCPROCESSLOG" in sql:
            log_ids = {str(p) for p in params}
            steps_out: list[tuple[Any, ...]] = []
            for lid, steps in _STEPS.items():
                if lid in log_ids:
                    steps_out.extend((lid, *s) for s in steps)
            return steps_out
        if "RSPCCHAIN" in sql:  # edges (checked last: substring of the others)
            cid = params[-1]
            return list(_EDGES.get(cid, []))
        return []


def _repo(present: set[str] | None = None) -> ChainsRepository:
    return ChainsRepository(ScriptedConnection(), _capability(present))


# --- helpers ------------------------------------------------------------------------------


def test_parse_bw_timestamp() -> None:
    assert _parse_bw_timestamp(Decimal("20160127135310.222")) == datetime(
        2016, 1, 27, 13, 53, 10, 222000
    )
    assert _parse_bw_timestamp(None) is None
    assert _parse_bw_timestamp(Decimal("0")) is None


def test_parse_dats_tims() -> None:
    assert _parse_dats_tims("20220118", "000211") == datetime(2022, 1, 18, 0, 2, 11)
    assert _parse_dats_tims("00000000", "000000") is None


def test_percentile() -> None:
    assert _percentile([10.0], 0.95) == 10.0
    assert _percentile([], 0.95) is None
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0


@pytest.mark.parametrize(
    ("rpd", "count", "expected"),
    [
        (25.0, 100, "hourly"),
        (3.0, 100, "multiple_daily"),
        (1.0, 100, "daily"),
        (0.14, 100, "weekly"),
        (0.03, 100, "monthly"),
        (0.005, 100, "irregular"),
        (5.0, 2, "unknown"),
        (None, 100, "unknown"),
    ],
)
def test_classify_frequency(rpd: float | None, count: int, expected: str) -> None:
    assert _classify_frequency(rpd, count) == expected


# --- gating -------------------------------------------------------------------------------


def test_require_returns_unsupported_when_absent() -> None:
    repo = _repo(present={"chain_edges", "chain_text"})  # chain_attr absent
    result = repo.list_chains()
    assert isinstance(result, UnsupportedResult)
    assert result.status == "unsupported_on_release"


# --- structure ----------------------------------------------------------------------------


def test_get_chain_edges_and_subchain_recursion() -> None:
    repo = _repo()
    chain = repo.get_chain("MASTER_CHAIN")
    assert not isinstance(chain, UnsupportedResult)
    assert {p.key for p in chain.processes} == {
        "TRIGGER:START",
        "CHAIN:SUBCHAIN_1",
        "LOADING:LOAD_A",
    }
    green = {(e.source_key, e.target_key) for e in chain.edges if e.link == "green"}
    assert ("TRIGGER:START", "CHAIN:SUBCHAIN_1") in green
    assert ("CHAIN:SUBCHAIN_1", "LOADING:LOAD_A") in green
    assert chain.subchain_ids == ["SUBCHAIN_1"]
    assert [s.chain_id for s in chain.subchains] == ["SUBCHAIN_1"]
    # every process carries provenance citing the physical table
    assert chain.processes[0].provenance.source_table == "RSPCCHAIN"


def test_get_chain_subchain_marked() -> None:
    repo = _repo()
    chain = repo.get_chain("MASTER_CHAIN")
    assert not isinstance(chain, UnsupportedResult)
    subref = next(p for p in chain.processes if p.key == "CHAIN:SUBCHAIN_1")
    assert subref.is_subchain is True
    assert subref.subchain_id == "SUBCHAIN_1"


# --- runtimes -----------------------------------------------------------------------------


def test_get_chain_runtimes_stats() -> None:
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    assert rt.total_runs == 3
    assert rt.successful_runs == 2
    assert rt.success_rate == pytest.approx(2 / 3, abs=0.01)
    # per-run durations: L1=601s, L2=300s, L3=120s
    assert rt.duration_seconds.max_s == pytest.approx(601.0, abs=1.0)
    assert rt.duration_seconds.min_s == pytest.approx(120.0, abs=1.0)
    # bottleneck: the 600s LOAD_A step is the longest
    assert rt.bottleneck_steps[0].duration_s == pytest.approx(600.0, abs=1.0)
    assert rt.bottleneck_steps[0].provenance.source_table == "RSPCPROCESSLOG"
    assert any("bottleneck" in c or "approximation" in c for c in rt.caveats)


def test_get_chain_runtimes_window_capped_at_one_year() -> None:
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=99999)
    assert not isinstance(rt, UnsupportedResult)
    assert rt.window_days_requested == 365  # capped


def test_get_chain_runtimes_no_runs() -> None:
    repo = _repo()
    rt = repo.get_chain_runtimes("UNKNOWN_CHAIN", days=90)
    assert not isinstance(rt, UnsupportedResult)
    assert rt.total_runs == 0
    assert rt.success_rate is None


# --- listing ------------------------------------------------------------------------------


def test_list_chains() -> None:
    repo = _repo()
    result = repo.list_chains(window_days=90)
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 4
    by_id = {s.chain_id: s for s in summaries}
    assert by_id["DAILY_LOAD"].description == "Daily finance load"
    assert by_id["MASTER_CHAIN"].active is True
    assert summaries[0].provenance  # provenance present


def test_list_chains_name_pattern_is_a_substring_match() -> None:
    """Regression: name_pattern went into ``CHAIN_ID LIKE ?`` verbatim, so a bare term (the natural
    thing for a caller to pass) silently matched nothing at all."""
    result = _repo().list_chains(name_pattern="DAILY")
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 1
    assert [s.chain_id for s in summaries] == ["DAILY_LOAD"]


def test_list_chains_name_pattern_underscore_is_literal() -> None:
    result = _repo().list_chains(name_pattern="SUBCHAIN_1")
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert [s.chain_id for s in summaries] == ["SUBCHAIN_1"]  # SUBCHAINX1 must not match
    assert total == 1


def test_list_chains_name_pattern_honours_caller_wildcards() -> None:
    result = _repo().list_chains(name_pattern="SUBCHAIN_1")
    wildcard = _repo().list_chains(name_pattern="%SUBCHAIN_1%")
    assert not isinstance(result, UnsupportedResult)
    assert not isinstance(wildcard, UnsupportedResult)
    # '_' is a single-character wildcard when the caller authors the pattern.
    assert {s.chain_id for s in wildcard[0]} == {"SUBCHAIN_1", "SUBCHAINX1"}
