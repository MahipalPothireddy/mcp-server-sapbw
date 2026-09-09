"""Compound analysis: the composition machinery, and the claim that the answer is auditable.

Most of these tests exist because a composed answer fails differently from a granular one. A single
reader returning nothing is visible; the same reader returning nothing *inside* a merged answer
reads as a fact about the system. So the tests here are mostly about the difference between "we
looked and found none" and "we could not look":

* every audit row names a granular tool that is actually registered, so a section can be re-run
* an unsupported section, a failed section and a budget-skipped section are three distinct statuses
* one reader failing never costs the others
* confidence is components, never a percentage, and is never inferred upward from what was gathered

Offline: the connection is scripted, so no BW system is contacted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.core.budget import BudgetExceeded, query_budget
from mcp_server_sapbw.models.analysis import (
    RELATIONSHIP_SECTION,
    Analysis,
    AnalysisLimitation,
    RelatedObject,
)
from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.evidence import Evidence, evidence_for
from mcp_server_sapbw.models.lineage import (
    ImpactAnalysis,
    LineageEdge,
    LineageGraph,
    LineageNode,
)
from mcp_server_sapbw.models.objects import BwObjectRef
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.models.providers import ObjectNotFound
from mcp_server_sapbw.repositories.chains import ChainsRepository
from mcp_server_sapbw.repositories.hana import HanaRepository
from mcp_server_sapbw.repositories.health import HealthRepository
from mcp_server_sapbw.repositories.providers import ProvidersRepository
from mcp_server_sapbw.repositories.queries import QueriesRepository
from mcp_server_sapbw.repositories.transformations import TransformationsRepository
from mcp_server_sapbw.services.analysis import (
    AnalysisReaders,
    AnalysisService,
    _reason_for,
    _Run,
)
from mcp_server_sapbw.services.lineage import LineageService
from mcp_server_sapbw.services.load_closure import LoadClosureService

SCHEMA = "TESTSCHEMA"

# Every logical table the composed readers may reach for. Present by default; a test removes one to
# prove that an absent table becomes a recorded `unsupported` section rather than an empty answer.
_TABLES = {
    "dso_header": "RSDODSO",
    "dso_field": "RSDODSOIOBJ",
    "dso_text": "RSDODSOT",
    "adso_header": "RSOADSO",
    "cube_header": "RSDCUBE",
    "composite_header": "RSOHCPR",
    "infoobject": "RSDIOBJ",
    "infoobject_text": "RSDIOBJT",
    "transformation": "RSTRAN",
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "routine_source": "RSAABAP",
    "dtp": "RSBKDTP",
    "chain_attr": "RSPCCHAINATTR",
    "chain_text": "RSPCCHAINT",
    "chain_edges": "RSPCCHAIN",
    "log_chain": "RSPCLOGCHAIN",
    "process_log": "RSPCPROCESSLOG",
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
    "request_status": "RSSTATMANPART",
    "cs_tables": "M_CS_TABLES",
    "object_dependencies": "OBJECT_DEPENDENCIES",
    "hana_views": "VIEWS",
    "auth_value": "RSECVAL",
}


class ScriptedConnection:
    """Rows by physical table, with two injectable faults.

    ``fail_on`` makes one table's read raise, and ``budget_after`` exhausts the per-call budget
    partway through. Both model things that genuinely happen mid-composition and that a granular
    tool never has to survive: there, the call simply fails.
    """

    def __init__(
        self,
        *,
        rows: dict[str, list[tuple[Any, ...]]] | None = None,
        fragments: dict[str, list[tuple[Any, ...]]] | None = None,
        fail_on: str | None = None,
        budget_after: int | None = None,
    ) -> None:
        self._rows = rows or {}
        # Matched on a SQL fragment rather than a table, because one table answers several different
        # questions with different column shapes - RSTRAN is read once per lineage direction.
        self._fragments = fragments or {}
        self._fail_on = fail_on
        self._budget_after = budget_after
        self.statements = 0

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements += 1
        if self._budget_after is not None and self.statements > self._budget_after:
            raise BudgetExceeded(
                reason="query budget exhausted", queries=self.statements, elapsed_seconds=1.0
            )
        if self._fail_on and f'"{self._fail_on}"' in sql:
            raise RuntimeError("scripted read failure")
        for fragment, rows in self._fragments.items():
            if fragment in sql:
                return rows
        for physical, rows in self._rows.items():
            if f'"{physical}"' in sql:
                return rows
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


# A provider that exists, so the anchor section resolves and the rest of the composition can run.
_ROWS: dict[str, list[tuple[Any, ...]]] = {
    "RSDODSO": [("", "SALES", "DEVUSER", "SD")],
    "RSDODSOIOBJ": [("DOC", 1, "X"), ("AMOUNT", 2, "")],
    "RSDODSOT": [("E", "Sales orders", "Daily sales order line items")],
}


def _service(
    *,
    present: set[str] | None = None,
    rows: dict[str, list[tuple[Any, ...]]] | None = None,
    fragments: dict[str, list[tuple[Any, ...]]] | None = None,
    fail_on: str | None = None,
    budget_after: int | None = None,
) -> AnalysisService:
    conn = ScriptedConnection(
        rows=rows if rows is not None else _ROWS,
        fragments=fragments,
        fail_on=fail_on,
        budget_after=budget_after,
    )
    cap = _capability(present)
    return AnalysisService(
        AnalysisReaders(
            system="qa",
            capability=cap,
            providers=ProvidersRepository(conn, cap),
            lineage=LineageService(conn, cap),
            transformations=TransformationsRepository(conn, cap),
            queries=QueriesRepository(conn, cap),
            chains=ChainsRepository(conn, cap),
            load_closure=LoadClosureService(conn, cap),
            health=HealthRepository(conn, cap),
            hana=HanaRepository(conn, cap),
            query_auth_exposure=lambda _query: pytest.fail("not reached in these tests"),
        )
    )


def _analysis(**kwargs: Any) -> Analysis:
    result = _service(**kwargs).analyze_object("SALES_DSO")
    assert isinstance(result, Analysis), result
    return result


# A flow with real edges: STAGE_DSO --TR1--> SALES_DSO --TR2--> SALES_CUBE, plus a transformation
# whose routine reads SALES_DSO (the consumer BW's own where-used list cannot show).
_FLOW_FRAGMENTS: dict[str, list[tuple[Any, ...]]] = {
    # lineage downstream: (target name, target tlogo, transformation id)
    "SOURCENAME = ?": [("SALES_CUBE", "CUBE", "TR2")],
    # lineage upstream: (source name, source tlogo, transformation id)
    "TARGETNAME = ?": [("STAGE_DSO", "ODSO", "TR1")],
}


# --- the envelope every compound tool returns ---------------------------------------------


def test_an_object_analysis_fills_the_shared_envelope() -> None:
    """One contract across all five questions, so a client learns it once."""
    analysis = _analysis()
    assert analysis.kind == "object"
    assert analysis.system == "qa"
    assert analysis.subject_name == "SALES_DSO"
    assert analysis.summary, "a composed answer with no summary is just five payloads"
    assert analysis.steps, "no audit trail means the answer cannot be checked"
    assert analysis.confidence.level in ("high", "medium", "low")
    assert analysis.next_actions


def test_a_missing_object_is_not_found_rather_than_an_empty_analysis() -> None:
    """An envelope full of nulls would read as 'this object has no dependencies'."""
    result = _service(rows={}).analyze_object("NOSUCH")
    assert isinstance(result, ObjectNotFound)


# --- the audit trail is the point ---------------------------------------------------------


def test_every_audit_row_names_a_tool_that_actually_exists() -> None:
    """The auditability claim, enforced.

    Each section says which granular tool reproduces it. If that name drifts from the registered
    surface the claim silently becomes false, and a reader following it gets an unknown-tool error.
    """

    async def names() -> set[str]:
        async with Client(server.mcp) as client:
            return {t.name for t in await client.list_tools()}

    registered = asyncio.run(names())
    cited = {step.tool for step in _analysis().steps}
    assert cited, "no tools cited"
    assert cited <= registered, f"cited but not registered: {sorted(cited - registered)}"


def test_audit_rows_carry_the_resolved_physical_tables() -> None:
    """Section-level provenance: a reader can see which tables an answer rests on."""
    rows = _analysis().steps
    tables = {table for step in rows for table in step.source_tables}
    assert "RSDODSO" in tables, "the resolved physical name should be recorded, not the logical one"


def test_each_section_reports_a_status_and_a_count() -> None:
    analysis = _analysis()
    definition = next(s for s in analysis.steps if s.section == "definition")
    assert definition.status == "complete"
    assert definition.record_count == 2  # the two scripted DSO fields


# --- one reader failing must not cost the others ------------------------------------------


def test_an_absent_table_makes_a_section_unsupported_not_empty() -> None:
    """'This release cannot report consumers' and 'there are none' are different answers."""
    analysis = _analysis(present=set(_TABLES) - {"object_dependencies"})
    calcviews = next(s for s in analysis.steps if s.section == "consumer_calcviews")
    assert calcviews.status == "unsupported"
    assert any(limitation.reason == "unsupported_on_release" for limitation in analysis.limitations)
    # and the rest of the analysis still arrived
    assert analysis.definition is not None
    assert next(s for s in analysis.steps if s.section == "definition").status == "complete"


def test_a_broken_reader_is_recorded_and_the_analysis_continues() -> None:
    analysis = _analysis(fail_on="RSZCOMPDIR")
    failed = [s for s in analysis.steps if s.status == "failed"]
    assert failed, "a raising reader should be recorded, not swallowed"
    assert analysis.definition is not None
    assert analysis.confidence.sections_failed == len(failed)
    assert any("could not be read" in limitation.limitation for limitation in analysis.limitations)


def test_an_unsupported_section_is_not_counted_as_complete() -> None:
    analysis = _analysis(present=set(_TABLES) - {"object_dependencies"})
    assert analysis.confidence.sections_unsupported >= 1
    assert analysis.confidence.level in ("medium", "low")


# --- the budget: a partial answer beats no answer -----------------------------------------


def test_budget_exhaustion_returns_the_sections_already_gathered() -> None:
    """The deliberate difference from a granular tool, which returns a BudgetResult and nothing.

    Five readers draw on one per-call allowance, so a later section can exhaust what earlier ones
    left. Losing four completed sections to report the fifth would be a worse answer.
    """
    analysis = _analysis(budget_after=4)
    assert analysis.stopped_on_budget is True
    assert analysis.definition is not None, "sections gathered before the budget ran out are kept"
    assert any(s.status == "skipped_budget" for s in analysis.steps)
    assert any(limitation.reason == "budget_exhausted" for limitation in analysis.limitations)


def test_sections_after_the_budget_are_not_attempted_again() -> None:
    """Retrying would raise on the first statement, so remaining rows are recorded, not tried."""
    conn = ScriptedConnection(rows=_ROWS, budget_after=4)
    cap = _capability()
    service = AnalysisService(
        AnalysisReaders(
            system="qa",
            capability=cap,
            providers=ProvidersRepository(conn, cap),
            lineage=LineageService(conn, cap),
            transformations=TransformationsRepository(conn, cap),
            queries=QueriesRepository(conn, cap),
            chains=ChainsRepository(conn, cap),
            load_closure=LoadClosureService(conn, cap),
            health=HealthRepository(conn, cap),
            hana=HanaRepository(conn, cap),
            query_auth_exposure=lambda _q: pytest.fail("not reached"),
        )
    )
    result = service.analyze_object("SALES_DSO")
    assert isinstance(result, Analysis)
    spent = conn.statements
    skipped = [s for s in result.steps if s.status == "skipped_budget"]
    assert len(skipped) > 1, "only the first over-budget section was recorded"
    # One statement over the bound raised; nothing after it was attempted.
    assert spent == 5


# --- confidence is components, never one number -------------------------------------------


def test_confidence_carries_components_and_reasons_not_a_score() -> None:
    """Collapsing coverage and evidence basis into a percentage would hide which one failed."""
    confidence = _analysis().confidence
    assert set(confidence.model_dump()) >= {
        "level",
        "sections_total",
        "sections_complete",
        "sections_unsupported",
        "sections_failed",
        "sections_skipped",
        "advisory_relationships",
        "reasons",
    }
    assert confidence.reasons
    assert not any(
        isinstance(value, float) and 0.0 < value < 1.0 for value in confidence.model_dump().values()
    ), "confidence must not carry a probability-shaped number"


def test_a_clean_run_says_so_rather_than_leaving_reasons_empty() -> None:
    """An empty reasons list would read as 'unknown' rather than 'nothing was wrong'."""
    confidence = _analysis().confidence
    if confidence.sections_unsupported == 0 and confidence.sections_failed == 0:
        assert any("every applicable section" in reason for reason in confidence.reasons)


def test_gaps_lower_the_level_and_are_counted() -> None:
    degraded = _analysis(present=set(_TABLES) - {"object_dependencies", "request_status", "dtp"})
    assert degraded.confidence.sections_unsupported >= 2
    assert degraded.confidence.level in ("medium", "low")


def test_a_not_applicable_section_is_not_a_gap() -> None:
    """A section that does not apply to this subject must not read as a coverage failure."""
    run = _Run(_service()._r)
    run.note_section("irrelevant", "bw_list_systems", "not_applicable")
    confidence = run.confidence()
    assert confidence.sections_total == 0
    assert confidence.sections_unsupported == 0


# --- relationships resolved from a real flow ----------------------------------------------


def test_lineage_edges_become_dependencies_and_consumers() -> None:
    """The normalised view: one list answers 'what feeds this' whichever reader established it."""
    analysis = _analysis(fragments=_FLOW_FRAGMENTS)
    assert "dso:STAGE_DSO" in {item.id for item in analysis.dependencies}
    assert "infocube:SALES_CUBE" in {item.id for item in analysis.consumers}


def test_a_relationship_records_the_transformation_it_runs_through() -> None:
    """Without `via` a caller knows two objects are connected but not where to look."""
    analysis = _analysis(fragments=_FLOW_FRAGMENTS)
    upstream = next(i for i in analysis.dependencies if i.id == "dso:STAGE_DSO")
    assert upstream.via == "TR1"
    assert upstream.relationship == "upstream"


def test_the_lineage_section_walks_upstream_only() -> None:
    """The downstream half is walked by the impact section, which also finds routine consumers.

    Asking for both directions here traversed every downstream hop twice: measured on a real DSO the
    redundant call was 21s of a 31s analysis. Both graphs are still returned - `lineage` upstream
    and `impact.graph` downstream - so nothing is lost and no hop is paid for twice.
    """
    analysis = _analysis(fragments=_FLOW_FRAGMENTS)
    assert analysis.lineage is not None
    assert analysis.lineage.direction == "upstream"
    assert analysis.impact is not None
    assert analysis.impact.graph.direction == "downstream"


def test_a_declared_edge_is_not_marked_advisory() -> None:
    """Advisory is reserved for derived links; marking a declared one would devalue the flag."""
    analysis = _analysis(fragments=_FLOW_FRAGMENTS)
    assert not any(item.advisory for item in analysis.dependencies)


def test_the_summary_states_the_reach_of_a_change_split_by_basis() -> None:
    result = _service(fragments=_FLOW_FRAGMENTS).assess_change_impact("SALES_DSO")
    assert isinstance(result, Analysis)
    joined = " ".join(result.summary)
    assert "declared" in joined
    assert result.kind == "change_impact"


# --- relationships: declared and advisory never merge -------------------------------------


def test_a_declared_relationship_wins_over_an_advisory_duplicate() -> None:
    """Two readers can see the same edge; keeping the advisory one would understate it."""
    ref = BwObjectRef(object_type="dso", name="STAGE_DSO")
    analysis = Analysis(
        kind="object",
        system="qa",
        subject_name="X",
        title="t",
        consumers=[
            RelatedObject(ref=ref, relationship="downstream", advisory=True),
            RelatedObject(ref=ref, relationship="downstream", advisory=False),
        ],
        confidence=_analysis().confidence,
    )
    assert len(analysis.consumers) == 1
    assert analysis.consumers[0].advisory is False


def test_the_same_object_in_two_roles_is_kept_twice() -> None:
    """Deduping on the object alone would drop the fact that it is both a source and a consumer."""
    ref = BwObjectRef(object_type="dso", name="STAGE_DSO")
    analysis = Analysis(
        kind="object",
        system="qa",
        subject_name="X",
        title="t",
        consumers=[
            RelatedObject(ref=ref, relationship="downstream"),
            RelatedObject(ref=ref, relationship="consumer_routine", advisory=True),
        ],
        confidence=_analysis().confidence,
    )
    assert len(analysis.consumers) == 2
    assert analysis.advisory_consumer_count == 1


# --- risks --------------------------------------------------------------------------------


def test_risks_are_ordered_most_severe_first() -> None:
    analysis = _analysis()
    severities = [risk.severity for risk in analysis.risks]
    ranks = ["info", "low", "medium", "high", "critical"]
    assert severities == sorted(severities, key=ranks.index, reverse=True)


def test_every_risk_carries_a_recommendation() -> None:
    """A finding without an action is an observation, and the caller has to work out what to do."""
    for risk in _analysis().risks:
        assert risk.recommendation.strip()
        assert risk.scenario.startswith("analysis.")


def test_a_risk_is_never_raised_about_a_section_that_did_not_run() -> None:
    """Judgement is derived only from records this analysis actually holds."""
    analysis = _analysis(present=set(_TABLES) - {"request_status", "cs_tables"})
    health_step = next(s for s in analysis.steps if s.section == "health")
    assert health_step.status == "unsupported"
    assert not any("changelog" in risk.title for risk in analysis.risks)


# --- next actions are runnable -------------------------------------------------------------


def test_next_actions_name_a_tool_and_say_why() -> None:
    for action in _analysis().next_actions:
        assert action.why.strip()
        if action.tool:
            assert action.tool.startswith("bw_")


def test_next_actions_are_ordered() -> None:
    orders = [action.order for action in _analysis().next_actions]
    assert orders == sorted(orders)


# --- caveat classification ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("caveat", "expected"),
    [
        ("routine reads are a heuristic lower bound", "heuristic_lower_bound"),
        ("advisory: resolved by naming convention", "heuristic_lower_bound"),
        ("response summarised: 60 of 900 nodes shown", "truncated"),
        ("the row cap bound on providers", "truncated"),
        ("customer-exit variables resolve in ABAP at runtime", "metadata_dead_end"),
        ("something this build does not classify", "reader_caveat"),
    ],
)
def test_a_reader_caveat_is_classified_for_machine_use(caveat: str, expected: str) -> None:
    assert _reason_for(caveat) == expected


def test_an_unclassifiable_caveat_is_still_carried_verbatim() -> None:
    """Mis-labelling a reason would be worse than admitting it is unclassified."""
    run = _Run(_service()._r)
    run.absorb("section", ["a caveat with no recognisable marker"])
    assert run.limitations == [
        AnalysisLimitation(
            scope="section",
            limitation="a caveat with no recognisable marker",
            reason="reader_caveat",
        )
    ]


def test_constituent_caveats_are_carried_up_not_dropped() -> None:
    """The classic composition bug: parts state their limits, the whole does not."""
    analysis = _analysis(present=set(_TABLES) - {"cs_tables"})
    assert analysis.limitations, "a degraded run reported no limitation at all"


# --- the contracted envelope fields --------------------------------------------------------
#
# Six fields were specified for the compound contract and none existed: findings, evidence,
# execution, budget, sections_skipped, validation_status. The information mostly existed but under
# other names, and three things were genuinely absent - query count, duration, and budget spend on a
# call that *succeeded*. These pin all six down, including the distinctions that make them honest:
# a skipped section is not an empty one, an unmeasured count is not zero, and a validation status
# with no stated basis is not checkable.


def test_every_contracted_envelope_field_is_present() -> None:
    """The additive contract. Existing published fields must survive alongside the new ones."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)

    for field in (
        "summary",
        "findings",
        "evidence",
        "dependencies",
        "consumers",
        "risks",
        "limitations",
        "next_actions",
        "execution",
        "budget",
        "sections_skipped",
        "validation_status",
    ):
        assert hasattr(report, field), f"contracted field {field} is missing"

    # Backward compatibility: nothing that was published before may have been renamed away.
    for legacy in ("kind", "system", "subject_name", "title", "steps", "confidence", "risks"):
        assert hasattr(report, legacy), f"published field {legacy} was removed"


