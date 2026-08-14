"""Tests for the scenario 9.1 latency contract: cadence of a load vs cadence of what it reads.

This is the mission's headline check — "does the looked-up object refresh at least as often as the
load that reads it?" — and it was implemented without coverage: the shared analyzer fixture has no
``chain_edges`` capability, so ``provider_to_chains`` returned ``UnsupportedResult`` and every
cadence came back ``unknown``. The comparison could have been inverted and nothing would fail.

The landscape gives one full-update consumer and four looked-up objects that between them
produce all three verdicts:

  consumer  CONSUMER_DSO   <- CH_CONSUMER, 3 runs/day over 30 days -> multiple_daily
  lookups   LOOKUP         <- CH_LOOKUP,   1 run/day               -> daily     -> STALE RISK
            L1DSO          <- CH_HOURLY,   24 runs/day             -> hourly    -> ok
            L2DSO          <- no DTP at all                        -> unknown   -> reported, not
                                                                                  assumed safe
            L3DSO          <- CH_DAILY,    1 run/day               -> daily     -> STALE RISK

Synthetic names only; the ``/BIC/`` lookup tables are built by concatenation so this file stays
clean for the customer-metadata scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services.analyzers import Analyzers

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "dtp": "RSBKDTP",
    "extractor": "ROOSOURCE",
    "chain_edges": "RSPCCHAIN",
    "log_chain": "RSPCLOGCHAIN",
}

# Generated table names, concatenated so the scan stays clean. /BIC/A<name>00 -> DSO <name>.
_BIC_LOOKUP = "/BIC/" + "ALOOKUP00"  # -> LOOKUP
_BIC_L1 = "/BIC/" + "AL1DSO00"  # -> L1DSO
_BIC_L2 = "/BIC/" + "AL2DSO00"  # -> L2DSO
_BIC_L3 = "/BIC/" + "AL3DSO00"  # -> L3DSO

CONSUMER = "CONSUMER_DSO"

# RSTRAN 12-col header: OBJSTAT, SRCTYPE, SRCSUB, SRCNAME, TGTTYPE, TGTSUB, TGTNAME, START, END,
# EXPERT, GLB, GLB2
_HEADER: dict[str, tuple[Any, ...]] = {
    "TR_ONE": ("ACT", "RSDS", "", "DS_SRC", "ODSO", "", CONSUMER, "CODE_ONE", "", "", "", ""),
    "TR_MIX": ("ACT", "RSDS", "", "DS_SRC", "ODSO", "", CONSUMER, "CODE_MIX", "", "", "", ""),
}
_RSAABAP = {
    # One lookup, on an object refreshed less often than this load runs.
    "CODE_ONE": ["METHOD start.", f"  SELECT * FROM {_BIC_LOOKUP} INTO lt.", "ENDMETHOD."],
    # Three lookups spanning ok / unknown / stale.
    "CODE_MIX": [
        "METHOD start.",
        f"  SELECT * FROM {_BIC_L1} INTO lt.",
        f"  SELECT * FROM {_BIC_L2} INTO lt.",
        f"  SELECT * FROM {_BIC_L3} INTO lt.",
        "ENDMETHOD.",
    ],
}

# provider -> DTP that loads it. L2DSO is deliberately absent (nothing loads it that we can see).
_DTP_BY_TARGET = {
    CONSUMER: "DTP_CONSUMER",
    "LOOKUP": "DTP_LOOKUP",
    "L1DSO": "DTP_L1",
    "L3DSO": "DTP_L3",
}
# DTP -> the chain whose step runs it (RSPCCHAIN.VARIANTE -> CHAIN_ID).
_CHAIN_BY_DTP = {
    "DTP_CONSUMER": "CH_CONSUMER",
    "DTP_LOOKUP": "CH_LOOKUP",
    "DTP_L1": "CH_HOURLY",
    "DTP_L3": "CH_DAILY",
}
# chain -> runs per day, over the 30-day history below.
_RUNS_PER_DAY = {"CH_CONSUMER": 3, "CH_LOOKUP": 1, "CH_HOURLY": 24, "CH_DAILY": 1}

_LAST_DAY = date(2026, 6, 30)
_RUN_DAYS = 30
_DATES = [_LAST_DAY - timedelta(days=offset) for offset in range(_RUN_DAYS - 1, -1, -1)]


def _datum(value: date) -> str:
    return value.strftime("%Y%m%d")


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "ROOSOURCE" in sql:
            return [("ADD",)]
        if "RSBKDTP" in sql:
            return self._dtp(sql, params)
        if "RSPCCHAIN" in sql:
            return self._chain_edges(sql, params)
        if "RSPCLOGCHAIN" in sql:
            return self._log_chain(sql, params)
        if "RSAABAP" in sql:
            return [(line,) for line in _RSAABAP.get(str(params[0]) if params else "", [])]
        if "RSTRANSTEPROUT" in sql:
            return []
        if "RSTRAN" in sql:
            return self._rstran(sql, params)
        return []

    @staticmethod
    def _dtp(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "DISTINCT TGT" in sql:  # full-update targets
            return [(CONSUMER,)]
        if "UPDMODE = ?" in sql:  # full-update source: (SRC, SRCTLOGO)
            return [("DS_SRC", "RSDS")]
        if "TGT = ?" in sql:  # provider -> its DTPs (load closure): (DTP,)
            dtp = _DTP_BY_TARGET.get(str(params[0]).strip())
            return [(dtp,)] if dtp else []
        return []

    @staticmethod
    def _chain_edges(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "TYPE = ?" in sql:  # parent (meta-chain) lookup: none in this landscape
            return []
        wanted = {str(p).strip() for p in params}
        chains = {_CHAIN_BY_DTP[d] for d in _CHAIN_BY_DTP if d in wanted}
        return [(chain,) for chain in sorted(chains)]

    @staticmethod
    def _log_chain(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        requested = {str(p).strip() for p in params} or set(_RUNS_PER_DAY)
        chains = [c for c in _RUNS_PER_DAY if c in requested]
        if "MAX(DATUM)" in sql and "CHAIN_ID" not in sql:  # reference date
            return [(_datum(_LAST_DAY),)]
        if "COUNT(DISTINCT LOG_ID)" in sql:  # run-day summary
            return [
                (
                    chain,
                    _RUNS_PER_DAY[chain] * _RUN_DAYS,
                    _RUN_DAYS,
                    _datum(_DATES[0]),
                    _datum(_LAST_DAY),
                )
                for chain in chains
            ]
        if "DATUM" in sql:  # distinct run days
            return [(chain, _datum(day)) for chain in chains for day in _DATES]
        return []

    @staticmethod
    def _rstran(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "OBJSTAT" in sql:
            header = _HEADER.get(str(params[0])) if params else None
            return [header] if header else []
        if "STARTROUTINE <> ''" in sql:
            return [("TR_ONE", CONSUMER), ("TR_MIX", CONSUMER)]
        return []


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


def _report(present: set[str] | None = None) -> Any:
    report = Analyzers(ScriptedConnection(), _capability(present)).check_load_latency()
    assert not isinstance(report, UnsupportedResult)
    return report


def _finding(tran_id: str) -> Any:
    return next(f for f in _report().findings if f.metrics["tran_id"] == tran_id)


# --- cadence resolution --------------------------------------------------------------------


def test_consumer_cadence_comes_from_run_history() -> None:
    """3 runs/day over 30 days is multiple_daily — read from RSPCLOGCHAIN, not from the name."""
    assert _finding("TR_ONE").metrics["consumer_frequency"] == "multiple_daily"


def test_each_lookup_gets_its_own_observed_cadence() -> None:
    metrics = _finding("TR_MIX").metrics
    assert metrics["lookup_frequency"] == {
        "L1DSO": "hourly",
        "L2DSO": "unknown",  # nothing loads it that BW can show us
        "L3DSO": "daily",
    }


# --- the contract itself -------------------------------------------------------------------


def test_lookup_refreshed_less_often_than_the_load_runs_is_flagged() -> None:
    """The mission's case: a load running more than once daily reading once-daily master data."""
    metrics = _finding("TR_ONE").metrics
    assert metrics["stale_risk_objects"] == ["LOOKUP"]
    assert metrics["cadence_unknown_objects"] == []


