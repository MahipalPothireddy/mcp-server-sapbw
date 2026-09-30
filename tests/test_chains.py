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
from mcp_server_sapbw.repositories import chains as chains_module
from mcp_server_sapbw.repositories.chains import (
    _STEP_FAILED_STATES,
    _STEP_OK_STATES,
    _STEP_STATE_LABEL,
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
    # L3 is the failing run, and it is deliberately the SHORTEST (120s). That is the whole shape of
    # D65: ranked by duration this step is last, so a reader chasing the failure was handed the
    # healthy 600s step from L1 instead. The fixture proved the defect before it proved the fix.
    "L3": [
        (
            "LOADING",
            "LOAD_A",
            "1",
            "R",
            _ts("20260722020000.000"),
            _ts("20260722020200.000"),
        ),  # 120s, Ended with errors
        # 'S' = "Skipped at restart". Outside the success set and NOT a failure. D65 proposed
        # calling
        # everything outside ('G','F') a failure; on the reference system that state alone covers
        # 102,895 steps sitting inside 32,405 runs whose own status is green.
        (
            "LOADING",
            "LOAD_SKIPPED",
            "1",
            "S",
            _ts("20260722020200.000"),
            _ts("20260722020201.000"),
        ),
        # 'X' = "Canceled", with NO usable end timestamp. A step killed mid-run often has none, so
        # this asserts that failures are accumulated *before* the timestamp guard rather than after
        # -
        # deriving them afterwards would drop exactly the cases a reader is looking for.
        ("LOADING", "LOAD_KILLED", "1", "X", _ts("20260722020300.000"), None),
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
            # Five different questions reach this one table with five different column shapes, so
            # the markers have to be specific. Ordered most-specific first: a looser order silently
            # feeds one reader another's rows, which is how the D52 fixture broke three tests.
            if "COUNT(DISTINCT DATUM)" in sql:  # cadence: run-day summary (5 columns)
                day_summary: list[tuple[Any, ...]] = []
                for cid, runs in _RUNS.items():
                    dats = [r[1] for r in runs]
                    day_summary.append((cid, len(runs), len(set(dats)), min(dats), max(dats)))
                return day_summary
            if "ANALYZED_STATUS" in sql:  # runtimes: runs for one chain
                cid = params[0]
                return [(r[0], r[1], r[2], r[3]) for r in _RUNS.get(cid, [])]
            if "CHAIN_ID <> ?" in sql:  # contention: every OTHER chain's start times (D64)
                excluded = str(params[0])
                return [
                    (cid, r[1], r[2])
                    for cid, runs in _RUNS.items()
                    if cid != excluded
                    for r in runs
                ]
            if "GROUP BY" in sql and "MIN(DATUM)" in sql:  # schedule matrix summary (4 columns)
                summary: list[tuple[Any, ...]] = []
                for cid, runs in _RUNS.items():
                    dats = [r[1] for r in runs]
                    summary.append((cid, len(runs), min(dats), max(dats)))
                return summary
            if "GROUP BY" in sql:  # cadence: distinct run days (CHAIN_ID, DATUM)
                return sorted(
                    {(cid, r[1]) for cid, runs in _RUNS.items() for r in runs}
                )
            if "MAX(DATUM)" in sql:  # cadence: reference date, one unkeyed aggregate
                every = [r[1] for runs in _RUNS.values() for r in runs]
                return [(max(every),)] if every else [(None,)]
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
    result = repo.list_chains()
    assert not isinstance(result, UnsupportedResult)
    summaries, total = result
    assert total == 4
    by_id = {s.chain_id: s for s in summaries}
    assert by_id["DAILY_LOAD"].description == "Daily finance load"
    assert by_id["MASTER_CHAIN"].active is True
    assert summaries[0].provenance  # provenance present


def test_listing_and_cadence_cannot_disagree_about_the_same_chain() -> None:
    """The two derived cadence separately, and on real data they contradicted each other (D61).

    ``list_chains`` ran a window-bounded summary of its own: a chain whose newest run
    predated the window produced no row, so it was reported ``frequency='unknown'`` with
    ``last_run=None`` while ``get_cadence`` called the same chain ``daily`` and named the
    date. Measured on the reference system, **81 chains reported ``last_run=None`` against
    23,789 recorded runs**, the worst with 2,290 - so the listing was not vague, it
    asserted that chains with years of history had never run.

    Asserted as agreement between the two readers rather than against fixed expected values,
    because the property that matters is that one derivation exists, not today's numbers.
    """
    repo = _repo()
    result = repo.list_chains()
    assert not isinstance(result, UnsupportedResult)
    summaries, _total = result
    cadences = repo.get_cadence([s.chain_id for s in summaries])

    assert cadences, "the fixture should produce cadence records to compare against"
    for summary in summaries:
        cadence = cadences.get(summary.chain_id)
        if cadence is None:
            continue
        assert summary.frequency == cadence.frequency, summary.chain_id
        assert summary.last_run == cadence.last_run, summary.chain_id
        assert summary.runs_per_day == cadence.runs_per_day, summary.chain_id


def test_a_chain_with_run_history_never_reports_a_missing_last_run() -> None:
    """The false statement D61 produced: history on record, ``last_run=None`` in the payload."""
    repo = _repo()
    result = repo.list_chains()
    assert not isinstance(result, UnsupportedResult)
    summaries, _total = result
    cadences = repo.get_cadence([s.chain_id for s in summaries])

    for summary in summaries:
        cadence = cadences.get(summary.chain_id)
        if cadence is not None and cadence.run_count:
            assert summary.last_run is not None, (
                f"{summary.chain_id} has {cadence.run_count} recorded run(s) but the listing "
                "reports no last run, which reads as 'never ran'"
            )


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


# --- cadence must not depend on how many chains you ask about (D62) ---------------------------


def test_cadence_is_the_same_asked_alone_or_in_a_batch() -> None:
    """The property the row cap broke, and the reason it went unnoticed for so long.

    ``_distinct_run_days`` pulled every ``(chain, day)`` pair in one capped statement
    ordered by chain name. On the reference system that is **411,581 pairs against a
    200,000 cap**, so every chain sorting after the 200,000th row came back with no run
    days, no gaps, no median gap - and ``_classify_cadence`` turns a missing median into
    ``"unknown"``. Measured on the S03 subject: asked alone it returned ``daily`` with a
    median gap of 1.0; asked as one of 280 it returned ``unknown`` from identical data.

    It went unnoticed because every existing caller asked about a handful of chains. Only wiring the
    listing to the shared derivation (D61) asked about all of them at once.

    Asserted as invariance rather than against expected values: whatever the cadence is,
    asking about more chains must not change it.
    """
    repo = _repo()
    listed = repo.list_chains()
    assert not isinstance(listed, UnsupportedResult)
    summaries, _total = listed
    names = [s.chain_id for s in summaries]

    together = repo.get_cadence(names)
    for chain_id in names:
        alone = repo.get_cadence([chain_id]).get(chain_id)
        batched = together.get(chain_id)
        if alone is None or batched is None:
            continue
        assert alone.frequency == batched.frequency, chain_id
        assert alone.median_gap_days == batched.median_gap_days, chain_id
        assert alone.run_days == batched.run_days, chain_id


def test_the_run_day_read_is_chunked_rather_than_one_capped_statement() -> None:
    """Chunking is the fix, so it is asserted by counting reads rather than by trusting the code.

    A first version only checked that nothing was truncated, which the fixture guarantees
    anyway - it asserted the absence of a symptom instead of the presence of the fix.
    """
    repo = _repo()
    connection = ScriptedConnection()
    reads: list[str] = []
    inner = connection.execute_select

    def counting(sql: str, parameters: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        if "RSPCLOGCHAIN" in sql and "GROUP BY" in sql and "COUNT" not in sql:
            reads.append(sql)
        return inner(sql, parameters)

    connection.execute_select = counting  # type: ignore[method-assign]
    repo = ChainsRepository(connection, _capability())

    chunk = chains_module._RUN_DAY_CHUNK
    many = [f"CHAIN_{index:03d}" for index in range(chunk * 2 + 3)]
    days, truncated = repo._distinct_run_days(many)

    expected = -(-len(many) // chunk)  # ceiling division
    assert len(reads) == expected, (
        f"{len(many)} chains at a chunk size of {chunk} should take {expected} read(s); "
        f"took {len(reads)}"
    )
    assert truncated == set(), "the fixture is far below the cap, so nothing should be truncated"
    assert isinstance(days, dict)


def test_a_truncated_run_day_read_is_reported_not_silently_classified() -> None:
    """The half of D62 that mattered most: the cap changed a classification and left no trace.

    Forced with a cap of 1 so the read truncates deterministically, because the offline
    fixture is nowhere near 200,000 rows and the honest way to test the path is to make it
    happen rather than to assume it works.
    """
    repo = _repo()
    original_cap = chains_module._MAX_RUN_DAY_ROWS
    try:
        chains_module._MAX_RUN_DAY_ROWS = 1
        cadences = repo.get_cadence(["DAILY_LOAD"])
    finally:
        chains_module._MAX_RUN_DAY_ROWS = original_cap

    entry = cadences.get("DAILY_LOAD")
    assert entry is not None
    assert entry.confidence == "low", "a cadence from a truncated read is not a measurement"
    assert entry.note is not None
    assert "cap" in entry.note and "lower bound" in entry.note


# --- a percentile over a handful of runs is labelled (D63) -------------------------------------


def test_a_p95_over_too_few_runs_is_labelled_rather_than_presented_as_planning_data() -> None:
    """REQ-03 clause 2, which S03 could not test because its subject had 88 clean runs.

    Measured on production against a chain with **three** runs in ninety days: the payload returned
    ``p95=6125.4`` beside ``max=6252.3`` with no indication that three samples cannot support a 95th
    percentile. At that count the p95 interpolates between the second and third values, so it is the
    maximum by another name, and a reader planning a schedule against "p95" is entitled to assume
    the word means something.

    The test records its own history because that is the useful part: the first automated check
    **passed** this clause, because it looked for any caveat mentioning "run" and matched the
    unrelated "longest-running steps". A sloppy matcher is how a gap gets certified as covered.
    """
    repo = _repo()
    result = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(result, UnsupportedResult)

    assert result.duration_seconds.count < chains_module._MIN_RUNS_FOR_PERCENTILE, (
        "the fixture must have few runs for this test to mean anything"
    )
    if result.duration_seconds.p95_s is not None:
        labelled = [c for c in result.caveats if "p95 is computed over only" in c]
        assert labelled, (
            "a p95 below the meaningful-run threshold must say so; caveats were: "
            f"{result.caveats}"
        )
        assert "not meaningfully distinct" in labelled[0]


def test_a_p95_over_enough_runs_carries_no_such_label() -> None:
    """The counterpart, so the label cannot become boilerplate attached to every answer."""
    repo = _repo()
    result = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(result, UnsupportedResult)

    original = chains_module._MIN_RUNS_FOR_PERCENTILE
    try:
        # Lower the threshold below the fixture's run count rather than inventing hundreds of runs:
        # the property under test is the comparison, not the data volume.
        chains_module._MIN_RUNS_FOR_PERCENTILE = 1
        plentiful = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    finally:
        chains_module._MIN_RUNS_FOR_PERCENTILE = original

    assert not isinstance(plentiful, UnsupportedResult)
    assert not [c for c in plentiful.caveats if "p95 is computed over only" in c]


# --- failed steps (D65) -------------------------------------------------------------------------
#
# The defect: a chain analysis never stated which step FAILED. `bottleneck_steps` ranks by duration,
# so a reader chasing a failure was handed the slowest step - a different step entirely on a chain
# that fails fast. It stayed hidden on the validation subject only because its failing loads hung
# for
# ~23.5 hours before dying, so the slowest steps happened to be the red ones.
#
# The fix's substance is not "surface STATE" but the CLASSIFICATION. D65 proposed treating
# everything
# outside ('G','F') as failed. Measured against the RSPC_STATE domain that is wrong: 'S' (Skipped at
# restart) and 'A' (Active) are outside the success set and are not failures, and on the reference
# system they account for 103,347 steps inside 32,405 runs whose own status is green.


def test_a_failing_step_is_reported_as_failed() -> None:
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    by_variant = {s.variant: s for s in rt.failed_steps}
    assert "LOAD_A" in by_variant
    failed = by_variant["LOAD_A"]
    assert failed.state == "R"
    assert failed.state_label == "Ended with errors"
    assert failed.occurrences == 1
    assert failed.process_type == "LOADING"
    assert failed.example_log_id == "L3"


def test_the_failed_list_is_not_the_duration_ranking_re_sorted() -> None:
    """The property the whole fix exists for, asserted rather than assumed.

    The fixture's failing step is its *shortest*, so if these two lists ever agree here the
    classification has collapsed back into a duration ranking. Verified on production too: on 2 of
    the 7 chains carrying both lists the most-failed step is not the slowest step.
    """
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    slowest = rt.bottleneck_steps[0]
    worst = rt.failed_steps[0]
    assert slowest.duration_s > (worst.longest_s or 0.0)
    assert slowest.state == "G", "the slowest step in this fixture succeeded"
    assert worst.state == "R"


def test_a_skipped_step_is_counted_but_never_called_a_failure() -> None:
    """'Skipped at restart' is a normal restart artefact, not a failure.

    Calling it one is the over-count D65's two-class rule would have introduced.
    """
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    assert "LOAD_SKIPPED" not in {s.variant for s in rt.failed_steps}
    assert rt.indeterminate_steps >= 1
    assert any("neither success nor failure" in c for c in rt.caveats)


def test_a_failure_with_no_end_timestamp_is_still_reported() -> None:
    """Accumulated before the timestamp guard, because a killed step often has no end time.

    Deriving failures after the guard would silently drop them - and a step that died mid-run is
    precisely the case a reader is chasing. On the reference system every 'Active' step and all 258
    'Undefined' steps have no end timestamp.
    """
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    killed = {s.variant: s for s in rt.failed_steps}.get("LOAD_KILLED")
    assert killed is not None, "a cancelled step with no end timestamp must still be reported"
    assert killed.state == "X"
    assert killed.state_label == "Canceled"
    # No duration is claimed for it, rather than a fabricated zero.
    assert killed.longest_s is None
    assert killed.shortest_s is None
    # And it is absent from the duration ranking, which can only rank what it can time.
    assert "LOAD_KILLED" not in {s.variant for s in rt.bottleneck_steps}


def test_failed_step_provenance_names_the_state_it_was_read_from() -> None:
    """A failure is an accusation about a step, so the citation carries its row."""
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    for step in rt.failed_steps:
        assert step.provenance.source_table == "RSPCPROCESSLOG"
        key = step.provenance.source_key or {}
        assert "LOG_ID" in key
        assert key.get("STATE") == step.state


def test_every_step_carries_its_decoded_state() -> None:
    """Decoded once in the repository, so no consumer needs to know what 'R' means."""
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    for step in rt.bottleneck_steps:
        assert step.state_label, step.variant
        assert step.failed is (step.state in ("R", "J", "X"))


def test_steps_examined_distinguishes_no_failures_from_nothing_read() -> None:
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    # L1 has 2 steps, L2 has 1, L3 has 3 = 6. Every row is examined, including the cancelled step
    # with no end timestamp - which is the difference between this count and the 5 that can be
    # timed.
    assert rt.steps_examined == 6
    assert len(rt.bottleneck_steps) == 5, "one step cannot be timed, so it cannot be ranked"
    empty = repo.get_chain_runtimes("UNKNOWN_CHAIN", days=90)
    assert not isinstance(empty, UnsupportedResult)
    assert empty.steps_examined == 0
    assert empty.failed_steps == []


def test_the_step_state_decode_matches_the_documented_domain() -> None:
    """Dictionary-backed, not empirical: DD03L types this column as RSPC_STATE and DD07T has texts.

    Pinned against the eleven values read from the reference system's own dictionary, and the three
    classes asserted to be disjoint - an overlap would make a step both a success and a failure.
    """
    assert set(_STEP_STATE_LABEL) == {"", "A", "F", "G", "J", "P", "Q", "R", "S", "X", "Y"}
    assert {"G", "F"} == _STEP_OK_STATES
    assert {"R", "J", "X"} == _STEP_FAILED_STATES
    assert not (_STEP_OK_STATES & _STEP_FAILED_STATES)
    # Every classified state must have a label, or a payload reports a class with no explanation.
    for state in _STEP_OK_STATES | _STEP_FAILED_STATES:
        assert _STEP_STATE_LABEL[state]


def test_the_caveat_no_longer_calls_the_step_decode_empirical() -> None:
    """It is read from the ABAP dictionary. Claiming otherwise understates the answer's strength."""
    repo = _repo()
    rt = repo.get_chain_runtimes("DAILY_LOAD", days=90)
    assert not isinstance(rt, UnsupportedResult)
    joined = " ".join(rt.caveats)
    assert "RSPC_STATE" in joined
    assert "step STATE / ANALYZED_STATUS decode is empirical" not in joined
