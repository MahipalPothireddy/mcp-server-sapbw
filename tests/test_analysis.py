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
from datetime import UTC, date, datetime, timedelta
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
    # Plural, matching the capability key. This fixture carried the singular form, which is why
    # nothing caught D60: the fixture agreed with the typo, so the resolution "worked" in tests and
    # emitted a non-existent table name on the real system.
    "auth_values": "RSECVAL",
    # Needed to expand a union provider into the parts that hold data (D58). `cube_field` is here
    # because auto-detection requires a probe's whole table group before it will try it, so without
    # it the InfoCube probe is skipped and a MultiProvider resolves as ObjectNotFound.
    "cube_field": "RSDCUBEIOBJ",
    "multiprovider_part": "RSDCUBEMULTI",
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
        fragments: dict[tuple[str, ...], list[tuple[Any, ...]]] | None = None,
        fail_on: str | None = None,
        budget_after: int | None = None,
    ) -> None:
        self._rows = rows or {}
        # Matched on SQL fragments rather than a table, because one table answers several different
        # questions with different column shapes - RSTRAN is read once per lineage direction, once
        # for a target's transformation list, and once for D33's non-active loaders.
        #
        # A key is a *tuple* and every part must appear, because single substrings could not tell
        # those reads apart: `TARGETNAME = ?` matches both the upstream lineage read and the D33
        # probe, so the probe was handed three-column lineage rows, raised on the unpack, and the
        # section handler recorded a failure no test looked at (D71). The conjunction lets the
        # active-version condition do the discriminating, which is what actually differs.
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
        matched = [key for key in self._fragments if all(part in sql for part in key)]
        # Ambiguity is refused rather than settled by order. Taking the first match is how a
        # collision becomes invisible; raising here makes the fixture name the two keys that
        # overlap.
        assert len(matched) <= 1, f"fragment keys {matched} both match one statement: {sql}"
        if matched:
            return self._fragments[matched[0]]
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
    fragments: dict[tuple[str, ...], list[tuple[Any, ...]]] | None = None,
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
    _assert_no_swallowed_failure(result, kwargs)
    return result


def _assert_no_swallowed_failure(result: Analysis, kwargs: dict[str, Any]) -> None:
    """No section may have failed unless the test asked for a failure (D71).

    A reader that raises is recorded as a ``failed`` section so one broken reader cannot cost the
    others, and that is the behaviour two tests below deliberately exercise. The cost is that a
    fixture whose rows stop matching a reader's SELECT produces the same recorded failure, and every
    test that does not inspect that section keeps passing. That is how D33's ``RSTRAN`` read went in
    with the ``loading_chains`` section raising ``ValueError`` on every call in this module.

    Checked here rather than in each test because every analysis in this module is built through
    ``_analysis``, so a drifted fixture now fails loudly at the point of use.
    """
    if kwargs.get("fail_on") or kwargs.get("budget_after"):
        return  # the failure is the subject of the test
    broken = [f"{s.section}/{s.tool}: {s.detail}" for s in result.steps if s.status == "failed"]
    assert not broken, f"a reader raised and the section handler swallowed it: {broken}"