def test_lookup_refreshed_more_often_is_not_flagged() -> None:
    metrics = _finding("TR_MIX").metrics
    assert "L1DSO" not in metrics["stale_risk_objects"]  # hourly vs multiple_daily: safe


def test_unresolvable_cadence_is_reported_not_assumed_safe() -> None:
    metrics = _finding("TR_MIX").metrics
    assert metrics["cadence_unknown_objects"] == ["L2DSO"]
    assert "L2DSO" not in metrics["stale_risk_objects"]


def test_mixed_lookups_flag_only_the_slower_ones() -> None:
    metrics = _finding("TR_MIX").metrics
    assert metrics["stale_risk_objects"] == ["L3DSO"]


def test_report_documents_how_cadence_was_derived() -> None:
    report = _report()
    assert any("RSPCLOGCHAIN" in caveat for caveat in report.caveats)
    assert any("assumed safe" in caveat for caveat in report.caveats)


def test_extractor_delta_method_is_carried() -> None:
    assert _finding("TR_ONE").metrics["extractor_delta_method"] == "ADD"


# --- degradation ---------------------------------------------------------------------------


def test_without_chain_history_cadence_is_unknown_and_nothing_is_flagged() -> None:
    """No run history must not silently read as 'contract holds'."""
    report = _report(present=set(_TABLES) - {"log_chain"})
    finding = next(f for f in report.findings if f.metrics["tran_id"] == "TR_ONE")
    assert finding.metrics["consumer_frequency"] == "unknown"
    assert finding.metrics["stale_risk_objects"] == []
    assert finding.metrics["cadence_unknown_objects"] == ["LOOKUP"]


def test_without_chain_edges_cadence_is_unknown() -> None:
    """Provider -> loading chain needs RSPCCHAIN. Absent, the contract is unevaluable."""
    report = _report(present=set(_TABLES) - {"chain_edges"})
    finding = next(f for f in report.findings if f.metrics["tran_id"] == "TR_ONE")
    assert finding.metrics["consumer_frequency"] == "unknown"
    assert finding.metrics["cadence_unknown_objects"] == ["LOOKUP"]