def test_findings_are_traceable_to_a_step_or_the_declared_relationship_view() -> None:
    """A finding whose section names nothing cannot be reproduced, so it is a dead end.

    Two legitimate sources: a step that ran, or the reserved relationship label. A relationship
    finding is assembled from several sections, so naming any one of them would be false.
    """
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    allowed = {row.section for row in report.steps} | {RELATIONSHIP_SECTION}

    assert report.findings
    for finding in report.findings:
        assert finding.section in allowed, f"{finding.section!r} names neither a step nor the view"
        assert finding.statement.strip()
        assert finding.basis in {"observed", "derived", "inferred", "unknown"}


def test_a_derived_relationship_makes_its_finding_inferred_and_a_lower_bound() -> None:
    """The headline distinction: routine-parsed consumers must never read as a complete set.

    Driven through `_Run` directly because no fixture in this module produces an advisory edge via
    the service - `_FLOW_FRAGMENTS` is all declared, and another test asserts exactly that. Faking
    one through the fixture would test the fixture rather than the derivation rule.
    """
    run = _Run(_service()._r, tool_name="bw_analyze_object")
    run.subject_label = "SALES_DSO"
    run.relate(
        run.consumers,
        BwObjectRef(object_type="dso", name="OTHER_DSO"),
        "consumer_routine",
        advisory=True,
        evidence=evidence_for("lineage_edge", "advisory"),
    )
    run.relate(run.consumers, BwObjectRef(object_type="dso", name="PLAIN_DSO"), "downstream")

    finding = next(f for f in run.findings() if "depend on" in f.statement)
    assert finding.basis == "inferred"
    assert finding.completeness == "lower_bound"
    assert "1 declared, 1 derived" in finding.statement
    assert finding.section == RELATIONSHIP_SECTION