# A flow with real edges: STAGE_DSO --TR1--> SALES_DSO --TR2--> SALES_CUBE, plus a transformation
# whose routine reads SALES_DSO (the consumer BW's own where-used list cannot show).
#
# Both keys carry the active-version condition, which is what separates a lineage read from D33's
# non-active loader probe over the same table and the same `TARGETNAME = ?` filter.
_FLOW_FRAGMENTS: dict[tuple[str, ...], list[tuple[Any, ...]]] = {
    # lineage downstream: (target name, target tlogo, transformation id)
    ("SOURCENAME = ?", "OBJVERS = 'A'"): [("SALES_CUBE", "CUBE", "TR2")],
    # lineage upstream: (source name, source tlogo, transformation id)
    ("TARGETNAME = ?", "OBJVERS = 'A'"): [("STAGE_DSO", "ODSO", "TR1")],
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


def _impact_with_declared_lookup_consumer() -> ImpactAnalysis:
    """Downstream graph whose consumer is reached only by a lookup BW *declares* (D15).

    RATE_MART is not the root's target and is in no ABAP. A transformation feeding it declares a
    read of the root in its rule metadata, which is how BW's own where-used list knows about it and
    how our consumer list did not.
    """
    prov = Provenance(source_table="RSTRANSTEPADSO", source_key={"TRANID": "TR7"})
    nodes = [
        LineageNode(id="SALES_DSO", object_type="dso", name="SALES_DSO", provenance=prov),
        LineageNode(id="RATE_MART", object_type="adso", name="RATE_MART", provenance=prov),
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
                    src="SALES_DSO",
                    dst="RATE_MART",
                    kind="declared_lookup",
                    derivation="declared",
                    confidence="exact",
                    transformation_id="TR7",
                    evidence=evidence_for("lineage_edge", "declared_lookup"),
                    provenance=prov,
                )
            ],
            node_count=len(nodes),
            edge_count=1,
        ),
        affected_object_count=1,
        declared_lookup_consumers=["RATE_MART"],
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


def test_a_declared_lookup_consumer_reaches_the_consumer_list_as_exact() -> None:
    """D15: BW's own where-used list named this consumer and bw_analyze_object omitted it.

    Found by S01 human verification round 2 against production. The two read relations must stay
    distinguishable: this one is exact and BW records it, the routine-parsed one is a lower bound.
    """
    run = _impact_run(_impact_with_declared_lookup_consumer())
    declared = next(c for c in run.consumers if c.relationship == "consumer_lookup")
    assert declared.ref.name == "RATE_MART"
    assert declared.ref.object_type == "adso"  # typed from the graph, not hardcoded unknown
    assert declared.advisory is False, "BW declares this read; calling it advisory understates it"
    assert declared.evidence is not None
    assert declared.evidence.basis == "observed"
    assert declared.evidence.method == "declared_lookup_rule"
    assert declared.evidence.completeness == "complete"


def test_a_declared_lookup_consumer_is_not_filed_as_a_routine_consumer() -> None:
    """A caller filtering for heuristic edges must not pick up an exact one, or vice versa."""
    run = _impact_run(_impact_with_declared_lookup_consumer())
    assert not [c for c in run.consumers if c.relationship == "consumer_routine"]
    # And the reverse: the routine fixture must not start reporting declared lookups.
    routine_run = _impact_run(_impact_with_routine_consumer())
    assert not [c for c in routine_run.consumers if c.relationship == "consumer_lookup"]


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


# --- a union provider holds no data of its own (D58) -------------------------------------------
#
# A MultiProvider (CUBETYPE 'M') over two parts, one of which the request ledger knows about. The
# rows are shared across providers by the scripted connection, which is fine here: what these tests
# pin down is *which object the currency question was asked about*, not what the answer was.
_UNION_ROWS: dict[str, list[tuple[Any, ...]]] = {
    # Emptied on purpose: auto-detection probes the classic DSO header first, so leaving the shared
    # RSDODSO row in place classifies the subject as a DSO and the union is never recognised.
    "RSDODSO": [],
    # RSDCUBE: CUBETYPE, OBJSTAT, INFOAREA, OWNER, APPL. 'M' is a MultiProvider.
    "RSDCUBE": [("M", "ACT", "SD", "DEVUSER", "SD")],
    # RSDCUBEMULTI: PARTCUBE, POSIT
    "RSDCUBEMULTI": [("PART_ONE", 1), ("PART_TWO", 2)],
}


def _union_service(**kwargs: Any) -> AnalysisService:
    return _service(rows={**_ROWS, **_UNION_ROWS}, **kwargs)


