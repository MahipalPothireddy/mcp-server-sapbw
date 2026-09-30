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
_TABLES = {
    "chain_edges": "RSPCCHAIN",
    "dtp": "RSBKDTP",
    "log_chain": "RSPCLOGCHAIN",
    # Read only when no active DTP loads a provider, to tell "never had a loader" apart from "had
    # one, frozen years ago" (D33).
    "transformation": "RSTRAN",
}

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

# Inbound transformations by target: (TRANID, OBJVERS, OBJSTAT, SOURCENAME, SOURCETYPE). Shaped
# after
# the three measured production cases, which each hold one RSTRAN row at OBJVERS 'R' with OBJSTAT
# 'ACT' and no DTP row at any version, against 6,483 targets whose only inbound transformation is
# unactivated delivered content. Both shapes are here because the whole design of the D33 probe is
# the line between them.
_TRANSFORMATIONS: dict[str, list[tuple[str, str, str, str, str]]] = {
    # The real case: a loader that used to run, against a provider still holding rows.
    "FROZEN_CUBE": [("TRAN_FROZEN", "R", "ACT", "RETIRED_DSO", "ODSO")],
    # BW-delivered content nobody activated. True, and useless to report.
    "SHELF_CUBE": [("TRAN_DELIVERED", "D", "INA", "SHELF_SRC", "ODSO")],
    # An active transformation with no DTP: loaded by something other than a DTP, not frozen.
    "ROUTINE_FED_DSO": [("TRAN_LIVE", "A", "ACT", "STG_DSO", "ODSO")],
}


class ScriptedConnection:
    def __init__(self) -> None:
        #: Recorded so "this read did not happen" can be asserted rather than assumed.
        self.statements: list[str] = []

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        params = [str(p).strip() for p in (parameters or [])]
        if "RSPCLOGCHAIN" in sql:
            return self._log_chain(sql, params)
        if "RSPCCHAIN" in sql:
            return self._chain_steps(sql, params)
        if "RSBKDTP" in sql:
            return self._dtp(sql, params)
        if "RSTRAN" in sql:
            return self._transformation(sql, params)
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

    @staticmethod
    def _transformation(sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        """Applies the conditions the statement actually carries, like a database would.

        Returning every row for the target regardless of the ``WHERE`` would make the narrowing
        untestable: the ``OBJSTAT`` condition could be deleted and these tests would stay green
        while
        the real system went from 576 targets to 6,483.

        The active-version assertion is the more important half. Mission Rule 6 has the dialect
        inject ``OBJVERS = 'A'`` on every ``RSTRAN`` read, and this one opts out by naming
        ``OBJVERS`` in its own ``WHERE``. If that opt-out ever stops working the live statement
        contradicts itself and returns nothing, while a fixture that ignored the clause would keep
        answering - a false pass on exactly the mechanism the feature depends on.
        """
        assert "OBJVERS = 'A'" not in sql, (
            "the dialect injected the active-version filter into the non-active loader read, so on "
            f"a real system it would return nothing: {sql}"
        )
        rows: list[tuple[str, ...]] = list(_TRANSFORMATIONS.get(params[0] if params else "", []))
        if "OBJVERS <> 'A'" in sql:
            rows = [r for r in rows if r[1] != "A"]
        if "OBJSTAT = 'ACT'" in sql:
            rows = [r for r in rows if r[2] == "ACT"]
        return list(rows)


def _capability(present: set[str] | None = None) -> CapabilityRecord:
    """``present`` narrows the record, so a release without one table can be exercised."""
    names = present if present is not None else set(_TABLES)
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables={
            logical: TableStatus(
                logical_name=logical,
                resolved_name=physical if logical in names else None,
                present=logical in names,
                schema_name=SCHEMA if logical in names else None,
            )
            for logical, physical in _TABLES.items()
        },
    )


def _service(
    connection: ScriptedConnection | None = None, present: set[str] | None = None
) -> LoadClosureService:
    return LoadClosureService(connection or ScriptedConnection(), _capability(present))


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