def test_an_all_declared_relationship_set_is_observed_and_complete() -> None:
    """The other side of the same rule: a declared set must not be devalued to a lower bound."""
    run = _Run(_service()._r, tool_name="bw_analyze_object")
    run.subject_label = "SALES_DSO"
    run.relate(run.dependencies, BwObjectRef(object_type="dso", name="STAGE_DSO"), "upstream")

    finding = next(f for f in run.findings() if "feed" in f.statement)
    assert finding.completeness == "complete"
    assert "all declared" in finding.statement
    assert finding.basis == "observed"


def test_evidence_is_deduplicated_and_ordered_weakest_first() -> None:
    """The inferred entries are what a reader has to weigh; burying them defeats the purpose."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    assert report.evidence

    keys = [(e.basis, e.method) for e in report.evidence]
    assert len(keys) == len(set(keys)), "evidence repeats a (basis, method) pair"
    ranks = [e.rank for e in report.evidence]
    assert ranks == sorted(ranks, reverse=True), "evidence is not weakest-first"


def test_evidence_reuses_the_one_trust_model() -> None:
    """No second vocabulary: every entry is the shared Evidence, with a method kept verbatim."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    for item in report.evidence:
        assert isinstance(item, Evidence)
        assert item.method.strip()
        assert item.detail, "an evidence entry with no detail cannot answer 'how do you know'"