def test_a_union_provider_is_expanded_to_the_parts_that_hold_the_data() -> None:
    """The defect S05 exposed, and the most dangerous answer shape this server can produce.

    A MultiProvider holds no rows and books no requests: it unions its parts when the query runs. So
    a currency check aimed at one finds nothing and says so, a loading-chain lookup finds nothing
    and says so, and the payload reads ``currency: no records found`` next to ``confidence: high,
    6 of 6 sections complete``. That is a silence indistinguishable from a clean bill of health, on
    the one question the tool exists to answer.

    Measured on production: a report over a MultiProvider whose three parts were one current and two
    sixty-six days behind, loaded by a monthly chain that had missed two cycles. The owner later
    confirmed the two flows had been retired deliberately. None of it appeared in the answer.
    """
    result = _union_service().troubleshoot_missing_data("UNION_PROVIDER")
    assert isinstance(result, Analysis), result

    currency_subjects = {
        step.detail for step in result.steps if step.section == "currency" and step.detail
    }
    assert {"PART_ONE", "PART_TWO"} <= currency_subjects, (
        f"currency must be checked on the parts; it was asked about {currency_subjects}"
    )
    assert "UNION_PROVIDER" not in currency_subjects, (
        "the union itself has no request ledger, so diagnosing it only adds a finding that says "
        "nothing"
    )


def test_the_union_expansion_is_stated_rather_than_silently_substituted() -> None:
    """A reader must be able to tell why the answer is about objects they did not ask about."""
    result = _union_service().troubleshoot_missing_data("UNION_PROVIDER")
    assert isinstance(result, Analysis), result

    summary = " ".join(result.summary)
    assert "UNION_PROVIDER" in summary and "union" in summary.lower()
    assert "PART_ONE" in summary and "PART_TWO" in summary

    limitation = next(
        (
            entry
            for entry in result.limitations
            if "UNION_PROVIDER" in entry.scope and "by design" in entry.limitation
        ),
        None,
    )
    assert limitation is not None, (
        "the empty ledger of a union provider is a property of its type, not a gap in the read, "
        "and saying so is what lets a caller tell it apart from a provider that has no requests"
    )


def test_each_loading_chain_row_names_the_provider_it_belongs_to() -> None:
    """With a union expanded, this section runs once per part.

    Three rows reading "loading_chains: 2 record(s)" with nothing to tell them apart is not a
    readable answer - and the point of the whole fix is that one part can be stale while the others
    are current, which is unsayable if the reader cannot match a chain to a part. Caught by
    measuring my own fix on production rather than by the fix being wrong.
    """
    result = _union_service().troubleshoot_missing_data("UNION_PROVIDER")
    assert isinstance(result, Analysis), result

    rows = [step for step in result.steps if step.section == "loading_chains"]
    assert rows, "the loading-chain section should have run for each part"
    assert all(row.detail for row in rows), (
        "every loading_chains row must name its provider, or several identical rows are "
        "indistinguishable"
    )


def test_a_non_union_provider_is_diagnosed_directly_and_not_expanded() -> None:
    """The fix must not add a hop for providers that hold their own data."""
    parts, source = ProvidersRepository(
        ScriptedConnection(rows=_ROWS), _capability()
    ).data_bearing_parts("SALES_DSO")
    assert source == "not_union"
    assert parts == []


def test_a_union_whose_parts_cannot_be_read_is_a_gap_not_an_empty_list() -> None:
    """Absent part rows must never read as "this provider has no parts".

    The union still has parts; they could not be read. Reported as a limitation that says to treat
    currency as unknown, because the alternative - checking the union's own empty ledger and saying
    nothing is wrong - is precisely the D58 failure in a different costume.
    """
    rows = {**_ROWS, **_UNION_ROWS, "RSDCUBEMULTI": []}
    result = _service(rows=rows).troubleshoot_missing_data("UNION_PROVIDER")
    assert isinstance(result, Analysis), result

    limitation = next(
        (
            entry
            for entry in result.limitations
            if "UNION_PROVIDER" in entry.scope and "could not be resolved" in entry.limitation
        ),
        None,
    )
    assert limitation is not None
    assert "NOT as up to date" in limitation.limitation
    assert limitation.reason == "metadata_dead_end"


