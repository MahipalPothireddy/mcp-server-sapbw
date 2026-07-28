"""Tests for observed-cadence classification and the chain <-> provider load closure.

Offline against a scripted landscape. Synthetic names only.

Landscape: META_CHAIN -> (SUB_A, SUB_B). SUB_A loads STG_DSO (delta) and EDW_DSO (full) via DTPs;
SUB_B loads MART_ADSO. META_CHAIN also runs housekeeping and orchestration steps. CYCLE_A and
CYCLE_B call each other (BW permits it) to prove the recursion is cycle-guarded.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.services.load_closure import LoadClosureService, cadence_of

SCHEMA = "TESTSCHEMA"
_TABLES = {"chain_edges": "RSPCCHAIN", "dtp": "RSBKDTP", "log_chain": "RSPCLOGCHAIN"}

# chain -> [(TYPE, VARIANTE)]
_STEPS: dict[str, list[tuple[str, str]]] = {
    "META_CHAIN": [
        ("CHAIN", "SUB_A"),
        ("CHAIN", "SUB_B"),
        ("AND", ""),
        ("PSADELETE", "CLEANUP"),
        ("ABAP", "CUSTOM_PROG"),
    ],
    "SUB_A": [("DTP_LOAD", "DTP_STG"), ("DTP_LOAD", "DTP_EDW"), ("ADSOACT", "ACT1")],
    "SUB_B": [("DTP_LOAD", "DTP_MART"), ("DTP_LOAD", "DTP_GONE")],
    "CYCLE_A": [("CHAIN", "CYCLE_B")],
    "CYCLE_B": [("CHAIN", "CYCLE_A")],
}
# DTP -> (TGT, TGTTLOGO, UPDMODE); DTP_GONE is deliberately absent from RSBKDTP.
_DTPS: dict[str, tuple[str, str, str]] = {
    "DTP_STG": ("STG_DSO", "ODSO", "D"),
    "DTP_EDW": ("EDW_DSO", "ADSO", "F"),
    "DTP_MART": ("MART_ADSO", "ADSO", "F"),
}
# chain -> (run_count, run_days, first, last) and the distinct run days
_RUNS: dict[str, tuple[int, int, str, str]] = {
    "SUB_A": (60, 30, "20260601", "20260730"),  # twice a day -> intraday
    "SUB_B": (10, 10, "20260501", "20260728"),  # weekly-ish
    "META_CHAIN": (30, 30, "20260701", "20260730"),  # daily
    "ONCE_CHAIN": (1, 1, "20240101", "20240101"),  # single run -> low confidence
}
_RUN_DAYS: dict[str, list[str]] = {
    "SUB_A": [f"202607{d:02d}" for d in range(1, 31)],  # consecutive days -> gap 1
    "SUB_B": [f"202607{d:02d}" for d in (1, 8, 15, 22, 29)],  # gap 7 -> weekly
    "META_CHAIN": [f"202607{d:02d}" for d in range(1, 31)],
    "ONCE_CHAIN": ["20240101"],
}
_REFERENCE = "20260730"


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = [str(p).strip() for p in (parameters or [])]
        if "RSPCLOGCHAIN" in sql:
            return self._log_chain(sql, params)
        if "RSPCCHAIN" in sql:
            return self._chain_steps(sql, params)
        if "RSBKDTP" in sql:
            return self._dtp(sql, params)
        return []

    @staticmethod
    def _log_chain(sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        if "MAX(DATUM)" in sql and "CHAIN_ID" not in sql:
            return [(_REFERENCE,)]
        wanted = [p for p in params if p in _RUNS] or list(_RUNS)
        if "COUNT(DISTINCT LOG_ID)" in sql:
            return [(c, _RUNS[c][0], _RUNS[c][1], _RUNS[c][2], _RUNS[c][3]) for c in wanted]
        if "DATUM" in sql:  # distinct (chain, run day)
            return [(c, day) for c in wanted for day in _RUN_DAYS.get(c, [])]
        return []

    @staticmethod
    def _chain_steps(sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        if "DISTINCT CHAIN_ID" in sql:
            if "TYPE = ?" in sql:  # parents calling a sub-chain
                children = set(params[1:])
                return [
                    (chain,)
                    for chain, steps in _STEPS.items()
                    if any(t == "CHAIN" and v in children for t, v in steps)
                ]
            targets = set(params)  # chains containing one of these DTP variants
            return [
                (chain,) for chain, steps in _STEPS.items() if any(v in targets for _, v in steps)
            ]
        chain = params[0] if params else ""
        return [(t, v) for t, v in _STEPS.get(chain, [])]

    @staticmethod
    def _dtp(sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        if "TGT = ?" in sql:  # which DTPs load this provider
            target = params[0]
            return [(d,) for d, v in _DTPS.items() if v[0] == target]
        wanted = [p for p in params if p in _DTPS]
        return [(d, *_DTPS[d]) for d in wanted]


def _capability() -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical,
                present=True,
                schema_name=SCHEMA,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _service() -> LoadClosureService:
    return LoadClosureService(ScriptedConnection(), _capability())


def _chains() -> ChainsRepository:
    return ChainsRepository(ScriptedConnection(), _capability())


# --- cadence ------------------------------------------------------------------------------


def test_cadence_bands_from_median_gap() -> None:
    cadence = _chains().get_cadence()
    assert cadence["SUB_B"].frequency == "weekly"  # 7-day median gap
    assert cadence["SUB_B"].median_gap_days == 1.0 * 7
    assert cadence["META_CHAIN"].frequency == "daily"  # 1-day gap, one run per day


def test_intraday_is_detected() -> None:
    """Two runs per run-day is the precondition for the stale-master-data risk (9.1)."""
    cadence = _chains().get_cadence()
    assert cadence["SUB_A"].intraday is True
    assert cadence["SUB_A"].runs_per_day == 2.0
    assert cadence["SUB_A"].frequency == "multiple_daily"
    assert cadence["META_CHAIN"].intraday is False


def test_single_run_chain_is_low_confidence_not_forced_into_a_band() -> None:
    cadence = _chains().get_cadence()
    once = cadence["ONCE_CHAIN"]
    assert once.confidence == "low"
    assert once.frequency == "unknown"
    assert once.note and "single run" in once.note


def test_liveness_is_cadence_aware_and_reference_date_comes_from_data() -> None:
    cadence = _chains().get_cadence()
    # The reference date is the latest run in the system, never today.
    assert all(c.reference_date is not None for c in cadence.values())
    assert cadence["SUB_A"].reference_date.isoformat() == "2026-07-30"  # type: ignore[union-attr]
    # A daily chain that ran on the reference date is active; a chain last run in 2024 is not.
    assert cadence["META_CHAIN"].active is True
    assert cadence["ONCE_CHAIN"].active is False
    # Windows differ by band rather than one global threshold.
    assert cadence["SUB_B"].liveness_window_days != cadence["SUB_A"].liveness_window_days


# --- closure ------------------------------------------------------------------------------


def test_chain_to_providers_walks_nested_subchains() -> None:
    """Most loads live in sub-chains, so a top-level step list alone would find nothing."""
    closure = _service().chain_to_providers("META_CHAIN")
    assert not isinstance(closure, UnsupportedResult)
    assert closure.direction == "chain_to_providers"
    assert {p.name for p in closure.providers_loaded} == {"STG_DSO", "EDW_DSO", "MART_ADSO"}
    assert set(closure.subchains_walked) == {"SUB_A", "SUB_B"}
    # Each load records which sub-chain reached it and the DTP's update mode.
    by_name = {p.name: p for p in closure.providers_loaded}
    assert by_name["STG_DSO"].via_subchain == "SUB_A"
    assert by_name["STG_DSO"].update_mode == "delta"
    assert by_name["EDW_DSO"].update_mode == "full"


def test_step_categories_are_derived_from_type_codes() -> None:
    closure = _service().chain_to_providers("META_CHAIN")
    assert not isinstance(closure, UnsupportedResult)
    categories = closure.step_categories
    # Counts steps, not resolved providers: 2 in SUB_A + 2 in SUB_B (one of which is a dangling
    # DTP). A step that exists is a step, whether or not its DTP still resolves.
    assert categories.get("data_load") == 4
    assert categories.get("housekeeping") == 1
    assert categories.get("orchestration", 0) >= 2  # the two CHAIN steps plus AND
    assert categories.get("custom_code") == 1


def test_unresolvable_dtp_is_reported_not_silently_dropped() -> None:
    closure = _service().chain_to_providers("META_CHAIN")
    assert not isinstance(closure, UnsupportedResult)
    assert any("could not be resolved" in c for c in closure.caveats)


def test_subchain_cycle_does_not_recurse_forever() -> None:
    closure = _service().chain_to_providers("CYCLE_A")
    assert not isinstance(closure, UnsupportedResult)
    assert closure.subchains_walked == ["CYCLE_B"]  # visited once, then guarded


def test_provider_to_chains_returns_cadence_and_parents() -> None:
    closure = _service().provider_to_chains("STG_DSO")
    assert not isinstance(closure, UnsupportedResult)
    assert closure.direction == "provider_to_chains"
    loading = {c.chain_id for c in closure.loading_chains}
    # The chain holding the DTP plus the parent whose schedule actually governs it.
    assert {"SUB_A", "META_CHAIN"} <= loading
    assert any("parent chains" in c for c in closure.caveats)


def test_provider_with_no_dtp_says_so() -> None:
    closure = _service().provider_to_chains("VIRTUAL_THING")
    assert not isinstance(closure, UnsupportedResult)
    assert closure.loading_chains == []
    assert any("no DTP targets this provider" in c for c in closure.caveats)


def test_governing_cadence_is_the_most_frequent_chain() -> None:
    cadence = _chains().get_cadence()
    governing = cadence_of([cadence["SUB_B"], cadence["SUB_A"]])
    assert governing is not None
    assert governing.chain_id == "SUB_A"  # 2 runs/day beats weekly
    assert cadence_of([]) is None