def test_execution_names_only_tools_that_exist_and_measures_duration() -> None:
    report = _analysis(fragments=_FLOW_FRAGMENTS)

    assert report.execution.step_count == len(report.steps)
    assert report.execution.tools_used == sorted({row.tool for row in report.steps})
    assert all(name.startswith("bw_") for name in report.execution.tools_used)
    assert report.execution.duration_ms >= 0
    assert report.execution.sections_answered <= report.execution.step_count


def test_queries_executed_is_none_rather_than_zero_when_nothing_counted() -> None:
    """Zero statements and "nobody was counting" are opposite readings, so they stay apart.

    The offline fixtures substitute their own connection and never charge the budget, so with no
    budget scope installed the count must be absent rather than reported as 0.
    """
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    assert report.execution.queries_executed is None
    assert report.budget.measured is False
    assert report.budget.query_limit is None
    assert report.budget.queries_used is None


def test_budget_is_reported_on_a_successful_call_not_only_on_exhaustion() -> None:
    """The defect this closes: spend was visible only when the budget ran out."""
    with query_budget(max_queries=500, max_seconds=30) as budget:
        report = _analysis(fragments=_FLOW_FRAGMENTS)
        assert budget is not None

    assert report.budget.measured is True
    assert report.budget.query_limit == 500
    assert report.budget.time_limit_ms == 30_000
    assert report.budget.queries_used is not None
    assert report.budget.truncated is False
    assert report.budget.time_used_ms is not None
    assert report.execution.queries_executed is not None