# --- a union whose parts disagree about how current they are (D33) ------------------------------
#
# The D58 fix made the parts get diagnosed. Nothing compared them. So a MultiProvider with one part
# loaded today and three last loaded six to nine years ago produced four individually accurate
# findings and no statement that one query unions them - which is the fact a reader needs, because a
# report over that union silently mixes current and historic data.
#
# Measured on the reference system: two MultiProviders, six parts each, three of them holding
# 1,081,275 rows between them at 2,442-3,191 days old beside two holding 34 million loaded the same
# day. Both were reported as homogeneous.

_REFERENCE_DAY = date(2026, 7, 30)
_REFERENCE_TS = "20260730060000"

#: D33's frozen-loader probe over RSTRAN: TRANID, OBJVERS, OBJSTAT, SOURCENAME, SOURCETYPE. Keyed on
#: the non-active condition, which is what tells this read apart from the lineage reads above.
_FROZEN_LOADER_FRAGMENT: dict[tuple[str, ...], list[tuple[Any, ...]]] = {
    ("OBJVERS <> 'A'", "OBJSTAT = 'ACT'"): [("TRAN_FROZEN", "R", "ACT", "RETIRED_DSO", "ODSO")],
}


class _AgedConnection(ScriptedConnection):
    """``RSSTATMANPART`` answered per provider, which the flat row fixture cannot do.

    The finding is a comparison *between* parts, so a fixture returning one set of request rows for
    every provider can only ever produce parts of identical age - which is the case the finding has
    to
    stay silent on. Ages are given in days behind the system-wide reference date, exactly as
    ``data_age_days`` is derived.
    """

    def __init__(self, ages: dict[str, int], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ages = ages

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if '"RSSTATMANPART"' not in sql:
            return super().execute_select(sql, parameters)
        self.statements += 1  # kept truthful: the budget counts this read like any other
        return self._requests(sql, [str(p).strip() for p in (parameters or [])])

    def _requests(self, sql: str, params: list[str]) -> list[tuple[Any, ...]]:
        if "MAX(TIMESTAMP_ANF)" in sql:
            # The honest "now": the newest request in the system, never today.
            return [(_REFERENCE_TS,)]
        provider = params[0] if params else ""
        if provider not in self._ages:
            return []
        if "DTA_TYPE" in sql:
            return [("CUBE",)]  # the object model the real cases have
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        day = _REFERENCE_DAY - timedelta(days=self._ages[provider])
        stamp = day.strftime("%Y%m%d") + "060000"
        # RNR, STATUS (@08@ green), TIMESTAMP_ANF, TIMESTAMP_VERB, ANZ_RECS, UPDMODE, OLTP, SOURCE
        return [(f"REQ_{provider}", "@08@", stamp, stamp, 1000, "F", "", "")]


def _mixed_union_risks(ages: dict[str, int], parts: list[str] | None = None) -> list[Any]:
    rows = {**_ROWS, **_UNION_ROWS}
    if parts is not None:
        rows = {**rows, "RSDCUBEMULTI": [(name, i + 1) for i, name in enumerate(parts)]}
    conn = _AgedConnection(ages, rows=rows)
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
            query_auth_exposure=lambda _query: pytest.fail("not reached in these tests"),
        )
    )
    result = service.troubleshoot_missing_data("UNION_PROVIDER")
    assert isinstance(result, Analysis), result
    _assert_no_swallowed_failure(result, {})
    return [r for r in result.risks if "unions parts of very different ages" in r.title]


def test_a_union_whose_parts_are_years_apart_says_the_report_mixes_periods() -> None:
    risks = _mixed_union_risks({"PART_ONE": 0, "PART_TWO": 2500})
    assert len(risks) == 1, "the heterogeneous union must be stated once, about the union"

    risk = risks[0]
    assert risk.severity == "high"  # beyond a year: at least one part predates any current period
    assert "UNION_PROVIDER" in risk.title
    assert "PART_TWO (2500d)" in risk.recommendation
    assert "PART_ONE" in risk.recommendation, "the current part is the comparison, so name it"
    assert "retained history behind a live union is a normal design" in risk.recommendation
    assert risk.metrics["age_spread_days"] == 2500
    assert risk.metrics["part_ages_days"] == {"PART_ONE": 0, "PART_TWO": 2500}
    assert risk.metrics["part_count"] == 2
    # The claim rests on the stale part's own request rows, not on the union - which books none.
    assert risk.evidence
    assert all(p.source_table == "RSSTATMANPART" for p in risk.evidence)


