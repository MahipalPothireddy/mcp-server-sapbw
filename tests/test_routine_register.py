"""Tests for the portfolio-wide routine register.

Offline against a scripted landscape of four routines of different sizes. Synthetic names only.

The register's contract is that size is complete but analysis is budgeted, so most of these tests
are about that boundary: an unparsed routine must not look clean.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.services.routine_register import RoutineRegisterService

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "transformation": "RSTRAN",
    "routine_source": "RSAABAP",
    "transformation_step_rout": "RSTRANSTEPROUT",
}

# TRANID, SOURCENAME, TARGETNAME, STARTROUTINE, ENDROUTINE, EXPERT, GLBCODE, GLBCODE2
_TRANSFORMATIONS = [
    ("TR_BIG", "SRC_A", "DSO_A", "CODE_BIG", "CODE_SMALL", "", "", ""),
    ("TR_MID", "SRC_B", "DSO_B", "CODE_MID", "", "", "", ""),
    # Declares a routine with no source rows: an orphaned reference, not an empty routine.
    ("TR_GHOST", "SRC_C", "DSO_C", "CODE_GHOST", "", "", "", ""),
]
# TRANID, CODEID, KIND
_FIELD_ROUTINES = [("TR_MID", "CODE_FIELD", "NORMAL")]

# CODEID -> source. CODE_BIG is the largest and has a SELECT inside a LOOP; CODE_MID is clean.
_SOURCE: dict[str, list[str]] = {
    "CODE_BIG": [
        "LOOP AT source_package INTO ls_row.",
        "  SELECT single f FROM tbl_lookup INTO lv WHERE k = ls_row-k.",
        "ENDLOOP.",
        "* padding to make this the largest routine",
        "* padding",
        "* padding",
    ],
    "CODE_MID": [
        "SELECT f FROM tbl_bulk INTO TABLE lt.",
        "* padding",
        "* padding",
    ],
    "CODE_SMALL": ["* nothing much here"],
    "CODE_FIELD": ["result = source_fields-amount * 2."],
}


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        if "RSTRANSTEPROUT" in sql:
            return list(_FIELD_ROUTINES)
        if "RSAABAP" in sql:
            if "COUNT(*)" in sql:  # size aggregate: CODEID, line count
                return [(code, len(lines)) for code, lines in _SOURCE.items()]
            # source fetch for the parse budget: CODEID, LINE for the requested code ids
            wanted = [str(p) for p in params]
            return [(code, line) for code in wanted if code in _SOURCE for line in _SOURCE[code]]
        if "RSTRAN" in sql:
            return list(_TRANSFORMATIONS)
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


def _register(**kwargs: Any) -> Any:
    service = RoutineRegisterService(ScriptedConnection(), _capability())
    return service.build(**kwargs)


def _entry(register: Any, code_id: str) -> Any:
    return next(e for e in register.entries if e.code_id == code_id)


# --- portfolio completeness ------------------------------------------------------------------


def test_every_routine_with_source_is_listed() -> None:
    register = _register()
    assert not isinstance(register, UnsupportedResult)
    assert {e.code_id for e in register.entries} == set(_SOURCE)
    assert register.total_routines == 4


def test_totals_cover_the_whole_portfolio() -> None:
    register = _register()
    assert register.total_lines == sum(len(lines) for lines in _SOURCE.values())


def test_orphaned_routine_reference_is_excluded_and_reported() -> None:
    """A CODEID on a transformation with no source rows is a leftover, not an empty routine."""
    register = _register()
    assert "CODE_GHOST" not in {e.code_id for e in register.entries}
    assert any("no source rows" in c for c in register.caveats)


def test_field_routines_are_included_with_their_slot() -> None:
    assert _entry(_register(), "CODE_FIELD").kind == "field"


def test_header_routines_carry_their_slot_and_transformation() -> None:
    register = _register()
    assert _entry(register, "CODE_BIG").kind == "start"
    assert _entry(register, "CODE_SMALL").kind == "end"
    assert _entry(register, "CODE_BIG").transformation_id == "TR_BIG"
    assert _entry(register, "CODE_BIG").target_name == "DSO_A"


# --- the budget boundary ---------------------------------------------------------------------


def test_analysis_is_limited_to_the_budget_largest_by_size() -> None:
    register = _register(parse_budget=1)
    assert register.analyzed_count == 1
    # CODE_BIG has the most lines, so it is the one worth the budget.
    assert _entry(register, "CODE_BIG").analyzed is True
    assert _entry(register, "CODE_MID").analyzed is False


def test_unparsed_routine_reports_no_pattern_counts_at_all() -> None:
    """Zeroes would read as 'clean'; an unparsed routine must be visibly unknown instead."""
    entry = _entry(_register(parse_budget=1), "CODE_MID")
    assert entry.analyzed is False
    assert entry.anti_pattern_counts == {}
    assert entry.anti_pattern_total == 0
    assert entry.table_reads == []
    # Its size is still known.
    assert entry.line_count == 3


def test_budget_shortfall_is_declared() -> None:
    register = _register(parse_budget=1)
    assert any("listed but not parsed" in c for c in register.caveats)
    assert any("analyzed=false has" in c for c in register.caveats)


def test_budget_is_clamped() -> None:
    assert _register(parse_budget=10_000).parse_budget <= 500
    assert _register(parse_budget=0).parse_budget == 1


# --- ranking ---------------------------------------------------------------------------------


def test_ranked_by_pattern_count_then_size() -> None:
    register = _register()
    assert register.entries[0].code_id == "CODE_BIG"  # the only one with an anti-pattern
    assert register.entries[0].anti_pattern_total >= 1


def test_select_in_loop_is_counted_and_attributed() -> None:
    entry = _entry(_register(), "CODE_BIG")
    assert entry.anti_pattern_counts.get("select_in_loop") == 1
    assert entry.table_reads == ["tbl_lookup"]
    assert entry.max_loop_nesting == 1


def test_clean_routine_has_no_patterns_but_is_analyzed() -> None:
    entry = _entry(_register(), "CODE_MID")
    assert entry.analyzed is True
    assert entry.anti_pattern_total == 0
    assert entry.select_count == 1


def test_portfolio_pattern_tally_is_aggregated() -> None:
    assert _register().anti_pattern_totals.get("select_in_loop") == 1


# --- paging and gating -----------------------------------------------------------------------


def test_paging_reports_truncation() -> None:
    first = _register(limit=2, offset=0)
    assert len(first.entries) == 2
    assert first.truncated is True
    assert first.total_routines == 4
    second = _register(limit=2, offset=2)
    assert second.truncated is False
    assert {e.code_id for e in first.entries}.isdisjoint({e.code_id for e in second.entries})


def test_missing_source_table_is_unsupported_not_empty() -> None:
    service = RoutineRegisterService(ScriptedConnection(), _capability(present={"transformation"}))
    result = service.build()
    assert isinstance(result, UnsupportedResult)
    assert "RSAABAP" in str(result.missing) or "routine_source" in str(result.missing)


def test_field_routines_skipped_when_table_absent() -> None:
    service = RoutineRegisterService(
        ScriptedConnection(), _capability(present={"transformation", "routine_source"})
    )
    register = service.build()
    assert not isinstance(register, UnsupportedResult)
    assert "CODE_FIELD" not in {e.code_id for e in register.entries}


def test_lower_bound_caveat_is_always_present() -> None:
    assert any("lower bound" in c for c in _register().caveats)