def test_budget_spend_is_a_delta_over_this_analysis_not_the_whole_session() -> None:
    """A budget shared with earlier work must not attribute that work to this analysis."""
    with query_budget(max_queries=500, max_seconds=30) as budget:
        for _ in range(7):
            budget.charge()  # work that happened before the analysis started
        report = _analysis(fragments=_FLOW_FRAGMENTS)

    assert report.budget.queries_used is not None
    assert report.budget.queries_used < budget.queries, (
        "the analysis claimed statements charged before it began"
    )


def test_budget_exhaustion_is_reported_as_truncated() -> None:
    report = _analysis(fragments=_FLOW_FRAGMENTS, budget_after=3)
    assert report.stopped_on_budget is True
    assert report.budget.truncated is True
    assert any(s.status == "skipped_budget" for s in report.sections_skipped)


def test_sections_skipped_excludes_a_section_that_ran_and_found_nothing() -> None:
    """An empty answer is an answer. Listing it as skipped would invent a gap."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    skipped = {s.section for s in report.sections_skipped}
    empty = {row.section for row in report.steps if row.status == "empty"}
    not_applicable = {row.section for row in report.steps if row.status == "not_applicable"}

    assert not (skipped & empty), "a section that found nothing was reported as skipped"
    assert not (skipped & not_applicable), "a section that did not apply was reported as skipped"


def test_every_skipped_section_states_a_reason_and_names_its_tool() -> None:
    report = _analysis(present={"dso_header", "dso_text", "dso_field"})
    assert report.sections_skipped, "fixture no longer produces a skipped section"
    for skip in report.sections_skipped:
        assert skip.reason.strip(), f"{skip.section} was skipped without a reason"
        assert skip.tool.startswith("bw_")
        assert skip.status in {"unsupported", "connector_required", "skipped_budget", "failed"}


def test_sections_skipped_agrees_with_the_audit_trail() -> None:
    """Two views of the same fact must not disagree, or a caller cannot trust either."""
    report = _analysis(present={"dso_header", "dso_text", "dso_field"})
    owed = {"unsupported", "connector_required", "skipped_budget", "failed"}
    from_steps = sorted((row.section, row.status) for row in report.steps if row.status in owed)
    from_field = sorted((s.section, s.status) for s in report.sections_skipped)
    assert from_steps == from_field


def test_validation_status_is_the_weakest_link_and_states_which_capability() -> None:
    """One unproven reader must not be hidden by four proven ones."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)

    assert report.validation_status in {
        "not_validated",
        "unit_tested",
        "integration_tested",
        "real_bw_validated",
        "customer_validated",
    }
    assert report.validation_basis.strip(), "a status with no stated basis is not checkable"