def test_a_union_whose_parts_are_all_current_raises_nothing() -> None:
    """The discriminator. A flag that fires on every union is not a flag.

    Without this the finding could be unconditional and every other assertion here would still pass.
    """
    assert _mixed_union_risks({"PART_ONE": 0, "PART_TWO": 1}) == []


def test_a_spread_inside_a_year_is_reported_rather_than_raised() -> None:
    """Two months apart is worth knowing and is not the same claim as nine years apart."""
    risks = _mixed_union_risks({"PART_ONE": 0, "PART_TWO": 60})
    assert len(risks) == 1
    assert risks[0].severity == "medium"


def test_one_dateable_part_cannot_establish_a_spread() -> None:
    """A comparison needs two sides. A spread derived from one age would invent the other."""
    assert _mixed_union_risks({"PART_ONE": 0}) == []


def test_the_stale_parts_are_counted_against_the_whole_union_not_just_the_dateable_ones() -> None:
    """A part whose ledger cannot be read is not evidence of currency either way.

    ``part_count`` is the union's real membership while the ages cover only what could be dated, so
    "1 of 3" says plainly that the third part was not comparable - which "1 of 1" would hide.
    """
    risks = _mixed_union_risks(
        {"PART_ONE": 0, "PART_TWO": 900}, parts=["PART_ONE", "PART_TWO", "PART_MUTE"]
    )
    assert len(risks) == 1
    assert risks[0].metrics["part_count"] == 3
    assert "1 of 3 part(s)" in risks[0].recommendation
    assert "PART_MUTE" not in risks[0].metrics["part_ages_days"]


# --- "nothing loads this" is qualified where a frozen loader exists (D33) -----------------------


def _no_loader_risks(frozen: bool, subject: str = "SALES_DSO") -> list[Any]:
    fragments = dict(_FROZEN_LOADER_FRAGMENT) if frozen else {}
    result = _service(fragments=fragments).troubleshoot_missing_data(subject)
    assert isinstance(result, Analysis), result
    _assert_no_swallowed_failure(result, {})
    return [r for r in result.risks if "loads" in r.title or "no longer loaded" in r.title]


def test_an_unexplained_orphan_is_still_reported_plainly() -> None:
    risks = _no_loader_risks(frozen=False)
    plain = [r for r in risks if r.title == "No walked process chain loads SALES_DSO"]
    assert len(plain) == 1
    assert plain[0].severity == "high"
    # An absence still cites what was read to establish it, transformations included.
    assert {p.source_table for p in plain[0].evidence} >= {"RSTRAN"}


def test_a_frozen_loader_lowers_the_severity_and_names_what_to_check() -> None:
    """Lowered, not raised - and that is the judgement, not an oversight.

    A populated provider whose only inbound transformation sits at a non-active version was loaded
    and no longer is. Told "no process chain loads this" at ``high``, a reader's reasonable next
    step is to remove it, and on the reference system that means deleting 1,081,275 rows of
    retained sales history. Reported at the same level as a genuine orphan, the category stops being
    worth reading.
    """
    risks = _no_loader_risks(frozen=True)
    assert not any(r.title.startswith("No walked process chain loads") for r in risks), (
        "the plain sentence must be replaced, not printed alongside its own correction"
    )

    qualified = [r for r in risks if "no longer loaded, but it was" in r.title]
    assert len(qualified) == 1
    risk = qualified[0]
    assert risk.severity == "medium"  # below the unexplained case, which is 'high'
    assert "Do not treat this as an orphan" in risk.recommendation
    assert "TRAN_FROZEN (version R, status ACT, from RETIRED_DSO)" in risk.recommendation
    assert risk.metrics["objvers"] == ["R"]
    # Rests on the frozen row itself rather than on an absence.
    assert [p.source_table for p in risk.evidence] == ["RSTRAN"]
    assert risk.affected_objects == ["SALES_DSO", "TRAN_FROZEN"]
