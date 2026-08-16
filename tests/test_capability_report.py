"""The per-system support profile: contract state crossed with what discovery found.

The verdict is the point of this service. "No answer" has two very different causes - the release
does not carry the object, or the reader is not written yet - and they are indistinguishable from
the outside. Only one of them is a gap in this server, so the tests below pin each combination.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mcp_server_sapbw.core.contract import contract
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.services.capability_report import build_report


def _pick(state: str) -> str:
    """A capability actually carrying the given contract state, so the test tracks reality."""
    names = sorted(n for n, e in contract().items() if e.state == state)
    assert names, f"no capability is in state {state}; the fixture assumption is stale"
    return names[0]


def _record(tables: dict[str, TableStatus]) -> CapabilityRecord:
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema="TESTSCHEMA",
        discovered_at=datetime.now(UTC),
        tables=tables,
    )


def _present(logical: str, *, present: bool) -> TableStatus:
    return TableStatus(
        logical_name=logical,
        resolved_name="SOME_TABLE" if present else None,
        present=present,
        schema_name="TESTSCHEMA",
    )


def _pick_two(state: str) -> tuple[str, str]:
    names = sorted(n for n, e in contract().items() if e.state == state)
    assert len(names) >= 2, f"fewer than two capabilities in state {state}; assumption is stale"
    return names[0], names[1]


def test_all_four_verdicts_are_produced() -> None:
    usable, absent = _pick_two("SUPPORTED")
    unread_present, unread_absent = _pick_two("PLANNED")
    report = build_report(
        _record(
            {
                usable: _present(usable, present=True),
                absent: _present(absent, present=False),
                unread_present: _present(unread_present, present=True),
                unread_absent: _present(unread_absent, present=False),
            }
        )
    )
    verdicts = {row.capability: row.verdict for row in report.capabilities}

    assert verdicts[usable] == "usable"
    assert verdicts[absent] == "absent_on_system"
    assert verdicts[unread_present] == "not_implemented"
    assert verdicts[unread_absent] == "not_applicable"


def test_unknown_presence_reports_the_implementation_question_only() -> None:
    """Presence unknown is not presence absent. The verdict falls back to what the server does."""
    supported, planned = _pick("SUPPORTED"), _pick("PLANNED")
    report = build_report(_record({}))
    verdicts = {row.capability: row.verdict for row in report.capabilities}
    assert verdicts[supported] == "usable"
    assert verdicts[planned] == "not_implemented"
    assert "not_applicable" not in set(verdicts.values()), (
        "not_applicable claims the object is absent, which an unprobed capability cannot support"
    )


def test_totals_and_by_state_account_for_every_row() -> None:
    supported = _pick("SUPPORTED")
    report = build_report(_record({supported: _present(supported, present=True)}))
    assert sum(report.totals.values()) == len(report.capabilities)
    assert sum(report.by_state.values()) == len(report.capabilities)
    assert report.by_state == {
        state: sum(1 for e in contract().values() if e.state == state)
        for state in {e.state for e in contract().values()}
    }


def test_unprobed_presence_is_reported_as_unknown_not_guessed() -> None:
    """A capability discovery never probed must not be reported as present or absent."""
    report = build_report(_record({}))
    assert all(row.present is None for row in report.capabilities)
    assert any("not probed by discovery" in c for c in report.caveats)


def test_absent_capabilities_are_named_in_a_caveat() -> None:
    absent = _pick("SUPPORTED")
    report = build_report(_record({absent: _present(absent, present=False)}))
    assert any("implemented here but absent on this system" in c for c in report.caveats)
    assert any(absent in c for c in report.caveats)


def test_usable_capabilities_are_counted_by_how_well_proven_they_are() -> None:
    """'usable' says a reader exists and the object is present, not that the pair is proven."""
    supported = _pick("SUPPORTED")
    report = build_report(_record({supported: _present(supported, present=True)}))
    assert sum(report.by_validation.values()) == report.totals["usable"]
    row = next(r for r in report.capabilities if r.capability == supported)
    assert row.verdict == "usable"
    assert row.validation in {
        "not_validated",
        "unit_tested",
        "integration_tested",
        "customer_validated",
    }


def test_validation_counts_cover_only_the_usable_set() -> None:
    """How well-proven a capability is only matters for the ones this system can actually use."""
    absent = _pick("SUPPORTED")
    report = build_report(_record({absent: _present(absent, present=False)}))
    assert report.totals.get("absent_on_system") == 1
    assert absent not in {r.capability for r in report.capabilities if r.verdict == "usable"}
    assert sum(report.by_validation.values()) == report.totals.get("usable", 0)


def test_report_says_when_usable_capabilities_are_unproven() -> None:
    supported = _pick("SUPPORTED")
    report = build_report(_record({supported: _present(supported, present=True)}))
    unproven = report.by_validation.get("not_validated", 0) + report.by_validation.get(
        "unit_tested", 0
    )
    if unproven:
        assert any("not been verified against a real BW system" in c for c in report.caveats)


def test_report_states_that_nothing_is_customer_validated_yet() -> None:
    report = build_report(_record({}))
    assert not report.by_validation.get("customer_validated")
    assert any("customer's own system" in c for c in report.caveats)


def test_an_integration_claim_carries_its_release() -> None:
    report = build_report(_record({}))
    for row in report.capabilities:
        if row.validation == "integration_tested":
            assert row.validated_on, f"{row.capability} claims integration testing with no release"


def test_report_cites_the_contract_revision() -> None:
    report = build_report(_record({}))
    assert any("contract revision" in c for c in report.caveats)


def test_release_and_system_are_carried_through() -> None:
    report = build_report(_record({}))
    assert (report.system, report.bw_release) == ("qa", "7.50")


def test_a_table_discovery_finds_but_the_contract_omits_is_flagged() -> None:
    """Package and resolver out of step. Reported as such rather than dropped from the list."""
    report = build_report(_record({"not_a_declared_capability": _present("x", present=True)}))
    row = next(r for r in report.capabilities if r.capability == "not_a_declared_capability")
    assert row.verdict == "not_implemented"
    assert "out of step" in row.reason


def test_row_carries_the_resolved_name_and_row_estimate() -> None:
    supported = _pick("SUPPORTED")
    status = TableStatus(
        logical_name=supported,
        resolved_name="RSSOMETHING",
        present=True,
        schema_name="TESTSCHEMA",
        row_estimate=4321,
    )
    report = build_report(_record({supported: status}))
    row = next(r for r in report.capabilities if r.capability == supported)
    assert (row.resolved_name, row.row_estimate) == ("RSSOMETHING", 4321)