def test_nothing_claims_real_bw_validation_from_the_offline_build() -> None:
    """That rung is emitted by the validation matrix, never by the contract the suite produces."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    assert report.validation_status not in {"real_bw_validated", "customer_validated"}


def test_confidence_still_carries_no_percentage() -> None:
    """The standing rule survives the new fields."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    dumped = report.confidence.model_dump()
    assert "percent" not in str(dumped).lower()
    assert report.confidence.level in {"high", "medium", "low"}


def test_the_envelope_round_trips_through_json() -> None:
    """Every new field has to survive serialisation, which is how a client actually receives it."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    restored = Analysis.model_validate_json(report.model_dump_json())

    assert restored.validation_status == report.validation_status
    assert len(restored.findings) == len(report.findings)
    assert len(restored.evidence) == len(report.evidence)
    assert restored.execution.tools_used == report.execution.tools_used
    assert restored.budget.measured == report.budget.measured
    assert len(restored.sections_skipped) == len(report.sections_skipped)


# --- the impact section's own mapping to RelatedObject -----------------------------------------
#
# Found by running S01 against a production ADSO: all four routine-derived consumers came back typed
# `unknown` and carrying no Evidence at all, while the declared consumers beside them were typed
# correctly. S01 names the requirement explicitly - a routine-derived consumer must be
# basis=inferred, method=routine_select_parse, completeness=lower_bound - so the items whose
# uncertainty matters most were the only ones that said nothing about it.
#
# Driven through `_add_impact` with a crafted ImpactAnalysis rather than through the fixture: the
# defect is in the mapping from ImpactAnalysis to RelatedObject, and reaching it through a reverse
# RSAABAP scan would test the fixture's SQL emulation instead of the rule.


def _impact_with_routine_consumer() -> ImpactAnalysis:
    """Downstream graph holding one declared consumer and one reached only through a routine."""
    prov = Provenance(source_table="RSTRAN", source_key={"TRANID": "TR9"})
    nodes = [
        LineageNode(id="SALES_DSO", object_type="dso", name="SALES_DSO", provenance=prov),
        LineageNode(id="MART_ADSO", object_type="adso", name="MART_ADSO", provenance=prov),
        # The routine consumer *is* in the graph, with its real type. That is what makes hardcoding
        # "unknown" a discarded answer rather than an unavoidable gap.
        LineageNode(id="LOOKUP_CUBE", object_type="infocube", name="LOOKUP_CUBE", provenance=prov),
    ]
    return ImpactAnalysis(
        root_id="SALES_DSO",
        graph=LineageGraph(
            root_id="SALES_DSO",
            direction="downstream",
            depth=2,
            nodes=nodes,
            edges=[
                LineageEdge(
                    src="SALES_DSO", dst="MART_ADSO", kind="transformation", provenance=prov
                )
            ],
            node_count=len(nodes),
            edge_count=1,
        ),
        affected_object_count=2,
        routine_lookup_consumers=["LOOKUP_CUBE"],
    )


def _impact_run(impact: ImpactAnalysis | None = None) -> _Run:
    """Drive ``_add_impact`` over a supplied ImpactAnalysis, and hand back the run it filled."""
    service = _service()
    run = _Run(service._r, tool_name="bw_analyze_object")
    run.subject_label = "SALES_DSO"
    fixed = impact if impact is not None else _impact_with_routine_consumer()

    def stub(name: str, *, depth: int = 2) -> ImpactAnalysis:
        return fixed

    service._r.lineage.impact_analysis = stub  # type: ignore[method-assign]
    service._add_impact(run, "SALES_DSO", depth=2)
    # `_add_impact` routes failures through `run.step`, which records a failed section instead of
    # raising. Without this, a broken stub would leave the lists empty and every assertion below
    # would pass vacuously - which is exactly what happened when this fixture first went in.
    statuses = {step.section: step.status for step in run.steps}
    assert statuses.get("downstream") == "complete", f"the impact section did not run: {statuses}"
    return run


def _impact_consumers() -> list[RelatedObject]:
    return _impact_run().consumers


def test_a_routine_derived_consumer_carries_the_evidence_s01_requires() -> None:
    routine = next(c for c in _impact_consumers() if c.relationship == "consumer_routine")
    assert routine.advisory is True
    assert routine.evidence is not None, "the least certain item was the one with no evidence"
    assert routine.evidence.basis == "inferred"
    assert routine.evidence.method == "routine_select_parse"
    assert routine.evidence.completeness == "lower_bound"


def test_a_routine_derived_consumer_is_typed_from_the_graph_not_hardcoded_unknown() -> None:
    """The graph already holds the node's type; asserting 'unknown' threw that away."""
    routine = next(c for c in _impact_consumers() if c.relationship == "consumer_routine")
    assert routine.ref.name == "LOOKUP_CUBE"
    assert routine.ref.object_type == "infocube"