# --- a provider whose only loader is frozen (D33) ----------------------------------------------
#
# "No DTP targets this provider, so it may be virtual, routine-filled, or InfoPackage-loaded" is a
# true sentence and, on a populated provider, a misleading one. Measured on the reference system:
# three InfoCubes holding 1,081,275 rows between them, 2,442-3,191 days old, each with exactly one
# inbound transformation at OBJVERS 'R' / OBJSTAT 'ACT' and no DTP row at any version. Told the
# plain
# sentence, a reader's next step is to delete them; the rows are retained sales history from a
# decommissioned source.


def test_a_frozen_loader_is_named_rather_than_left_as_three_guesses() -> None:
    closure = _service().provider_to_chains("FROZEN_CUBE")
    assert not isinstance(closure, UnsupportedResult)

    assert closure.loading_chains == []  # Rule 6 still governs the answer above
    assert [loader.tran_id for loader in closure.inactive_loaders] == ["TRAN_FROZEN"]
    frozen = closure.inactive_loaders[0]
    assert (frozen.objvers, frozen.objstat) == ("R", "ACT")
    assert frozen.source_name == "RETIRED_DSO"

    detail = " ".join(closure.caveats)
    assert "non-active version (R)" in detail
    assert "RETIRED_DSO" in detail, "the retired source is the first thing to check"
    assert "not unloaded by design" in detail
    # The plain sentence must survive alongside it: the three innocent explanations are still live
    # possibilities, and replacing them would overstate what a frozen row proves.
    assert any("it may be loaded by an InfoPackage" in c for c in closure.caveats)


def test_unactivated_delivered_content_is_not_reported_as_a_frozen_loader() -> None:
    """The narrowing, which is the whole design.

    "Any non-active inbound transformation" is 6,483 targets on the reference system, nearly all
    OBJVERS 'D' with OBJSTAT 'INA' - content shipped by SAP that nobody activated. Requiring
    OBJSTAT 'ACT' cuts it to 576, of which ten hold rows. A finding that fires 6,483 times is not a
    finding.
    """
    closure = _service().provider_to_chains("SHELF_CUBE")
    assert not isinstance(closure, UnsupportedResult)

    assert closure.inactive_loaders == []
    assert closure.caveats == [c for c in closure.caveats if "non-active version" not in c]
    assert any("no DTP targets this provider" in c for c in closure.caveats)


def test_an_active_transformation_with_no_dtp_is_not_a_frozen_loader() -> None:
    """A live transformation loaded by something other than a DTP is not evidence of a stoppage.

    Guards the ``OBJVERS <> 'A'`` half of the filter specifically: without it this provider would be
    reported as "no longer loaded, but it was" while its loader is the current active version.
    """
    closure = _service().provider_to_chains("ROUTINE_FED_DSO")
    assert not isinstance(closure, UnsupportedResult)

    assert closure.inactive_loaders == []
    assert not any("non-active version" in c for c in closure.caveats)


def test_the_frozen_loader_cites_the_transformation_row_it_was_read_from() -> None:
    """Mission Rule 3. The claim is about a specific row, so it has to name one."""
    closure = _service().provider_to_chains("FROZEN_CUBE")
    assert not isinstance(closure, UnsupportedResult)

    provenance = closure.inactive_loaders[0].provenance
    assert provenance is not None
    assert provenance.source_table == "RSTRAN"
    assert provenance.source_key == {
        "TRANID": "TRAN_FROZEN",
        "OBJVERS": "R",
        "TARGETNAME": "FROZEN_CUBE",
    }


def test_a_release_without_rstran_still_answers_rather_than_failing() -> None:
    """The probe is an addition to the answer, so its absence must cost only itself."""
    closure = _service(present=set(_TABLES) - {"transformation"}).provider_to_chains("FROZEN_CUBE")
    assert not isinstance(closure, UnsupportedResult)

    assert closure.inactive_loaders == []
    assert any("no DTP targets this provider" in c for c in closure.caveats)


def test_a_provider_with_a_working_loader_never_pays_for_the_frozen_probe() -> None:
    """Reached only when the active read came back empty, so a healthy provider costs nothing."""
    conn = ScriptedConnection()
    closure = _service(conn).provider_to_chains("STG_DSO")
    assert not isinstance(closure, UnsupportedResult)

    assert closure.loading_chains, "the fixture must resolve a loader for this to mean anything"
    assert not any("RSTRAN" in sql for sql in conn.statements)
