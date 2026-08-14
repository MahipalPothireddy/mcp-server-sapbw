"""Tests for the BW 3.x dataflow layer (scenario 5), offline against a scripted landscape.

Synthetic names only. The behaviour worth pinning down is that a DataSource with no 7.x
transformation is flagged as such — that is the difference between a leftover and the live load
logic — and that ``OBJVERS = 'A'`` is stated explicitly, since the dialect does not inject it for
these table families.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.provenance import UnsupportedResult
from mcp_server_sapbw.repositories.threex import ThreeXRepository

SCHEMA = "TESTSCHEMA"
_TABLES = {
    "infosource_map": "RSISOSMAP",
    "transfer_structure": "RSTS",
    "transfer_rule": "RSTSRULES",
    "update_rule": "RSUPDINFO",
    "update_rule_routine": "RSUPDROUT",
    "transformation": "RSTRAN",
}

# DS_LEGACY routes 3.x only. DS_BOTH has a 3.x route AND a 7.x transformation. DS_SHELL's transfer
# structure carries no rules at all, so it is a PSA shell rather than a dataflow.
_MAP_ROWS = [
    ("DS_LEGACY", "SRC100", "IS_LEGACY", "TS_LEGACY", "D"),
    ("DS_BOTH", "SRC100", "IS_BOTH", "TS_BOTH", "D"),
    ("DS_SHELL", "SRC100", "IS_SHELL", "TS_SHELL", "D"),
]
# TRANSTRU, COUNT, with-routine, with-formula, with-constant
_RULE_PROFILE = [
    ("TS_LEGACY", 12, 3, 1, 2),
    ("TS_BOTH", 5, 0, 0, 0),
]
_RULE_ROWS = [
    # TRANSTRU, COMSTRU, IOBJNM, IOBJNM_TS, FIXED_VALUE, CONVROUT_G, CONVROUT_L, FORMULA_ID, CONV
    ("TS_LEGACY", "CS_LEGACY", "0MATERIAL", "MATNR", "", "", "", "", ""),
    ("TS_LEGACY", "CS_LEGACY", "0PLANT", "", "1000", "", "", "", ""),
    ("TS_LEGACY", "CS_LEGACY", "0CURRENCY", "WAERS", "", "", "CONV_CURRENCY", "", ""),
    ("TS_LEGACY", "CS_LEGACY", "0AMOUNT", "", "", "", "", "FORM_01", ""),
]


class ScriptedConnection:
    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        # Every query against these families must state the active-version filter itself, because
        # the dialect only injects it for RSD/RSO/RSZ/RSTRAN. Assert that rather than trust it.
        if "RSISOSMAP" in sql or "RSTSRULES" in sql or "RSUPDINFO" in sql or "RSUPDROUT" in sql:
            assert "OBJVERS = 'A'" in sql, f"missing active-version filter: {sql}"

        if "RSISOSMAP" in sql:
            return list(_MAP_ROWS)
        if "RSTSRULES" in sql:
            if "GROUP BY" in sql:
                return list(_RULE_PROFILE)
            wanted = str(parameters[0]) if parameters else ""
            return [r for r in _RULE_ROWS if r[0] == wanted]
        if "RSTS" in sql:  # transfer structures with a start routine
            return [("TS_LEGACY",)]
        if "RSUPDROUT" in sql:
            return [("UPD_1", 2)]
        if "RSUPDINFO" in sql:
            if "TOTAL_COUNT" in sql or "COUNT(" in sql:
                return [(1,)]
            if "ISOURCE" in sql and "INFOCUBE" in sql and "UPDID" not in sql:
                return [("IS_LEGACY", "0MATERIAL")]
            return [("UPD_1", "IS_LEGACY", "0MATERIAL", "X", "", "ACT")]
        if "RSTRAN" in sql:
            return [("DS_BOTH",)]
        return []


def _repo(*, omit: set[str] | None = None) -> ThreeXRepository:
    omit = omit or set()
    tables = {
        logical: TableStatus(
            logical_name=logical,
            resolved_name=physical,
            present=logical not in omit,
            schema_name=SCHEMA,
        )
        for logical, physical in _TABLES.items()
    }
    capability = CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        tables=tables,
    )
    return ThreeXRepository(ScriptedConnection(), capability)


def test_flow_without_a_7x_transformation_is_flagged() -> None:
    """The whole point of the scenario: no 7.x path means the transfer rules are the live logic."""
    report = _repo().list_flows()
    assert not isinstance(report, UnsupportedResult)
    by_name = {f.datasource: f for f in report.flows}
    assert by_name["DS_LEGACY"].has_seven_x_transformation is False
    assert by_name["DS_BOTH"].has_seven_x_transformation is True


def test_rule_less_transfer_structure_is_excluded() -> None:
    """A transfer structure with no rules is a PSA shell, not a dataflow."""
    report = _repo().list_flows()
    assert not isinstance(report, UnsupportedResult)
    assert "DS_SHELL" not in {f.datasource for f in report.flows}


def test_rule_profile_and_start_routine_are_carried() -> None:
    report = _repo().list_flows()
    assert not isinstance(report, UnsupportedResult)
    legacy = next(f for f in report.flows if f.datasource == "DS_LEGACY")
    assert legacy.rule_count == 12
    assert legacy.rules_with_routine == 3
    assert legacy.rules_with_formula == 1
    assert legacy.rules_with_constant == 2
    assert legacy.has_start_routine is True
    assert legacy.update_rule_targets == ["0MATERIAL"]


def test_only_without_transformation_filters() -> None:
    report = _repo().list_flows(only_without_transformation=True)
    assert not isinstance(report, UnsupportedResult)
    assert {f.datasource for f in report.flows} == {"DS_LEGACY"}
    assert report.datasources_3x_only == 1


def test_transfer_rule_mechanism_is_classified_not_guessed() -> None:
    rules = _repo().get_transfer_rules("TS_LEGACY")
    assert not isinstance(rules, UnsupportedResult)
    mechanisms = {r.infoobject: r.mechanism for r in rules}
    assert mechanisms["0MATERIAL"] == "direct_assignment"
    assert mechanisms["0PLANT"] == "constant"
    assert mechanisms["0CURRENCY"] == "routine"
    assert mechanisms["0AMOUNT"] == "formula"


def test_update_rules_report_routine_count_and_start_routine() -> None:
    rules = _repo().list_update_rules()
    assert not isinstance(rules, UnsupportedResult)
    assert len(rules) == 1
    assert rules[0].infosource == "IS_LEGACY"
    assert rules[0].target == "0MATERIAL"
    assert rules[0].has_start_routine is True
    assert rules[0].routine_count == 2


def test_missing_tables_degrade_to_unsupported_rather_than_empty() -> None:
    """An absent table on some other release must say so, not report 'no 3.x flows'."""
    assert isinstance(_repo(omit={"transfer_rule"}).list_flows(), UnsupportedResult)
    assert isinstance(_repo(omit={"update_rule"}).list_update_rules(), UnsupportedResult)


def test_update_rule_targets_unresolved_is_caveated() -> None:
    report = _repo(omit={"update_rule"}).list_flows()
    assert not isinstance(report, UnsupportedResult)
    assert any("RSUPDINFO is unavailable" in c for c in report.caveats)