def test_a_consumer_named_by_no_graph_node_still_degrades_to_unknown() -> None:
    """Typing from the graph must not become a crash when the name is not in it."""
    impact = _impact_with_routine_consumer()
    impact.routine_lookup_consumers = ["NOT_IN_THE_GRAPH"]
    run = _impact_run(impact)
    routine = next(c for c in run.consumers if c.relationship == "consumer_routine")
    assert routine.ref.object_type == "unknown"
    assert routine.ref.name == "NOT_IN_THE_GRAPH"


def test_a_declared_downstream_consumer_says_it_is_declared() -> None:
    """Its note claims a declared transformation, so its evidence has to agree.

    Left unset, the strongest half of the consumer list carried no basis while the weakest half
    did - which inverts what a reader needs from the field.
    """
    declared = [c for c in _impact_consumers() if c.relationship == "downstream"]
    assert declared, "the fixture must produce a declared consumer for this to mean anything"
    for consumer in declared:
        assert consumer.advisory is False
        assert consumer.evidence is not None
        assert consumer.evidence.basis == "observed"
        assert consumer.evidence.method == "declared_metadata"


def test_the_routine_consumer_now_reaches_the_answers_own_evidence_summary() -> None:
    """`relate` collects only the evidence it is given, so an omitted one never reached it."""
    methods = {e.method for e in _impact_run().evidence}
    assert "routine_select_parse" in methods
    assert "declared_metadata" in methods


def test_every_related_object_carries_evidence_whatever_its_relationship() -> None:
    """The invariant, rather than one case at a time.

    Mission Rule 3 wants every fact traceable, and `Evidence` is how this server says *how firmly*.
    A related object with none is a claim with no stated basis - and the gaps were not random. They
    were whole branches (routine consumers, then declared queries), so a per-case test would have
    kept passing while the next branch shipped without one. Asserted over the whole set, so a new
    relationship cannot be added without one.
    """
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    missing = [
        f"{item.relationship}:{item.ref.object_type}"
        for item in [*report.dependencies, *report.consumers]
        if item.evidence is None
    ]
    assert not missing, f"related objects with no evidence: {sorted(set(missing))}"


def test_an_advisory_relationship_never_claims_an_observed_basis() -> None:
    """`advisory` and the basis say the same thing two ways, so they must not disagree."""
    report = _analysis(fragments=_FLOW_FRAGMENTS)
    for item in [*report.dependencies, *report.consumers]:
        assert item.evidence is not None
        if item.advisory:
            assert item.evidence.basis in {"inferred", "unknown"}, (
                f"{item.relationship} is flagged advisory but claims basis={item.evidence.basis}"
            )
        else:
            assert item.evidence.basis in {"observed", "derived"}, (
                f"{item.relationship} is not advisory but claims basis={item.evidence.basis}"
            )
