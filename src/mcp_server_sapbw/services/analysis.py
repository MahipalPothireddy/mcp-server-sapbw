"""Composes the granular readers into one answer per analyst question.

Five questions, one envelope. Each analysis is a fixed sequence of readers whose results are folded
into an :class:`Analysis`, with an audit row per reader naming the granular tool that reproduces it.

**Three rules hold everywhere in this module**, and they are what separate a composed answer from a
merged one:

1. **One reader failing never loses the others.** :meth:`_Run.step` runs each reader inside a guard
   that turns an unsupported release, a missing connector or a broken read into a recorded status.
   The anchor section is the single exception: without the subject there is nothing to compose.
2. **Judgement is derived only from facts already gathered, and stays in ``risks``.** No analyzer is
   re-run here. A risk is a reading of a record this analysis already holds, and it cites that
   record - so nothing in ``risks`` can be true of a section that did not run.
3. **The budget is shared, so exhausting it returns a partial answer rather than nothing.** Five
   readers draw on one per-call allowance; see :meth:`_Run.step`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ..core.budget import BudgetExceeded, current_budget
from ..core.validation import validation_for_tool
from ..models.analysis import (
    RELATIONSHIP_SECTION,
    Analysis,
    AnalysisBudget,
    AnalysisConfidence,
    AnalysisExecution,
    AnalysisFinding,
    AnalysisKind,
    AnalysisLimitation,
    AnalysisStep,
    LimitationReason,
    NextAction,
    RelatedObject,
    Relationship,
    SectionStatus,
    SkippedSection,
)
from ..models.capability import CapabilityRecord, ValidationStatus
from ..models.chains import Chain, ChainCadence, ChainRuntimes, LoadClosure
from ..models.completeness import BoundHit, Completeness
from ..models.ecc import ConnectorUnavailable
from ..models.evidence import Evidence, EvidenceSummary, evidence_for, summarise
from ..models.findings import Finding, Severity
from ..models.health import ProviderHealth
from ..models.lineage import ImpactAnalysis, LineageGraph
from ..models.objects import BwObjectRef, normalise_object_type
from ..models.provenance import Provenance, UnsupportedResult
from ..models.providers import ObjectNotFound, Provider
from ..models.queries import Query, QueryCondition, QueryLineage, QueryUsage
from ..models.security import QueryAuthExposure
from ..repositories.chains import ChainsRepository
from ..repositories.hana import HanaRepository
from ..repositories.health import HealthRepository
from ..repositories.providers import ProvidersRepository
from ..repositories.queries import QueriesRepository
from ..repositories.transformations import TransformationsRepository
from .lineage import LineageService
from .load_closure import LoadClosureService

_T = TypeVar("_T")

#: Every shape a reader returns instead of an answer. Named once so :meth:`_Run.step` can strip them
#: from its return type, which is what lets a composed section be type-checked as data.
_Failure = UnsupportedResult | ConnectorUnavailable | ObjectNotFound
#: What an *anchor* can fail with. Narrower than ``_Failure`` because the section an analysis rests
#: on is always a BW read - a connector is never the subject - so the tool's own return union does
#: not have to advertise a shape it can never produce.
_AnchorFailure = UnsupportedResult | ObjectNotFound

#: Objects named on one mechanical finding before it is capped. The count is the signal; naming a
#: thousand objects on a single finding would bury it.
_FINDING_OBJECTS = 20

#: Fallback wording per skip status, used when the step recorded no detail of its own. Every skip
#: has to say why, so this makes an unexplained one impossible rather than merely unlikely.
_SKIP_REASONS: dict[str, str] = {
    "unsupported": "this release does not carry the metadata the section reads",
    "connector_required": "the answer is completed by a system outside BW",
    "skipped_budget": "the per-call budget was spent before this section ran",
    "failed": "the section's read broke",
}

#: How many consumer queries and calc-view crossings to resolve for one subject. A compound tool
#: already spends several reads; an unbounded consumer scan would turn one question into a full
#: catalogue walk.
_CONSUMER_CAP = 200
#: Crossing rows to scan when answering "which calc views read this object". One statement, bounded.
_CROSSING_CAP = 500
#: Feeding providers to diagnose in one missing-data walk, and inbound transformations to name.
_DIAGNOSE_CAP = 5
_LOGIC_CAP = 25
#: How many objects a summary sentence names before it says "and others".
_NAMED_CHAINS = 3
#: How many part providers a union-expansion sentence names before eliding (D58).
_NAMED_PARTS = 6
#: How many affected objects a risk lists. The full set is in `dependencies` / `consumers`.
_RISK_OBJECTS = 10

# Thresholds behind the derived risks. Named because each one is a judgement, and a reader should be
# able to see and disagree with the number rather than find it inline.
#: A chain succeeding on less than this share of its runs is a finding; below _FAILING_RATE it is
#: critical.
_UNRELIABLE_RATE = 0.95
_FAILING_RATE = 0.80
#: p95 more than this multiple of the median means the duration is not a property of the chain.
_VARIANCE_FACTOR = 3
#: Changelog larger than this multiple of the active table is worth reporting on a HANA system.
_CHANGELOG_FACTOR = 3
#: Data older than this many days behind the newest request in the system is worth reporting;
#: beyond _STALE_HIGH_DAYS it is raised.
_STALE_DAYS = 1
_STALE_HIGH_DAYS = 7
#: Age spread across a union's parts at which the union is reported as mixing currencies (D33).
#:
#: Thirty days, reasoned rather than picked. A union whose parts differ by hours or days is normal -
#: daily loads finish at different times, and a monthly part is legitimately four weeks behind its
#: daily neighbours. What is worth a finding is a part that is behind by more than any ordinary
#: cadence explains, because then a report over the union is mixing periods rather than lagging.
#: Measured on the reference system the real cases are not close to the line: the spread is
#: 2,442-3,191 days against parts loaded the same day.
_MIXED_CURRENCY_SPREAD_DAYS = 30
#: Spread above which the mixed union is raised rather than reported. A year means at least one part
#: predates every reporting period a current query is likely to compare against.
_MIXED_CURRENCY_HIGH_DAYS = 365
#: How many parts must be dateable before their ages can be compared at all. Below this there is no
#: spread to report, and reporting one from a single age would be inventing the other side (D33).
_COMPARABLE_PARTS = 2


@dataclass(frozen=True)
class AnalysisReaders:
    """The readers an analysis composes, already built for one system.

    Injected rather than constructed here for two reasons. The security repository must be built
    without a cache - permission data is never written to disk - and that rule is enforced in one
    place on the runtime; re-deriving it here would be a second place to get it wrong. And
    ``LineageService`` and ``TransformationsRepository`` memoise per instance, so an analysis has to
    reuse one instance of each across its sections or it throws the memo away on every call.
    """

    system: str
    capability: CapabilityRecord
    providers: ProvidersRepository
    lineage: LineageService
    transformations: TransformationsRepository
    queries: QueriesRepository
    chains: ChainsRepository
    load_closure: LoadClosureService
    health: HealthRepository
    hana: HanaRepository
    #: Bound to the system already, because the join spans the query and security subsystems and
    #: lives on the runtime for that reason.
    query_auth_exposure: Callable[[str], QueryAuthExposure | UnsupportedResult]


@dataclass
class _Run:
    """Accumulates one analysis: its sections, audit rows, limitations and evidence."""

    readers: AnalysisReaders
    steps: list[AnalysisStep] = field(default_factory=list)
    limitations: list[AnalysisLimitation] = field(default_factory=list)
    dependencies: list[RelatedObject] = field(default_factory=list)
    consumers: list[RelatedObject] = field(default_factory=list)
    risks: list[Finding] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    budget_exhausted: bool = False
    #: Wall clock and budget spend at the moment the run started, so both are a delta over *this*
    #: analysis rather than whatever the session had already accumulated.
    started_at: float = field(default_factory=time.monotonic)
    queries_at_start: int | None = None
    #: The tool whose registration is running, for the validation lookup. Set by the service.
    tool_name: str = ""
    #: The subject's name, for readable finding statements. Set in :meth:`build`.
    subject_label: str = ""

    def __post_init__(self) -> None:
        budget = current_budget()
        self.queries_at_start = budget.queries if budget is not None else None

    # --- section running ------------------------------------------------------------------

    def step(
        self,
        section: str,
        tool: str,
        call: Callable[[], _T | _Failure],
        *,
        tables: Sequence[str] = (),
    ) -> _T | None:
        """Run one reader, record what became of it, and return its value or ``None``.

        The signature is what makes this safe to compose: the callable may return the reader's
        success type *or* any failure shape, and the return type is the success type alone. A caller
        that checks for ``None`` has, by construction, a value the type checker knows is an answer -
        so an unsupported release cannot be read as though it were data.

        Every non-answer becomes a recorded status rather than an exception, so a release missing
        one metadata table costs the caller that section and not the whole analysis.

        The budget deserves its own note. All sections of one call draw on a single per-call
        allowance, so a later reader can exhaust what earlier ones left. When that happens the
        exception is caught here, every remaining section is recorded ``skipped_budget`` without
        being attempted - retrying would raise on its first statement - and the sections already
        gathered are returned. This is a deliberate difference from a granular tool, which returns a
        ``BudgetResult`` and nothing else: there, stopping early leaves no answer to keep, and here
        four sections of five is a materially better answer than none.
        """
        if self.budget_exhausted:
            self._record(section, tool, "skipped_budget", tables=tables)
            return None
        try:
            value = call()
        except BudgetExceeded as exc:
            self._on_budget(section, tool, exc, tables=tables)
            return None
        except Exception as exc:
            self._on_error(section, tool, exc, tables=tables)
            return None
        return self._classify(section, tool, value, tables=tables)

    def _classify(
        self, section: str, tool: str, value: _T | _Failure, *, tables: Sequence[str]
    ) -> _T | None:
        """Split a reader's return union into an answer or a recorded non-answer.

        A row is recorded on **every** path including success. It was previously written only on the
        failure paths, with :meth:`done` expected to add the success row - but ``done`` refines an
        existing row rather than creating one, so a fully successful analysis came back with an
        empty audit trail. Since the audit trail is the whole reason these tools can be trusted,
        that made the best case the least checkable one.
        """
        if isinstance(value, UnsupportedResult):
            self._record(section, tool, "unsupported", tables=tables, detail=value.detail)
            self.limit(
                section,
                f"not available on release {value.release}: {value.detail}",
                "unsupported_on_release",
            )
            return None
        if isinstance(value, ConnectorUnavailable):
            self._record(section, tool, "connector_required", tables=tables, detail=value.detail)
            self.limit(section, value.detail, "connector_not_configured")
            return None
        if isinstance(value, ObjectNotFound):
            self._record(section, tool, "empty", tables=tables, detail=value.detail)
            return None
        self._record(section, tool, "complete", tables=tables)
        return value

    def _on_budget(
        self, section: str, tool: str, exc: BudgetExceeded, *, tables: Sequence[str]
    ) -> None:
        self.budget_exhausted = True
        self._record(section, tool, "skipped_budget", tables=tables, detail=f"budget: {exc.reason}")
        self.limit(
            section,
            f"the per-call budget was exhausted after {exc.queries} statements, so this section "
            "and any after it were not read. Ask for this section on its own, or raise "
            "SAPBW_MAX_QUERIES_PER_CALL / SAPBW_MAX_SECONDS_PER_CALL.",
            "budget_exhausted",
        )

    def _on_error(self, section: str, tool: str, exc: Exception, *, tables: Sequence[str]) -> None:
        """One broken reader must not lose the others, so the failure becomes a recorded status."""
        self._record(section, tool, "failed", tables=tables, detail=type(exc).__name__)
        self.limit(
            section,
            f"this section could not be read ({type(exc).__name__}), so nothing here is a "
            "statement about it either way.",
            "reader_caveat",
        )

    def _record(
        self,
        section: str,
        tool: str,
        status: SectionStatus,
        *,
        tables: Sequence[str] = (),
        detail: str | None = None,
        record_count: int | None = None,
    ) -> None:
        self.steps.append(
            AnalysisStep(
                section=section,
                tool=tool,
                status=status,
                source_tables=[self.readers.providers.physical(t) for t in tables],
                record_count=record_count,
                detail=detail,
            )
        )

    def anchor(
        self,
        section: str,
        tool: str,
        call: Callable[[], _T | _AnchorFailure],
        *,
        tables: Sequence[str] = (),
    ) -> _T | _AnchorFailure:
        """Run the section the whole analysis rests on, and hand back the raw result.

        Unlike :meth:`step` this does **not** collapse a failure into ``None``. The anchor is the
        one section whose absence ends the analysis, and the caller has to tell the reasons apart: a
        release that cannot report the object type must surface as unsupported, not as "no such
        object". Those have different remedies, and reporting the first as the second sends someone
        hunting for a typo in a name that is spelled correctly.
        """
        try:
            value = call()
        except BudgetExceeded as exc:
            self._on_budget(section, tool, exc, tables=tables)
            raise
        except Exception as exc:
            self._on_error(section, tool, exc, tables=tables)
            raise
        if isinstance(value, UnsupportedResult):
            self._record(section, tool, "unsupported", tables=tables, detail=value.detail)
        elif isinstance(value, ObjectNotFound):
            self._record(section, tool, "empty", tables=tables, detail=value.detail)
        else:
            self._record(section, tool, "complete", tables=tables)
        return value

    def note_section(
        self,
        section: str,
        tool: str,
        status: SectionStatus,
        *,
        tables: Sequence[str] = (),
        detail: str | None = None,
    ) -> None:
        """Record an audit row for work done outside :meth:`step`.

        Used where the reader was already called to decide *which* analysis to run - resolving
        whether a name is a query or a provider, for instance - so the call cannot be wrapped.
        """
        self._record(section, tool, status, tables=tables, detail=detail)

    def done(self, section: str, *, count: int | None = None, detail: str | None = None) -> None:
        """Refine a section's row once its contribution is known.

        Only ever called after a successful :meth:`step`, so it refines rather than creates - and it
        never overwrites a failure status, because a section that read zero rows and a section that
        could not be read must not converge on the same row.
        """
        for row in reversed(self.steps):
            if row.section == section:
                if row.status == "complete":
                    row.status = "empty" if count == 0 else "complete"
                    row.record_count = count
                    if detail:
                        row.detail = detail
                return

    # --- accumulation ---------------------------------------------------------------------

    def limit(self, scope: str, limitation: str, reason: LimitationReason) -> None:
        entry = AnalysisLimitation(scope=scope, limitation=limitation, reason=reason)
        if entry not in self.limitations:
            self.limitations.append(entry)

    def absorb(self, scope: str, caveats: Sequence[str]) -> None:
        """Carry a constituent reader's own caveats up into the composed answer.

        Dropping them would be the classic composition bug: each reader states its scope limit
        honestly, and merging the results without them produces an answer more confident than any of
        its parts.
        """
        for caveat in caveats:
            self.limit(scope, caveat, _reason_for(caveat))

    def relate(
        self,
        target: list[RelatedObject],
        ref: BwObjectRef,
        relationship: Relationship,
        *,
        via: str | None = None,
        advisory: bool = False,
        evidence: Evidence | None = None,
        note: str | None = None,
    ) -> None:
        target.append(
            RelatedObject(
                ref=ref,
                relationship=relationship,
                via=via,
                advisory=advisory,
                evidence=evidence,
                note=note,
            )
        )
        if evidence is not None:
            self.evidence.append(evidence)

    def risk(
        self,
        kind: AnalysisKind,
        severity: Severity,
        title: str,
        recommendation: str,
        *,
        objects: Sequence[str] = (),
        evidence: Sequence[Provenance] = (),
        detail: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        self.risks.append(
            Finding(
                scenario=f"analysis.{kind}",
                severity=severity,
                title=title,
                affected_objects=list(objects),
                evidence=list(evidence),
                recommendation=recommendation,
                detail=detail,
                metrics=metrics or {},
            )
        )

    def say(self, sentence: str) -> None:
        if sentence not in self.summary:
            self.summary.append(sentence)

    def say_first(self, sentence: str) -> None:
        """Put the headline at the front, whichever section happened to produce it.

        Sections speak as they run, so the sentence that answers the question asked can otherwise
        end up below incidental detail gathered on the way to it.
        """
        if sentence in self.summary:
            self.summary.remove(sentence)
        self.summary.insert(0, sentence)

    # --- assembly -------------------------------------------------------------------------

    def confidence(self) -> AnalysisConfidence:
        """Coverage and basis as separate components, never one number. See the model docstring."""
        complete = sum(1 for s in self.steps if s.status in ("complete", "empty"))
        unsupported = sum(1 for s in self.steps if s.status == "unsupported")
        failed = sum(1 for s in self.steps if s.status == "failed")
        skipped = sum(1 for s in self.steps if s.status == "skipped_budget")
        applicable = [s for s in self.steps if s.status != "not_applicable"]
        advisory = sum(1 for item in (*self.dependencies, *self.consumers) if item.advisory)
        summary: EvidenceSummary | None = summarise(self.evidence) if self.evidence else None

        reasons: list[str] = []
        if unsupported:
            reasons.append(
                f"{unsupported} section(s) are unavailable on release "
                f"{self.readers.capability.bw_release}, so this answer is narrower than the same "
                "question would be on a release that carries them."
            )
        if failed:
            reasons.append(f"{failed} section(s) could not be read at all.")
        if skipped:
            reasons.append(
                f"{skipped} section(s) were not read because the per-call budget ran out."
            )
        if advisory:
            reasons.append(
                f"{advisory} relationship(s) were derived rather than declared - parsed from ABAP "
                "routine source, or resolved from a generated table name by convention - and are a "
                "lower bound, not a complete set."
            )
        if not reasons:
            reasons.append(
                "every applicable section was read from metadata, and every relationship reported "
                "is a declared one."
            )

        # Coverage decides the level, because a missing section is the failure a caller can act on.
        # An advisory relationship caps it at medium rather than lowering it further: the declared
        # facts are still declared, and only the derived edges are a lower bound.
        gaps = unsupported + failed + skipped
        total = len(applicable) or 1
        if gaps == 0:
            level: Any = "medium" if advisory else "high"
        elif gaps * 2 >= total:
            level = "low"
        else:
            level = "medium"
        return AnalysisConfidence(
            level=level,
            sections_total=len(applicable),
            sections_complete=complete,
            sections_unsupported=unsupported,
            sections_failed=failed,
            sections_skipped=skipped,
            evidence=summary,
            advisory_relationships=advisory,
            reasons=reasons,
        )

    # --- the contracted envelope fields, all derived from what already ran ------------------

    def execution(self) -> AnalysisExecution:
        """What ran, what it cost, how long it took. Every value measured here, none declared."""
        budget = current_budget()
        spent: int | None = None
        if budget is not None and self.queries_at_start is not None:
            spent = max(budget.queries - self.queries_at_start, 0)
        tools: dict[str, None] = {}
        for row in self.steps:
            tools[row.tool] = None
        return AnalysisExecution(
            tools_used=sorted(tools),
            step_count=len(self.steps),
            sections_answered=sum(1 for s in self.steps if s.status in ("complete", "empty")),
            queries_executed=spent,
            duration_ms=int((time.monotonic() - self.started_at) * 1000),
        )

    def budget_report(self) -> AnalysisBudget:
        """The allowance and the spend, reported on success as well as on exhaustion."""
        budget = current_budget()
        if budget is None:
            return AnalysisBudget(measured=False, completeness=self._budget_completeness())
        snapshot = budget.snapshot()
        used = (
            max(budget.queries - self.queries_at_start, 0)
            if self.queries_at_start is not None
            else int(snapshot["queries"])
        )
        return AnalysisBudget(
            query_limit=int(snapshot["max_queries"]) or None,
            queries_used=used,
            time_limit_ms=int(float(snapshot["max_seconds"]) * 1000) or None,
            time_used_ms=int((time.monotonic() - self.started_at) * 1000),
            completeness=self._budget_completeness(),
            measured=True,
        )

    def _budget_completeness(self) -> Completeness:
        """Which of the two causes stopped the analysis short.

        The bool merged them, and they need opposite responses: a per-call budget stop is fixed by
        raising the allowance, a section's own row cap by narrowing the request. Both can hold (D6).
        """
        return Completeness(
            bounds=[
                *([BoundHit(bound="query_budget")] if self.budget_exhausted else []),
                *(
                    [BoundHit(bound="row_cap", scope="analysis_sections")]
                    if self._any_truncated()
                    else []
                ),
            ]
        )

    def _any_truncated(self) -> bool:
        """True when a section's own cap stopped it short, not only the per-call budget."""
        return any(item.reason == "truncated" for item in self.limitations)

    def skipped(self) -> list[SkippedSection]:
        """Sections that owed an answer and did not deliver one. See :class:`SkippedSection`."""
        owed = {"unsupported", "connector_required", "skipped_budget", "failed"}
        return [
            SkippedSection(
                section=row.section,
                tool=row.tool,
                status=row.status,  # type: ignore[arg-type]  # narrowed by `owed`
                reason=row.detail or _SKIP_REASONS[row.status],
            )
            for row in self.steps
            if row.status in owed
        ]

    def distinct_evidence(self) -> list[Evidence]:
        """One entry per (basis, method), so the list says how without repeating itself."""
        best: dict[tuple[str, str], Evidence] = {}
        for item in self.evidence:
            best.setdefault((item.basis, item.method), item)
        return list(best.values())

    def findings(self) -> list[AnalysisFinding]:
        """The structured form of the summary: mechanical, derived, never authored per analysis.

        Two sources, both already justified by the envelope: the normalised relationship lists, and
        every section that reported a countable contribution. Nothing is inferred beyond what the
        section that produced it already established.
        """
        found: list[AnalysisFinding] = []
        for label, items in (("feed", self.dependencies), ("depend on", self.consumers)):
            if not items:
                continue
            advisory = [i for i in items if i.advisory]
            weakest = min(
                (i.evidence for i in items if i.evidence is not None),
                key=lambda e: e.rank,
                default=None,
            )
            declared = len(items) - len(advisory)
            detail = (
                f"{len(items)} object(s) {label} {self.subject_label}"
                f" ({declared} declared, {len(advisory)} derived)"
                if advisory
                else f"{len(items)} object(s) {label} {self.subject_label}, all declared"
            )
            found.append(
                AnalysisFinding(
                    statement=detail,
                    section=RELATIONSHIP_SECTION,
                    basis=("inferred" if advisory else (weakest.basis if weakest else "observed")),
                    completeness="lower_bound" if advisory else "complete",
                    objects=[i.id for i in items][:_FINDING_OBJECTS],
                    evidence=weakest,
                )
            )

        for row in self.steps:
            if row.record_count is None or row.status not in ("complete", "empty"):
                continue
            statement = (
                f"{row.section}: no records found"
                if row.record_count == 0
                else f"{row.section}: {row.record_count} record(s)"
            )
            found.append(
                AnalysisFinding(
                    statement=statement,
                    section=row.section,
                    # A count read straight from metadata rows is observed; a section that read no
                    # table computed it, so it is derived.
                    basis="observed" if row.source_tables else "derived",
                    completeness="lower_bound" if self.budget_exhausted else "complete",
                )
            )
        return found

    def build(
        self,
        *,
        kind: AnalysisKind,
        subject_name: str,
        title: str,
        subject: BwObjectRef | None = None,
        next_actions: Sequence[NextAction] = (),
        **payloads: Any,
    ) -> Analysis:
        self.subject_label = subject_name
        status: ValidationStatus = "not_validated"
        basis = (
            "the tool that produced this answer was not recorded, so how far its readers have been "
            "proven could not be established."
        )
        if self.tool_name:
            status, basis = validation_for_tool(self.tool_name)
        return Analysis(
            kind=kind,
            system=self.readers.system,
            subject=subject,
            subject_name=subject_name,
            title=title,
            summary=self.summary,
            dependencies=self.dependencies,
            consumers=self.consumers,
            risks=self.risks,
            findings=self.findings(),
            evidence=self.distinct_evidence(),
            limitations=self.limitations,
            next_actions=list(next_actions),
            steps=self.steps,
            execution=self.execution(),
            budget=self.budget_report(),
            sections_skipped=self.skipped(),
            validation_status=status,
            validation_basis=basis,
            confidence=self.confidence(),
            stopped_on_budget=self.budget_exhausted,
            **payloads,
        )


_HEURISTIC_MARKERS = ("advisory", "heuristic", "lower bound", "lower-bound", "parsed from")
_TRUNCATION_MARKERS = ("truncated", "summarised", "row cap", "capped", "showing")
_DEAD_END_MARKERS = ("customer-exit", "customer exit", "dead end", "resolve at runtime")


def _reason_for(caveat: str) -> LimitationReason:
    """Classify a reader's free-text caveat, so a caller can filter without parsing sentences.

    Falls back to ``reader_caveat`` rather than guessing: an unrecognised caveat is still carried
    up verbatim, and mis-labelling its reason would be worse than admitting it is unclassified.
    """
    lowered = caveat.lower()
    if any(marker in lowered for marker in _DEAD_END_MARKERS):
        return "metadata_dead_end"
    if any(marker in lowered for marker in _HEURISTIC_MARKERS):
        return "heuristic_lower_bound"
    if any(marker in lowered for marker in _TRUNCATION_MARKERS):
        return "truncated"
    return "reader_caveat"


class AnalysisService:
    """Builds the five composed answers. One method per compound tool."""

    def __init__(self, readers: AnalysisReaders) -> None:
        self._r = readers

    # --- 1. an object ---------------------------------------------------------------------

    def analyze_object(
        self, name: str, *, depth: int = 2
    ) -> Analysis | ObjectNotFound | UnsupportedResult:
        """What this object is, what feeds it, what depends on it, and whether it is healthy."""
        run = _Run(self._r, tool_name="bw_analyze_object")
        definition = run.anchor(
            "definition",
            "bw_describe_object",
            lambda: self._r.providers.describe(name),
            tables=("dso_header",),
        )
        if not isinstance(definition, Provider):
            return definition
        run.done("definition", count=len(definition.fields))
        run.absorb("definition", definition.caveats)
        subject = definition.ref or BwObjectRef(object_type="unknown", name=definition.name)
        run.say(
            f"{definition.name} is a {definition.object_type} in info area "
            f"{definition.info_area or 'unknown'} with {len(definition.fields)} field(s), "
            f"{'active' if definition.active else 'not active'}."
        )
        if definition.description and definition.description.origin == "generated":
            run.limit(
                "definition",
                "the description is generated by this server from surrounding metadata; BW holds "
                "no usable one for this object.",
                "reader_caveat",
            )

        # Upstream only. The downstream half is walked by `_add_impact` just below, which also adds
        # the routine-derived consumers, so asking for `both` here would traverse every downstream
        # hop twice. Measured on a real DSO: 21s of a 31s analysis was this one call, and the impact
        # walk that followed cost 2.4s because it reused the same service's memos.
        # `Analysis.lineage` therefore holds the upstream graph; the downstream graph is
        # `Analysis.impact.graph`. Both are returned, and no hop is paid for twice.
        graph = self._add_lineage(run, name, depth=depth, direction="upstream")
        impact = self._add_impact(run, name, depth=depth)
        health = self._add_health(run, definition)
        load = self._add_loading_chains(run, name)
        self._add_query_consumers(run, name)
        self._add_calcview_consumers(run, name)
        self._object_risks(run, definition, health=health, load=load, impact=impact)

        return run.build(
            kind="object",
            subject=subject,
            subject_name=definition.name,
            title=f"Object analysis: {definition.name}",
            definition=definition,
            health=health,
            load=load,
            lineage=graph,
            impact=impact,
            next_actions=self._object_actions(definition, health=health, load=load),
        )

    # --- 2. a BEx query -------------------------------------------------------------------

    def analyze_query(self, query: str) -> Analysis | ObjectNotFound | UnsupportedResult:
        """What this report reads, who sees different numbers, and when its data is current."""
        run = _Run(self._r, tool_name="bw_analyze_query")
        definition = run.anchor(
            "definition",
            "bw_get_query",
            lambda: self._r.queries.get_query(query),
            tables=("query_dir", "element_text"),
        )
        if isinstance(definition, UnsupportedResult):
            return definition
        # A query the reader could not find comes back as an empty shell rather than as not-found,
        # so the absent COMPID is what distinguishes "no such query" here.
        if not isinstance(definition, Query) or not definition.compid:
            return ObjectNotFound(name=query, detail="no BEx query matches this name or COMPUID")
        run.done("definition", count=len(definition.elements))
        run.absorb("definition", definition.caveats)
        subject = BwObjectRef(object_type="query", name=definition.compid)
        run.say(
            f"{definition.compid} ({definition.description or 'no description'}) reads "
            f"{', '.join(definition.providers) or 'no resolved provider'} and holds "
            f"{len(definition.elements)} element(s) and {len(definition.variables)} variable(s)."
        )
        if definition.conditions:
            # Split on/off in the narrative rather than giving a single count: "4 conditions" reads
            # as "this report is filtered" when all four are switched off (D26).
            live = sum(1 for c in definition.conditions if c.active)
            run.say(
                f"It defines {len(definition.conditions)} condition(s) or exception(s), "
                f"{live} of them active."
            )

        lineage = self._add_query_lineage(run, query)
        usage = self._add_query_usage(run, query)
        security = self._add_query_security(run, query)
        providers = list(lineage.providers) if lineage else list(definition.providers)
        load = self._add_query_currency(run, providers)
        self._query_risks(run, definition, lineage=lineage, usage=usage, security=security)

        return run.build(
            kind="query",
            subject=subject,
            subject_name=definition.compid,
            title=f"Query analysis: {definition.compid}",
            query=definition,
            query_lineage=lineage,
            query_usage=usage,
            security=security,
            load=load,
            next_actions=self._query_actions(definition, usage=usage, security=security),
        )

    # --- 3. a process chain ---------------------------------------------------------------

    def analyze_process_chain(
        self, chain_id: str, *, days: int = 90
    ) -> Analysis | ObjectNotFound | UnsupportedResult:
        """What this chain does, what it loads, how reliably it runs, and where it is fragile."""
        run = _Run(self._r, tool_name="bw_analyze_process_chain")
        chain = run.anchor(
            "structure",
            "bw_get_chain",
            lambda: self._r.chains.get_chain(chain_id),
            tables=("chain_attr", "chain_edges"),
        )
        if not isinstance(chain, Chain):
            return chain
        # The reader returns a shell rather than a not-found for an unknown id, so a chain with
        # neither steps nor a description is the closest thing to evidence that the id is wrong. The
        # detail names the ambiguity instead of asserting the chain does not exist.
        if not chain.processes and not chain.description:
            return ObjectNotFound(
                name=chain_id,
                detail="no process chain with this id, or a chain carrying neither steps nor a "
                "description - check the id with bw_list_chains",
            )
        run.done("structure", count=len(chain.processes))
        subject = BwObjectRef(object_type="chain", name=chain.chain_id)
        run.say(
            f"{chain.chain_id} ({chain.description or 'no description'}) has "
            f"{len(chain.processes)} process(es) and {len(chain.subchain_ids)} sub-chain(s), "
            f"{'active' if chain.active else 'not active'}."
        )
        if chain.truncated_recursion:
            run.limit(
                "structure",
                "sub-chain resolution hit its depth limit, so some nested steps are not included "
                "and what this chain loads is a lower bound.",
                "truncated",
            )

        cadence = self._add_cadence(run, chain_id)
        runtimes = self._add_runtimes(run, chain_id, days=days)
        load = self._add_chain_loads(run, chain_id)
        self._chain_risks(run, chain, runtimes=runtimes, load=load)

        return run.build(
            kind="process_chain",
            subject=subject,
            subject_name=chain.chain_id,
            title=f"Process chain analysis: {chain.chain_id}",
            chain=chain,
            cadence=cadence,
            runtimes=runtimes,
            load=load,
            next_actions=self._chain_actions(chain, runtimes=runtimes),
        )

    # --- 4. a proposed change -------------------------------------------------------------

    def assess_change_impact(
        self, name: str, *, depth: int = 3
    ) -> Analysis | ObjectNotFound | UnsupportedResult:
        """Everything a change to this object reaches, and what to verify before transporting it."""
        run = _Run(self._r, tool_name="bw_assess_change_impact")
        # Described first and outside `step`, because a change subject need not be a provider - it
        # may be a transformation or a chain - and failing to describe it must not end the analysis.
        described = self._r.providers.describe(name)
        definition = described if isinstance(described, Provider) else None
        run.note_section(
            "definition",
            "bw_describe_object",
            "complete" if definition else "empty",
            tables=("dso_header",),
            detail=None if definition else "the subject is not a provider or InfoObject",
        )
        subject = (
            definition.ref
            if definition and definition.ref
            else BwObjectRef(object_type="unknown", name=name)
        )
        if definition is not None:
            run.absorb("definition", definition.caveats)

        impact = self._add_impact(run, name, depth=depth)
        trace = run.step(
            "upstream",
            "bw_trace_to_source",
            lambda: self._r.lineage.trace_to_source(name, depth=max(depth, 4)),
            tables=("transformation",),
        )
        if trace is not None:
            run.done("upstream", count=len(trace.datasources_reached))
            run.absorb("upstream", trace.caveats)
            for datasource in trace.datasources_reached:
                run.relate(
                    run.dependencies,
                    BwObjectRef(object_type="datasource", name=datasource),
                    "source_datasource",
                    note="reached by walking upstream; re-initialisation may be needed",
                )
            if trace.unresolved_boundaries:
                run.limit(
                    "upstream",
                    f"{len(trace.unresolved_boundaries)} upstream path(s) did not reach a "
                    "DataSource, so the full set of feeding sources is a lower bound.",
                    "heuristic_lower_bound",
                )

        load = self._add_loading_chains(run, name)
        self._add_query_consumers(run, name)
        self._add_calcview_consumers(run, name)

        advisory = sum(1 for c in run.consumers if c.advisory)
        run.say_first(
            f"A change to {name} reaches {len(run.consumers)} downstream object(s): "
            f"{len(run.consumers) - advisory} declared and {advisory} found by parsing ABAP or by "
            "resolving a generated table name."
        )
        self._change_risks(run, name, impact=impact, load=load)

        return run.build(
            kind="change_impact",
            subject=subject,
            subject_name=name,
            title=f"Change impact: {name}",
            definition=definition,
            impact=impact,
            trace=trace,
            load=load,
            next_actions=self._change_actions(run, name, load=load),
        )

    # --- 5. missing or wrong data ---------------------------------------------------------

    def troubleshoot_missing_data(
        self, target: str
    ) -> Analysis | ObjectNotFound | UnsupportedResult:
        """Walk the layers above a wrong-looking number and report what each one says.

        Ordered the way an analyst actually diagnoses: establish what the object reads, then ask
        whether the data arrived (the request ledger), then whether the load that should have
        delivered it ran (chain history), and only then look at the logic. Most wrong-data incidents
        are answered by the second or third question, and reading routines first wastes the call.
        """
        run = _Run(self._r, tool_name="bw_troubleshoot_missing_data")
        # Resolved outside `step` because the answer decides which sequence of sections to run: a
        # query is diagnosed through its providers, a provider through its own upstream.
        as_query = self._r.queries.get_query(target)
        query = as_query if isinstance(as_query, Query) and as_query.compid else None
        is_query = query is not None
        run.note_section(
            "subject",
            "bw_get_query" if is_query else "bw_describe_object",
            "complete",
            tables=("query_dir",),
            detail="the target is a BEx query"
            if is_query
            else "no query matched the name, so the target is treated as a provider",
        )

        providers: list[str] = []
        lineage: QueryLineage | None = None
        if query is not None:
            lineage = self._add_query_lineage(run, target)
            providers = list(lineage.providers) if lineage else list(query.providers)
            run.say(
                f"{query.compid} is a BEx query reading "
                f"{', '.join(providers) or 'no resolved provider'}."
            )
        else:
            providers = [target]
            self._add_lineage(run, target, depth=2, direction="upstream")
            run.say(f"{target} is treated as a provider; its feeding objects were traced upstream.")

        # `union_members` records which parts came from which union, so the parts can afterwards be
        # compared *against each other* (D33). Diagnosing each correctly and in isolation is not
        # enough: a union holding one part loaded today and another frozen in 2018 produced two
        # accurate findings and no statement that they disagreed, which is what "reported as
        # homogeneous" means.
        providers, union_members = self._expand_union_providers(run, providers)
        checked = 0
        health_by_provider: dict[str, ProviderHealth] = {}
        for provider in providers[:_DIAGNOSE_CAP]:
            checked += 1
            diagnosed = self._diagnose_provider(run, provider)
            if diagnosed is not None:
                health_by_provider[provider] = diagnosed
        self._mixed_currency_risks(run, union_members, health_by_provider)
        if len(providers) > _DIAGNOSE_CAP:
            run.limit(
                "currency",
                f"{len(providers)} providers feed this target; the first {_DIAGNOSE_CAP} were "
                "diagnosed. Ask about a specific provider to check the rest.",
                "truncated",
            )
        if checked == 0:
            run.limit(
                "currency",
                "no feeding provider resolved, so no layer could be checked for a load failure. "
                "This is the first thing to fix: without a provider the data path is unknown.",
                "metadata_dead_end",
            )

        self._add_suspect_logic(run, providers[:3])
        if query is not None:
            security = self._add_query_security(run, target)
            if security is not None and security.user_specific_result:
                restricted = (
                    ", ".join(security.auth_relevant_characteristics[:5])
                    or "the restricted characteristics"
                )
                run.risk(
                    "missing_data",
                    "high",
                    "The report returns different rows per user, which explains missing data with "
                    "no load fault at all",
                    f"Compare the two users' analysis authorisations on {restricted} before "
                    "investigating the dataflow.",
                    objects=[target],
                    evidence=list(_as_list(security.provenance)),
                    detail="Two people reading one report can both be right when it is restricted "
                    "on an authorisation-relevant characteristic.",
                )
        else:
            security = None

        if not run.risks:
            run.say(
                "No load failure, stale request or restricted characteristic was found in the "
                "layers checked, so the cause is more likely in transformation logic or in the "
                "source data than in the load."
            )

        return run.build(
            kind="missing_data",
            subject=BwObjectRef(object_type="query" if query else "unknown", name=target),
            subject_name=target,
            title=f"Missing-data diagnostic: {target}",
            query=query,
            query_lineage=lineage,
            security=security,
            next_actions=self._missing_data_actions(run, target, providers=providers),
        )

    # --- shared sections ------------------------------------------------------------------

    def _add_lineage(
        self, run: _Run, name: str, *, depth: int, direction: str = "both"
    ) -> LineageGraph | None:
        graph = run.step(
            "lineage",
            "bw_get_lineage",
            lambda: self._r.lineage.get_lineage(name, direction=direction, depth=depth),  # type: ignore[arg-type]
            tables=("transformation", "dtp"),
        )
        if graph is None:
            return None
        run.done("lineage", count=graph.node_count)
        run.absorb("lineage", graph.caveats)
        root = graph.root_id
        for edge in graph.edges:
            advisory = edge.confidence == "advisory"
            if edge.src == root and edge.dst == root:
                # A self-transformation: the object reads itself to write itself. Both branches
                # below exclude it by construction, so it was silently absent from an object's own
                # dependency list even though BW declares the transformation and the worksheet
                # ground truth lists it as an inbound source. It is also the shape behind the
                # stale-lookup and destructive-re-run hazards, so it is the last thing to drop.
                run.relate(
                    run.dependencies,
                    _ref_for(graph, edge.src),
                    "upstream",
                    via=edge.transformation_id,
                    advisory=advisory,
                    evidence=edge.evidence,
                    note=edge.note
                    or "self-transformation: this object is its own source, so a re-run reads what "
                    "the previous run wrote",
                )
            elif edge.dst == root and edge.src != root:
                run.relate(
                    run.dependencies,
                    _ref_for(graph, edge.src),
                    "upstream",
                    via=edge.transformation_id,
                    advisory=advisory,
                    evidence=edge.evidence,
                    note=edge.note,
                )
            elif edge.src == root and edge.dst != root:
                run.relate(
                    run.consumers,
                    _ref_for(graph, edge.dst),
                    "downstream",
                    via=edge.transformation_id,
                    advisory=advisory,
                    evidence=edge.evidence,
                    note=edge.note,
                )
        if graph.truncated:
            run.limit(
                "lineage",
                "the lineage graph hit its node cap, so the dependency and consumer lists are a "
                "lower bound rather than the complete set.",
                "truncated",
            )
        return graph

    def _add_impact(self, run: _Run, name: str, *, depth: int) -> ImpactAnalysis | None:
        impact = run.step(
            "downstream",
            "bw_impact_analysis",
            lambda: self._r.lineage.impact_analysis(name, depth=depth),
            tables=("transformation", "routine_source"),
        )
        if impact is None:
            return None
        run.done("downstream", count=impact.affected_object_count)
        run.absorb("downstream", impact.caveats)
        root = impact.graph.root_id
        for node in impact.graph.nodes:
            if node.id == root:
                continue
            run.relate(
                run.consumers,
                _node_ref(node),
                "downstream",
                note="reached by walking declared transformations downstream",
                # The note already claims these were reached through *declared* transformations, so
                # the evidence has to say the same thing. Left unset, the strongest half of the
                # consumer list arrived carrying no basis at all while the weakest half did.
                evidence=evidence_for("lineage_edge", "exact"),
            )
        for consumer in impact.declared_lookup_consumers:
            run.relate(
                run.consumers,
                _ref_for(impact.graph, consumer),
                "consumer_lookup",
                # Not advisory, and that is the whole point of the separate relationship: BW records
                # this read in the transformation's rule metadata and lists it under its own
                # where-used. Filing it beside the ABAP-parsed consumers would understate it.
                advisory=False,
                evidence=evidence_for("lineage_edge", "declared_lookup"),
                note="a transformation declares a lookup of this object in its rule metadata, so a "
                "change here changes that transformation's result even though this object is not "
                "its source",
            )
        for consumer in impact.routine_lookup_consumers:
            run.relate(
                run.consumers,
                # Typed from the graph the walk already built, not hardcoded to unknown. The impact
                # graph holds each routine consumer as a node with its real type, so asserting
                # "unknown" discarded an answer that was already in hand - and on a production ADSO
                # that was every one of the four routine consumers, rendered as untyped objects.
                _ref_for(impact.graph, consumer),
                "consumer_routine",
                advisory=True,
                # S01 requires exactly this on a routine-derived consumer: inferred basis,
                # routine_select_parse method, lower_bound completeness. It was omitted entirely, so
                # the items whose uncertainty matters most were the only ones with no evidence, and
                # they contributed nothing to the answer's own evidence summary.
                evidence=evidence_for("lineage_edge", "advisory"),
                note="a routine on this object reads the subject; invisible to BW's where-used "
                "list, and found by parsing ABAP, so this set is a lower bound",
            )
        if impact.routine_lookup_consumers:
            run.limit(
                "downstream",
                f"{len(impact.routine_lookup_consumers)} consumer(s) were found by parsing ABAP "
                "routine source. Dynamic SQL, function-module calls and class methods are not "
                "followed, so the true set can only be larger.",
                "heuristic_lower_bound",
            )
        return impact

    def _add_health(self, run: _Run, definition: Provider) -> ProviderHealth | None:
        gate = self._r.health.require_health()
        if gate is not None:
            run.step("health", "bw_get_provider_health", lambda: gate, tables=("request_status",))
            return None
        health = run.step(
            "health",
            "bw_get_provider_health",
            lambda: self._r.health.get_health(definition.name, definition.object_type),
            tables=("request_status", "cs_tables"),
        )
        if health is None:
            return None
        run.done("health", count=health.tables_found)
        run.absorb("health", health.caveats)
        if health.unloaded:
            run.say(f"{definition.name} has generated tables but holds no rows.")
        elif health.volume_resolved:
            run.say(
                f"It holds {health.active_records:,} active row(s)"
                + (
                    f" and {health.changelog_records:,} changelog row(s)"
                    if health.changelog_records
                    else ""
                )
                + "."
            )
        return health

    def _add_loading_chains(self, run: _Run, name: str) -> LoadClosure | None:
        load = run.step(
            "loading_chains",
            "bw_get_load_closure",
            lambda: self._r.load_closure.provider_to_chains(name),
            tables=("chain_edges", "dtp"),
        )
        if load is None:
            return None
        # `detail` names the provider because this section now runs once per part of a union (D58).
        # Three rows reading "loading_chains: 2 record(s)" with nothing to tell them apart is not a
        # readable answer, and the currency rows beside them already carry the provider.
        run.done("loading_chains", count=len(load.loading_chains), detail=name)
        run.absorb("loading_chains", load.caveats)
        for cadence in load.loading_chains:
            # Not advisory: that the chain loads this provider is declared on the DTP. Only its
            # *cadence* is uncertain when the log window holds too few runs, and marking the whole
            # relationship advisory would overstate that - it would read as though the link itself
            # had been guessed. The uncertainty goes to the note and to a limitation instead.
            run.relate(
                run.dependencies,
                BwObjectRef(object_type="chain", name=cadence.chain_id),
                "loading_chain",
                evidence=cadence.evidence,
                note=f"observed cadence {cadence.frequency}"
                + (" (low confidence: too few runs)" if cadence.confidence == "low" else ""),
            )
        unmeasured = [c.chain_id for c in load.loading_chains if c.confidence == "low"]
        if unmeasured:
            run.limit(
                "loading_chains",
                f"the cadence of {', '.join(unmeasured[:_NAMED_CHAINS])} was classified from too "
                "few runs in the retained log window to measure an interval, so when this object's "
                "data is current is a reading of a small sample rather than an observed schedule.",
                "heuristic_lower_bound",
            )
        if load.loading_chains:
            named = load.loading_chains[:_NAMED_CHAINS]
            # Named rather than "It is loaded by", because a union's parts each get one of these
            # sentences (D58) and three consecutive subjectless ones leave the reader unable to tell
            # which chain loads which part - which is the whole point when one part is stale and the
            # others are current.
            run.say(
                f"{name} is loaded by "
                + ", ".join(f"{c.chain_id} ({c.frequency})" for c in named)
                + ("." if len(load.loading_chains) <= _NAMED_CHAINS else ", and others.")
            )
        return load

    def _add_query_consumers(self, run: _Run, name: str) -> None:
        result = run.step(
            "consumer_queries",
            "bw_list_queries",
            lambda: self._r.queries.list_queries(provider=name, limit=_CONSUMER_CAP),
            tables=("query_dir", "query_provider"),
        )
        if result is None:
            return
        items, total = result
        run.done("consumer_queries", count=total)
        for item in items:
            run.relate(
                run.consumers,
                BwObjectRef(object_type="query", name=item.compid or item.compuid),
                "consumer_query",
                # RSZCOMPIC *declares* the query against this provider, so this is observed. It is
                # deliberately the declared-assignment vocabulary rather than the generic one: the
                # same conclusion reached by noticing a generated view touching a table is a weaker
                # claim, and the two must not read alike.
                evidence=evidence_for("declared_query_provider", "rszcompic"),
                note=f"{item.origin} query" + (f", owner {item.owner}" if item.owner else ""),
            )
        if total > len(items):
            run.limit(
                "consumer_queries",
                f"{total} queries read this object; the first {len(items)} are listed.",
                "truncated",
            )

    def _add_calcview_consumers(self, run: _Run, name: str) -> None:
        report = run.step(
            "consumer_calcviews",
            "bw_get_hana_crossings",
            lambda: self._r.hana.get_hana_crossings(limit=_CROSSING_CAP),
            tables=("object_dependencies",),
        )
        if report is None:
            return
        wanted = name.strip().upper()
        matched = [
            crossing
            for crossing in report.crossings
            if crossing.direction == "hana_reads_bw"
            and (crossing.bw_object_resolved or crossing.bw_object or "").strip().upper() == wanted
        ]
        run.done("consumer_calcviews", count=len(matched))
        for crossing in matched:
            run.relate(
                run.consumers,
                BwObjectRef(object_type="calcview", name=crossing.hana_object),
                "consumer_calcview",
                advisory=crossing.resolution == "bic_table",
                evidence=crossing.evidence,
                note="a calc view reads this object's generated table; a change here changes the "
                "view's result with no BW where-used warning",
            )
        if report.truncated:
            run.limit(
                "consumer_calcviews",
                f"the crossing scan stopped at {_CROSSING_CAP} rows, so a calc view beyond that is "
                "not listed here.",
                "truncated",
            )

    def _add_query_lineage(self, run: _Run, query: str) -> QueryLineage | None:
        lineage = run.step(
            "field_lineage",
            "bw_get_query_lineage",
            lambda: self._r.queries.get_query_lineage(query),
            tables=("element_xref", "transformation"),
        )
        if lineage is None:
            return None
        run.done("field_lineage", count=len(lineage.paths))
        run.absorb("field_lineage", lineage.caveats)
        for provider in lineage.providers:
            run.relate(
                run.dependencies,
                BwObjectRef(object_type="unknown", name=provider),
                "upstream",
                note="the provider this query reads",
            )
        unresolved = [p for p in lineage.paths if p.resolution != "field"]
        if unresolved:
            run.limit(
                "field_lineage",
                f"{len(unresolved)} of {len(lineage.paths)} field path(s) did not resolve to a "
                "field's own derivation; they fall back to the provider's upstream objects, which "
                "must not be read as field lineage.",
                "heuristic_lower_bound",
            )
        if lineage.customer_exit_variables:
            run.limit(
                "field_lineage",
                f"{len(lineage.customer_exit_variables)} customer-exit variable(s) resolve in ABAP "
                "at runtime: they can be named but their values are a metadata dead end.",
                "metadata_dead_end",
            )
        return lineage

    def _add_query_usage(self, run: _Run, query: str) -> QueryUsage | None:
        usage = run.step(
            "usage",
            "bw_get_query_usage",
            lambda: self._r.queries.get_query_usage(query),
            tables=("query_dir",),
        )
        if usage is None:
            return None
        run.done("usage", detail=usage.reason)
        return usage

    def _add_query_security(self, run: _Run, query: str) -> QueryAuthExposure | None:
        exposure = run.step(
            "security",
            "bw_get_query_auth_exposure",
            lambda: self._r.query_auth_exposure(query),
            # Plural. The singular form is not a capability key, so `physical()` fell through to its
            # fallback and the audit row cited "auth_value" - a table that exists on no BW
            # system (D60). A citation naming a non-table is worse than none, because it reads
            # as authoritative; the guard in tests/test_section_tables.py now makes that impossible.
            tables=("auth_values",),
        )
        if exposure is None:
            return None
        run.done("security", count=len(exposure.auth_relevant_characteristics))
        run.absorb("security", exposure.caveats)
        return exposure

    def _add_query_currency(self, run: _Run, providers: Sequence[str]) -> LoadClosure | None:
        """When the report's data is current, via the chains loading its first provider.

        First provider only, deliberately: a report on a MultiProvider can read a dozen, and the
        answer to "is this current" is governed by the one that loads last. Resolving all of them
        would multiply the reads for a question the caller can ask per provider.
        """
        if not providers:
            run.note_section(
                "loading_chains",
                "bw_get_load_closure",
                "not_applicable",
                detail="no provider resolved for this query, so no loading chain can be found",
            )
            return None
        return self._add_loading_chains(run, providers[0])

    def _add_cadence(self, run: _Run, chain_id: str) -> ChainCadence | None:
        """How often this chain actually runs, and on what basis (D52).

        The reader already computed this - ``bw_list_chains`` returns a frequency per chain - and
        the analysis simply never carried it, so the tool S03 nominates reported no cadence at all
        while the scenario names it as required evidence. Plumbing, not a missing capability.

        The basis is what makes it worth reporting rather than a bare label. ``ChainCadence`` has
        ``evidence`` derived from its own confidence: ``derived``/``observed_run_history`` when
        there are enough runs to mean something, ``inferred``/``sparse_run_history`` when not. A
        without that distinction invites "daily" from two runs to be read exactly like "daily" from
        eighty-eight.

        And it is never read from the name. The chain that exposed this is called "... 6 AM CST" and
        actually starts at 05:30 - the name reaches the answer only as the chain's description.
        """
        cadences = run.step(
            "cadence",
            "bw_list_chains",
            lambda: self._r.chains.get_cadence([chain_id]),
            tables=("log_chain",),
        )
        if not cadences:
            return None
        cadence = cadences.get(chain_id)
        if cadence is None:
            run.note_section(
                "cadence",
                "bw_list_chains",
                "empty",
                tables=("log_chain",),
                detail="no run history for this chain, so no cadence could be observed",
            )
            return None
        run.done("cadence", count=cadence.run_count)
        if cadence.note:
            run.absorb("cadence", [cadence.note])
        basis = cadence.evidence.basis if cadence.evidence else "unstated"
        run.say(
            f"It runs {cadence.frequency} ({basis} from {cadence.run_count} run(s) over "
            f"{cadence.run_days} day(s))"
            + (", and has not run within the window for that cadence." if cadence.active is False
               else ".")
        )
        return cadence

    def _add_runtimes(self, run: _Run, chain_id: str, *, days: int) -> ChainRuntimes | None:
        runtimes = run.step(
            "runtimes",
            "bw_get_chain_runtimes",
            lambda: self._r.chains.get_chain_runtimes(chain_id, days=days),
            tables=("log_chain", "process_log"),
        )
        if runtimes is None:
            return None
        run.done("runtimes", count=runtimes.total_runs)
        run.absorb("runtimes", runtimes.caveats)
        if runtimes.total_runs:
            rate = (
                f"{runtimes.success_rate:.0%}"
                if runtimes.success_rate is not None
                else "an unmeasured"
            )
            p95 = runtimes.duration_seconds.p95_s
            duration = f", p95 {p95:.0f}s" if p95 is not None else ""
            run.say(
                f"Over the {runtimes.window_days_actual}-day retained window it ran "
                f"{runtimes.total_runs} time(s) with a {rate} success rate{duration}."
            )
        return runtimes

    def _add_chain_loads(self, run: _Run, chain_id: str) -> LoadClosure | None:
        load = run.step(
            "loads",
            "bw_get_load_closure",
            lambda: self._r.load_closure.chain_to_providers(chain_id),
            tables=("chain_edges", "dtp"),
        )
        if load is None:
            return None
        run.done("loads", count=len(load.providers_loaded))
        run.absorb("loads", load.caveats)
        for loaded in load.providers_loaded:
            run.relate(
                run.consumers,
                BwObjectRef(object_type=normalise_object_type(loaded.type_code), name=loaded.name),
                "loaded_provider",
                via=loaded.dtp_id,
                note=f"update mode {loaded.update_mode or 'unknown'}"
                + (f", via sub-chain {loaded.via_subchain}" if loaded.via_subchain else ""),
            )
        if load.providers_loaded:
            run.say(f"It loads {len(load.providers_loaded)} provider(s).")
        return load

    def _tables_evidence(
        self, name: str, logicals: Sequence[str], outcome: str
    ) -> list[Provenance]:
        """Cite the tables a conclusion was drawn from, when no single row carries it (D59).

        Needed because not every fact in a risk comes from one row. "No process chain loads this
        object" is a conclusion from an exhaustive read finding nothing, and a consumer count is a
        conclusion from a graph walk. Both are facts, both are alarming, and both need a source: a
        reader has to be able to tell "we walked the chain graph and the DTP register and found
        nothing" from a lookup that silently failed. ``outcome`` records which of those it was.

        Tables the connected release lacks are skipped rather than cited, so a citation never
        names something that was never read.
        """
        evidence: list[Provenance] = []
        for logical in logicals:
            status = self._r.capability.table(logical)
            if status is None or not status.present or not status.resolved_name:
                continue
            evidence.append(
                Provenance(
                    source_table=status.resolved_name,
                    source_key={"searched_for": name, "result": outcome},
                )
            )
        return evidence

    def _absence_evidence(self, name: str, logicals: Sequence[str]) -> list[Provenance]:
        """Cite the tables read to establish that something is *not* there."""
        return self._tables_evidence(name, logicals, "no matching row")

    def _consumer_evidence(self, name: str, impact: ImpactAnalysis | None) -> list[Provenance]:
        """Cite what established a consumer set.

        The consumer objects themselves cannot be cited, and mypy said so before I could talk myself
        into it: ``RelatedObject`` carries an ``Evidence`` record and ``ImpactAnalysis`` none at
        all. ``Evidence`` says *how firmly* a link was established, not *which row* established
        it, and treating one as the other would have produced a citation that looked precise and was
        not. So the tables the walk covered are cited instead, which is the true statement.
        """
        _ = impact  # kept in the signature: the reader identity is what the citation describes
        return self._tables_evidence(
            name,
            ("transformation", "object_dependencies", "query_provider"),
            "walked for consumers",
        )

    def _health_evidence(self, health: ProviderHealth) -> list[Provenance]:
        """Cite what a volume-or-currency claim rests on, most specific source first.

        ``ProviderHealth`` has no provenance of its own; its *parts* do. A currency claim comes from
        a request row, a volume claim from a generated table's row count, and where neither exists
        the ledgers that were read are named - because "this provider holds no rows" has to be
        distinguishable from "nothing could be read about this provider".
        """
        if health.last_request is not None:
            return list(_as_list(health.last_request.provenance))
        if health.tables:
            return [p for table in health.tables for p in _as_list(table.provenance)]
        return self._tables_evidence(
            health.provider,
            ("request_status", "adso_request", "cs_tables"),
            "read for volume and currency",
        )

    def _mixed_currency_risks(
        self,
        run: _Run,
        union_members: dict[str, list[str]],
        health_by_provider: dict[str, ProviderHealth],
    ) -> None:
        """Say when a union's parts disagree about how current they are (D33).

        The parts were always diagnosed correctly; nothing compared them. So a MultiProvider whose
        parts were one loaded today and three last loaded six to nine years ago produced four
        accurate findings and no statement that a single query unions them - which is the fact a
        reader needs, because a report over that union silently mixes current and historic data.

        Measured on the reference system: two MultiProviders each hold six parts, of which **three
        hold 1,081,275 rows between them at 2,442-3,191 days old** while two others hold 34 million
        rows loaded the same day. Both were reported as homogeneous.

        Deliberately *not* a severity escalation of the per-part findings. The owner confirmed the
        old parts are retained legacy sales history read only when a report asks for history, so the
        finding is that the union is heterogeneous - which a reader must know - and not that
        anything is broken.
        """
        for union, parts in union_members.items():
            ages = {
                part: health.data_age_days
                for part in parts
                if (health := health_by_provider.get(part)) is not None
                and health.data_age_days is not None
            }
            known = sorted(ages.values())
            if len(known) < _COMPARABLE_PARTS or known[-1] - known[0] < _MIXED_CURRENCY_SPREAD_DAYS:
                continue
            stale = sorted(
                (part for part, age in ages.items() if age >= _MIXED_CURRENCY_SPREAD_DAYS),
                key=lambda part: -(ages[part] or 0),
            )
            current = sorted(part for part, age in ages.items() if age < _STALE_DAYS)
            frozen_rows = sum(
                (health_by_provider[part].active_records or 0)
                for part in stale
                if part in health_by_provider
            )
            run.risk(
                "missing_data",
                "high" if known[-1] > _MIXED_CURRENCY_HIGH_DAYS else "medium",
                f"{union} unions parts of very different ages, so a report over it mixes current "
                "and historic data",
                f"{len(stale)} of {len(parts)} part(s) are far behind the others"
                + (f" and hold {frozen_rows:,} row(s) between them" if frozen_rows else "")
                + f": {', '.join(f'{p} ({ages[p]}d)' for p in stale[:_NAMED_PARTS])}"
                + (f", against {', '.join(current[:_NAMED_PARTS])} loaded within a day" if current
                   else "")
                + ". Confirm this is intended - retained history behind a live union is a normal "
                "design - and check which parts a report actually reads before comparing figures "
                "across periods.",
                objects=[union, *stale[:_RISK_OBJECTS]],
                # Each stale part's own health provenance, so the claim rests on the request rows
                # that date it rather than on the union, which has no ledger of its own (D58/D59).
                evidence=[
                    p
                    for part in stale[:_RISK_OBJECTS]
                    if part in health_by_provider
                    for p in self._health_evidence(health_by_provider[part])
                ],
                metrics={
                    "part_count": len(parts),
                    "part_ages_days": dict(sorted(ages.items())),
                    "age_spread_days": known[-1] - known[0],
                    "stale_part_rows": frozen_rows,
                },
            )

    def _expand_union_providers(
        self, run: _Run, providers: Sequence[str]
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Replace each union provider with the parts that actually hold data (D58).

        Without this, the central question of the whole tool goes unanswered on any report built on
        a MultiProvider or CompositeProvider - which is most of them. A union provider holds no rows
        and books no requests: it unions its parts when the query runs. So a currency check aimed at
        one finds nothing, a loading-chain lookup finds nothing, and both report exactly that. The
        payload then reads ``currency: no records found`` and ``loading_chains: no records``
        alongside ``confidence: high, 6 of 6 sections complete``, which is the most dangerous answer
        shape available: a silence that looks like a clean bill of health.

        Measured on the validation subject - a query on a MultiProvider whose three parts were one
        current and two sixty-six days behind, loaded by a monthly chain that had missed two cycles.
        The readers were never the problem: asked about a part directly, ``bw_get_provider_health``
        returns the age and ``bw_get_load_closure`` names the chain and its cadence. Only the choice
        of object was wrong.

        The union itself is dropped rather than diagnosed alongside its parts, because a request
        ledger has nothing to say about it and including it only adds a finding that says nothing.
        An unresolvable composition is the one case that stays loud: the parts exist and could not
        be read, which is a gap, and is emphatically not "this provider has no parts".
        """
        expanded: list[str] = []
        members: dict[str, list[str]] = {}
        for provider in providers:
            try:
                parts, source = self._r.providers.data_bearing_parts(provider)
            except Exception:
                # A failure to classify must not lose the provider: diagnosing the union directly is
                # a worse answer than diagnosing its parts, but it is far better than skipping it.
                expanded.append(provider)
                continue
            if source == "not_union":
                expanded.append(provider)
                continue
            if not parts:
                run.limit(
                    f"currency:{provider}",
                    f"{provider} unions other providers but its parts could not be resolved, so "
                    "currency was checked on the union itself, which holds no requests of its own. "
                    "Treat this as unknown, NOT as up to date.",
                    "metadata_dead_end",
                )
                expanded.append(provider)
                continue
            run.say(
                f"{provider} unions {len(parts)} part provider(s) and holds no data of its own, so "
                f"currency was checked on each part: {', '.join(parts[:_NAMED_PARTS])}"
                + (", and others." if len(parts) > _NAMED_PARTS else ".")
            )
            run.limit(
                f"currency:{provider}",
                f"{provider} is a union provider, so its own request ledger is empty by design "
                f"({source} composition). The {len(parts)} part provider(s) were checked instead. "
                "A report over a union can be stale in one part while the others are current.",
                "reader_caveat",
            )
            members[provider] = list(parts)
            expanded.extend(parts)
        # Order-preserving dedupe: two unions can share a part - measured on the validation subject,
        # where the same two cubes sat under two different MultiProviders - and diagnosing a part
        # twice would double every risk it raises.
        seen: set[str] = set()
        unique: list[str] = []
        for provider in expanded:
            if provider not in seen:
                seen.add(provider)
                unique.append(provider)
        return unique, members

    def _diagnose_provider(self, run: _Run, provider: str) -> ProviderHealth | None:
        """The load-failure question, per feeding provider: did the data arrive, and did it load?

        Returns the health record so the caller can compare parts of the same union against each
        other (D33). Previously returned ``None``: every part was diagnosed correctly and in
        isolation, so a union holding one current part and one frozen years ago produced two
        accurate findings and no statement that they disagreed.
        """
        gate = self._r.health.require_health()
        if gate is not None:
            run.step("currency", "bw_get_provider_health", lambda: gate, tables=("request_status",))
            return None
        health = run.step(
            "currency",
            "bw_get_provider_health",
            lambda: self._r.health.get_health(provider),
            tables=("request_status", "cs_tables"),
        )
        if health is None:
            return None
        run.done("currency", count=health.request_count, detail=provider)
        run.absorb(f"currency:{provider}", health.caveats)
        last = health.last_request
        # The health record's own provenance is what every risk below rests on, so it is resolved
        # once. Where a risk turns on one specific request row, that row's provenance is cited
        # instead: "this cube last loaded 66 days ago" is a claim about a single ledger entry, and
        # citing the whole reader would be vaguer than the fact deserves (D59).
        health_evidence = self._health_evidence(health)
        if health.unloaded:
            run.risk(
                "missing_data",
                "critical",
                f"{provider} holds no rows at all",
                f"Check whether {provider} has ever been loaded, and run its DTP before looking "
                "at anything downstream.",
                objects=[provider],
                evidence=health_evidence,
            )
        # "unknown" is excluded deliberately: an undecoded status code is not evidence of a failure,
        # and reporting it as one would send the reader after a load that may have been fine.
        if last is not None and last.status in ("error", "incomplete"):
            run.risk(
                "missing_data",
                "critical",
                f"The most recent load of {provider} finished {last.status}",
                f"Inspect request {last.request_id} in the {provider} request ledger and re-run "
                "it; a load that did not succeed explains missing rows directly.",
                objects=[provider],
                evidence=list(_as_list(last.provenance)),
                detail=f"update mode {last.update_mode or 'unknown'}",
                metrics={"request_status": last.status},
            )
        if health.data_age_days is not None and health.data_age_days > _STALE_DAYS:
            run.risk(
                "missing_data",
                "high" if health.data_age_days > _STALE_HIGH_DAYS else "medium",
                f"{provider} last loaded {health.data_age_days} day(s) before the newest request "
                "in the system",
                "Compare that against the cadence of the chain that loads it: a provider lagging "
                "the rest of the system explains a report showing yesterday's numbers.",
                objects=[provider],
                # The age is computed from the newest successful request, so that row is what a
                # reader has to open to check the number. This is the risk D59 was found on.
                evidence=list(_as_list(health.last_successful_request.provenance))
                if health.last_successful_request is not None
                else health_evidence,
                metrics={"data_age_days": health.data_age_days},
            )
        if health.failed_request_count:
            run.risk(
                "missing_data",
                "medium",
                f"{provider} has {health.failed_request_count} failed request(s) on record",
                "Check whether a failed request left a partial load in place.",
                objects=[provider],
                evidence=health_evidence,
                metrics={"failed_request_count": health.failed_request_count},
            )
        load = self._add_loading_chains(run, provider)
        if load is not None and not load.loading_chains:
            self._no_loader_risk(run, provider, load, health=health, severity="high")
        return health

    def _no_loader_risk(
        self,
        run: _Run,
        provider: str,
        load: LoadClosure,
        *,
        health: ProviderHealth | None,
        severity: Severity,
    ) -> None:
        """Raise "nothing loads this" - qualified by a frozen loader where one exists (D33).

        One helper for both risk sites, because they said the same thing in two places and only one
        of them would otherwise have been corrected.

        **What the qualification is for.** A populated provider whose only inbound transformation
        sits at a non-active version is not an orphan: it was loaded and no longer is. Told the
        plain sentence, a reader's next step is to remove it - and on the reference system that
        would mean deleting **1,081,275 rows** of deliberately retained sales history from a
        decommissioned source. So the frozen loader is named, and the severity is *lowered* rather
        than raised: a deliberate retention is less alarming than an unexplained orphan, and
        reporting it at the same level as a genuine one trains a reader to skip the category.
        """
        frozen = load.inactive_loaders
        rows = health.active_records if health is not None else None
        if frozen:
            named = ", ".join(
                f"{loader.tran_id} (version {loader.objvers}, status {loader.objstat or '?'}"
                + (f", from {loader.source_name}" if loader.source_name else "")
                + ")"
                for loader in frozen[:_NAMED_CHAINS]
            )
            run.risk(
                "missing_data" if severity == "high" else "object",
                # Deliberately below the unexplained case: this one has an explanation, and the
                # explanation is usually "the source was decommissioned and the data is kept".
                "medium" if severity == "high" else "low",
                f"{provider} is no longer loaded, but it was: its loader exists at a non-active "
                "version",
                f"Do not treat this as an orphan. {len(frozen)} transformation(s) target it and "
                f"are excluded because they are not the active version: {named}. Confirm whether "
                "the source was decommissioned and the data is retained on purpose"
                + (f" - it still holds {rows:,} row(s)." if rows else ".")
                + " Mission rule 6 reads the active version only, so the exclusion is correct; "
                "what would be wrong is reading it as never having been loaded.",
                objects=[provider, *[loader.tran_id for loader in frozen[:_RISK_OBJECTS]]],
                # The frozen rows themselves, which is the whole point: this claim rests on specific
                # RSTRAN rows rather than on an absence.
                evidence=[
                    p for loader in frozen[:_RISK_OBJECTS] for p in _as_list(loader.provenance)
                ],
                metrics={
                    "inactive_loaders": len(frozen),
                    "objvers": sorted({loader.objvers for loader in frozen}),
                    "active_records": rows,
                },
            )
            return
        run.risk(
            "missing_data" if severity == "high" else "object",
            severity,
            f"No walked process chain loads {provider}",
            "Confirm how it is loaded: a DTP run outside a chain has no schedule, so nothing "
            "guarantees the data is ever refreshed.",
            objects=[provider],
            # An absence still cites what was read to establish it. "We walked the chain graph, the
            # DTP register and the transformation catalogue and found no loader at any version" is a
            # checkable claim; "nothing loads this" on its own is not. `transformation` joined the
            # list when the non-active read did (D33) - a citation must name everything consulted.
            evidence=self._absence_evidence(provider, ("chain_edges", "dtp", "transformation")),
        )

    def _add_suspect_logic(self, run: _Run, providers: Sequence[str]) -> None:
        """Which inbound transformations carry routines - the layer to read once loads look fine."""
        if not providers:
            run.note_section(
                "logic",
                "bw_list_transformations",
                "not_applicable",
                detail="no provider resolved, so no inbound transformation could be identified",
            )
            return
        result = run.step(
            "logic",
            "bw_list_transformations",
            lambda: self._r.transformations.list_transformations(
                target_name=providers[0], with_routines_only=True, limit=25
            ),
            tables=("transformation",),
        )
        if result is None:
            return
        items, total = result
        run.done("logic", count=total)
        with_routines = [item.tran_id for item in items]
        if with_routines:
            run.risk(
                "missing_data",
                "medium",
                f"{total} inbound transformation(s) into {providers[0]} carry routines",
                "Read the routine source for these before concluding the source data is wrong: "
                "routine logic can drop or overwrite records, and metadata cannot say which.",
                objects=with_routines[:_RISK_OBJECTS],
                evidence=[p for item in items for p in _as_list(item.provenance)][:_RISK_OBJECTS],
                detail="Routine analysis is a static parse and a lower bound; dynamic calls are "
                "not followed.",
            )
            run.limit(
                "logic",
                "transformation routines were identified but not parsed here. Call "
                "bw_analyze_routine on a specific transformation for its table reads and "
                "anti-patterns.",
                "heuristic_lower_bound",
            )

    # --- risk derivation ------------------------------------------------------------------

    def _object_risks(
        self,
        run: _Run,
        definition: Provider,
        *,
        health: ProviderHealth | None,
        load: LoadClosure | None,
        impact: ImpactAnalysis | None,
    ) -> None:
        if not definition.active:
            run.risk(
                "object",
                "high",
                f"{definition.name} is not active",
                "Activate it or confirm it is deliberately retired; an inactive provider cannot be "
                "loaded and anything downstream of it is stale by definition.",
                objects=[definition.name],
                evidence=list(_as_list(definition.provenance)),
            )
        if load is not None and not load.loading_chains:
            # Same helper as the missing-data path (D33), so the qualification cannot be applied in
            # one place and forgotten in the other - which is how the two sentences drifted before.
            self._no_loader_risk(run, definition.name, load, health=health, severity="medium")
        modes = {c.frequency for c in (load.loading_chains if load else [])}
        if len(modes) > 1:
            run.risk(
                "object",
                "medium",
                f"{definition.name} is loaded by chains on different cadences",
                "Check which chain wins on a day when both run; a provider fed at two cadences can "
                "hold data of two different ages at once.",
                objects=[definition.name],
                evidence=[
                    p
                    for cadence in (load.loading_chains if load else [])
                    for p in _as_list(cadence.provenance)
                ][:_RISK_OBJECTS],
                metrics={"cadences": sorted(modes)},
            )
        if health is not None and health.changelog_records > max(health.active_records, 1) * 3:
            run.risk(
                "object",
                "medium",
                f"{definition.name} holds far more changelog than active rows",
                "Consider a changelog deletion policy: the changelog is "
                f"{health.changelog_records / max(health.active_records, 1):.1f}x the active table "
                "and is charged to memory on a HANA system.",
                objects=[definition.name],
                evidence=self._health_evidence(health),
                metrics={
                    "active_records": health.active_records,
                    "changelog_records": health.changelog_records,
                },
            )
        if impact is not None and not run.consumers:
            run.risk(
                "object",
                "low",
                f"Nothing maintained appears to depend on {definition.name}",
                "Check bw_find_unused_providers and bw_get_hana_crossings before acting: "
                "consumption from outside BW is not visible here.",
                objects=[definition.name],
                evidence=self._consumer_evidence(definition.name, impact),
            )

    def _query_risks(
        self,
        run: _Run,
        definition: Query,
        *,
        lineage: QueryLineage | None,
        usage: QueryUsage | None,
        security: QueryAuthExposure | None,
    ) -> None:
        # COMPID is the technical name a person recognises, but it is nullable; the COMPUID always
        # exists, so a risk always names something a caller can look the query up by.
        name = definition.compid or definition.compuid
        if usage is not None and usage.decommission_candidate:
            run.risk(
                "query",
                "low",
                f"{name} looks like a decommission candidate",
                "Confirm with the owner before removing it; last-used is only recorded for "
                "executions BW saw.",
                objects=[name],
                detail=usage.reason,
                evidence=list(_as_list(usage.provenance)),
            )
        if security is not None and security.user_specific_result:
            run.risk(
                "query",
                "medium",
                f"{name} returns different data per user",
                "Expect two users to see different numbers legitimately. Compare their analysis "
                "authorisations before treating a discrepancy as a data fault.",
                objects=[name],
                evidence=list(_as_list(security.provenance)),
                metrics={"characteristics": security.auth_relevant_characteristics[:_RISK_OBJECTS]},
            )
        if security is not None and security.uncovered_characteristics:
            uncovered = security.uncovered_characteristics[:_NAMED_CHAINS]
            run.risk(
                "query",
                "high",
                "This query touches authorisation-relevant characteristics that no authorisation "
                "covers",
                "Every user without a catch-all authorisation is blocked from this query until an "
                f"authorisation covers {', '.join(uncovered)}.",
                objects=[name, *uncovered],
                evidence=list(_as_list(security.provenance)),
            )
        exit_variables = list(lineage.customer_exit_variables) if lineage else []
        if exit_variables:
            run.risk(
                "query",
                "info",
                f"{len(exit_variables)} customer-exit variable(s) decide what this query returns",
                "Read the ABAP exit to know the effective selection; metadata can name these but "
                "cannot resolve their values.",
                objects=exit_variables[:_RISK_OBJECTS],
                evidence=list(_as_list(lineage.provenance)) if lineage else [],
            )
        if not definition.providers:
            run.risk(
                "query",
                "medium",
                f"No InfoProvider resolved for {name}",
                "Without a provider nothing can say what this report reads or when its data is "
                "current; check RSZCOMPIC for this COMPUID.",
                objects=[name],
                evidence=self._absence_evidence(definition.compuid, ("query_provider",)),
            )
        self._condition_risks(run, definition, name)

    @staticmethod
    def _condition_risks_detail(condition: QueryCondition) -> str:
        """One active condition: what it is, how it cuts, and whether the cut-off is knowable.

        The operator code is reported even when it has no label, because the two ranking operators
        this landscape uses are not declared fixed values of SAP's own domain and a blank is worse
        than a raw code. A variable threshold means the cut-off is not in metadata at all.
        """
        label = condition.description or condition.name or condition.eltuid
        if condition.alert_levels:
            # An exception has no single operator: it has bands, and the severity of each is what a
            # reader needs. Naming them beats naming a threshold it does not have (D41).
            bands = ", ".join(
                level.level.label or level.level.code for level in condition.alert_levels
            )
            scope = condition.evaluation_scope
            where = f" on {scope.label}" if scope and scope.label else ""
            return f"{label}: {len(condition.alert_levels)} band(s) ({bands}){where}"
        operator = condition.operator.code if condition.operator else "operator unknown"
        suffix = (
            " with a threshold resolved per execution"
            if condition.threshold_source and condition.threshold_source.runtime_resolved
            else ""
        )
        return f"{label}: {operator}{suffix}"

    def _condition_risks(self, run: _Run, definition: Query, name: str) -> None:
        """Conditions change which rows a reader sees while changing no figure (D26).

        That is exactly why they belong in the risks and not only in the definition. An active Top N
        suppresses rows silently: the query totals stop matching the provider and nothing in the
        result says a filter did it. An *inactive* one is the opposite problem - harmless today, and
        one checkbox away from changing every number a report consumer compares week to week - so it
        is reported at ``info`` rather than dropped.
        """
        # A condition suppresses rows; an exception colours them. Only the first stops totals
        # reconciling, so they are raised separately rather than counted together (D41).
        suppressing = [c for c in definition.conditions if c.active and c.kind == "condition"]
        colouring = [c for c in definition.conditions if c.active and c.kind == "exception"]
        inactive = [c for c in definition.conditions if not c.active]
        if suppressing:
            labels = [c.description or c.name or c.eltuid for c in suppressing]
            run.risk(
                "query",
                "medium",
                f"{len(suppressing)} active condition(s) suppress rows in {name}",
                "Totals here will not reconcile against the provider, and nothing in the result "
                "says a condition did it. Check these before treating a gap as a load fault.",
                objects=[name, *labels[:_RISK_OBJECTS]],
                evidence=[
                    p for c in suppressing[:_RISK_OBJECTS] for p in _as_list(c.provenance)
                ],
                detail="; ".join(
                    self._condition_risks_detail(c) for c in suppressing[:_RISK_OBJECTS]
                ),
            )
        if colouring:
            run.risk(
                "query",
                "info",
                f"{len(colouring)} active exception(s) colour results in {name}",
                "Figures are unaffected, so this never explains a reconciliation gap. It does "
                "explain why a value looks flagged, and the band thresholds are the thing to read "
                "before changing what counts as acceptable.",
                objects=[name],
                evidence=[p for c in colouring[:_RISK_OBJECTS] for p in _as_list(c.provenance)],
                detail="; ".join(
                    self._condition_risks_detail(c) for c in colouring[:_RISK_OBJECTS]
                ),
            )
        if inactive:
            run.risk(
                "query",
                "info",
                f"{len(inactive)} condition(s) or exception(s) are defined on {name} but off",
                "They change nothing today. Activating one changes which rows every consumer of "
                "this report sees, or how they are flagged, without changing any figure - so treat "
                "it as a content change.",
                objects=[name],
                evidence=[p for c in inactive[:_RISK_OBJECTS] for p in _as_list(c.provenance)],
            )

    def _chain_risks(
        self,
        run: _Run,
        chain: Chain,
        *,
        runtimes: ChainRuntimes | None,
        load: LoadClosure | None,
    ) -> None:
        if not chain.active:
            run.risk(
                "process_chain",
                "high",
                f"{chain.chain_id} is not active",
                "Confirm it is deliberately stopped: anything it loads is not being refreshed.",
                objects=[chain.chain_id],
                evidence=list(_as_list(chain.provenance)),
            )
        if runtimes is None:
            return
        rate = runtimes.success_rate
        if runtimes.total_runs and rate is not None and rate < _UNRELIABLE_RATE:
            # The recommendation used to say "investigate the failing step" against a payload that
            # never named one, so the reader's only lead was `bottleneck_steps` - ranked by
            # duration,
            # which on a chain that fails fast is a different step entirely (D65). Now the step is
            # named, with how often it failed and how long it ran before doing so.
            failing = runtimes.failed_steps
            if failing:
                named = ", ".join(
                    f"{step.variant or step.process_type} "
                    f"({step.occurrences}x, {step.state_label.lower()}"
                    + (f", up to {step.longest_s:.0f}s" if step.longest_s is not None else "")
                    + ")"
                    for step in failing[:_NAMED_CHAINS]
                )
                recommendation = (
                    f"Start with the step(s) that actually failed: {named}. "
                    "These are ranked by how often they failed, not by how long they ran - the "
                    "slowest step is reported separately and is often not the one that broke."
                )
            else:
                recommendation = (
                    "No individual step is recorded as failed in the step log for "
                    "the runs examined, so the failure is at run level (or outside "
                    "the step window). Check the run log in RSPC directly before "
                    "relying on anything this chain loads."
                )
            run.risk(
                "process_chain",
                "critical" if rate < _FAILING_RATE else "high",
                f"{chain.chain_id} succeeded on only {rate:.0%} of its runs",
                recommendation,
                objects=[
                    chain.chain_id,
                    *[s.variant for s in failing[:_RISK_OBJECTS] if s.variant],
                ],
                # The failing steps' own rows, most specific first: each carries its LOG_ID, TYPE
                # and
                # STATE, which is what someone would search RSPC for. Falls back to the chain-level
                # provenance when no step failed, because then that is genuinely all this rests on.
                evidence=(
                    [p for step in failing[:_RISK_OBJECTS] for p in _as_list(step.provenance)]
                    or list(_as_list(runtimes.provenance))
                ),
                metrics={
                    "success_rate": rate,
                    "total_runs": runtimes.total_runs,
                    "successful_runs": runtimes.successful_runs,
                    "failed_steps": len(failing),
                    "failed_step_occurrences": sum(s.occurrences for s in failing),
                    # Counted so a zero failed-step list is not read as "nothing else was wrong":
                    # skipped and still-running steps are neither successes nor failures.
                    "indeterminate_steps": runtimes.indeterminate_steps,
                    "steps_examined": runtimes.steps_examined,
                },
            )
        stats = runtimes.duration_seconds
        median, p95 = stats.median_s, stats.p95_s
        if (
            stats.count > 1
            and median is not None
            and p95 is not None
            and median > 0
            and p95 > median * _VARIANCE_FACTOR
        ):
            run.risk(
                "process_chain",
                "medium",
                f"{chain.chain_id} runtime varies widely between runs",
                "Treat the p95 rather than the median as the planning number, and check the "
                "contention count below: a chain whose duration triples is usually sharing the "
                "window with something else.",
                objects=[chain.chain_id],
                evidence=list(_as_list(runtimes.provenance)),
                metrics={"median_s": median, "p95_s": p95},
            )
        # Two separate findings, because they were one number wearing the other's caption (D64). A
        # chain overlapping *itself* is a scheduling fault in that chain; a chain sharing its window
        # with *others* is contention, and only the second explains a duration that is not the
        # chain's own cost. The old single risk was titled "at the same time as another chain" and
        # driven by the self-overlap count, so on a chain contending heavily it reported nothing.
        if runtimes.self_overlap_runs:
            run.risk(
                "process_chain",
                "high",
                f"{chain.chain_id} starts again before its previous run has finished",
                "Check its schedule against its p95 duration: overlapping runs of one chain can "
                "load the same target twice at once, and its durations are not independent.",
                objects=[chain.chain_id],
                evidence=list(_as_list(runtimes.provenance)),
                metrics={"self_overlap_runs": runtimes.self_overlap_runs},
            )
        if runtimes.contended_runs:
            run.risk(
                "process_chain",
                "medium",
                f"{chain.chain_id} was observed running at the same time as another chain",
                "Its measured durations reflect contention rather than intrinsic cost; do not read "
                "them as a fixed property. Contending chains: "
                + ", ".join(runtimes.contending_chains[:_NAMED_CHAINS]),
                objects=[chain.chain_id, *runtimes.contending_chains[:_RISK_OBJECTS]],
                evidence=list(_as_list(runtimes.provenance)),
                metrics={
                    "contended_runs": runtimes.contended_runs,
                    "contending_chains": len(runtimes.contending_chains),
                },
            )
        # Slowest and failed are stated as two separate sentences, and the wording keeps them apart
        # on purpose. They are different questions, and on the validation subject they happened to
        # have the same answer - its failing loads hung for ~23.5 hours before dying - which is
        # exactly what hid D65. On a chain that fails fast they differ, and a reader who was handed
        # only the slowest step would be looking at the wrong one.
        if runtimes.bottleneck_steps:
            slowest = runtimes.bottleneck_steps[0]
            run.say(
                f"The longest-running step is {slowest.variant or slowest.process_type} at "
                f"{slowest.duration_s:.0f}s (longest, not necessarily failing)."
            )
        if runtimes.failed_steps:
            worst = runtimes.failed_steps[0]
            duration = f", running up to {worst.longest_s:.0f}s" if worst.longest_s else ""
            run.say(
                f"The step that failed most often is {worst.variant or worst.process_type}: "
                f"{worst.occurrences} failure(s), {worst.state_label.lower()}{duration}."
            )
        elif runtimes.steps_examined and runtimes.success_rate not in (None, 1.0):
            run.say(
                f"No step is recorded as failed across the {runtimes.steps_examined} step(s) "
                "examined, so the failure is recorded at run level rather than against a step."
            )
        modes = {p.update_mode for p in (load.providers_loaded if load else []) if p.update_mode}
        if len(modes) > 1:
            run.risk(
                "process_chain",
                "low",
                f"{chain.chain_id} loads in more than one update mode ({', '.join(sorted(modes))})",
                "Confirm the sequencing: a full load and a delta load writing the same targets in "
                "one chain depend on running in the right order.",
                objects=[chain.chain_id],
                evidence=[
                    p
                    for loaded in (load.providers_loaded if load else [])[:_RISK_OBJECTS]
                    for p in _as_list(loaded.provenance)
                ],
            )

    def _change_risks(
        self, run: _Run, name: str, *, impact: ImpactAnalysis | None, load: LoadClosure | None
    ) -> None:
        advisory = [c for c in run.consumers if c.advisory]
        if advisory:
            run.risk(
                "change_impact",
                "high",
                f"{len(advisory)} consumer(s) of {name} are invisible to BW's own where-used list",
                "Review each one by hand before transporting: these were found by parsing ABAP or "
                "by resolving a generated table name, and BW will not warn you about them.",
                objects=[c.ref.name for c in advisory[:10]],
                evidence=self._consumer_evidence(name, impact),
            )
        queries = [c for c in run.consumers if c.relationship == "consumer_query"]
        if queries:
            run.risk(
                "change_impact",
                "medium",
                f"{len(queries)} report(s) read {name} and need re-validation after the change",
                "Re-run each report and compare figures; a shared restricted key figure or "
                "structure changes every consumer at once.",
                objects=[c.ref.name for c in queries[:10]],
                evidence=self._consumer_evidence(name, impact),
            )
        calcviews = [c for c in run.consumers if c.relationship == "consumer_calcview"]
        if calcviews:
            run.risk(
                "change_impact",
                "high",
                f"{len(calcviews)} calc view(s) read this object's generated table",
                "Coordinate the change with the HANA side: a calc view reads the generated table "
                "directly, so a field change breaks it with no BW activation warning.",
                objects=[c.ref.name for c in calcviews[:10]],
                evidence=self._consumer_evidence(name, impact),
            )
        if load is not None and load.loading_chains:
            run.risk(
                "change_impact",
                "info",
                f"{len(load.loading_chains)} chain(s) load {name} and must be re-run after "
                "re-activation",
                "Re-run them in dependency order: "
                + ", ".join(c.chain_id for c in load.loading_chains[:5]),
                objects=[c.chain_id for c in load.loading_chains[:5]],
                evidence=[
                    p
                    for cadence in load.loading_chains[:_RISK_OBJECTS]
                    for p in _as_list(cadence.provenance)
                ],
            )
        if impact is None:
            run.risk(
                "change_impact",
                "high",
                "The downstream blast radius could not be established",
                "Do not transport on the strength of this analysis; resolve the reported "
                "limitation first.",
                objects=[name],
                evidence=self._absence_evidence(name, ("transformation", "dtp")),
            )

    # --- next actions ---------------------------------------------------------------------

    def _object_actions(
        self, definition: Provider, *, health: ProviderHealth | None, load: LoadClosure | None
    ) -> list[NextAction]:
        actions = [
            NextAction(
                order=1,
                action="Review the full field list and its provenance",
                why="The analysis summarises fields; the resource holds every field with the table "
                "it came from.",
                tool="bw_describe_object",
                arguments={"name": definition.name, "detail": "full"},
            ),
            NextAction(
                order=2,
                action="Draw the dataflow",
                why="A diagram shows the shape of the graph without spending context on JSON.",
                tool="bw_render_lineage",
                arguments={"name": definition.name, "direction": "both"},
            ),
        ]
        if load is not None and load.loading_chains:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action=f"Check the run history of {load.loading_chains[0].chain_id}",
                    why="The loading chain's reliability is what determines whether this object's "
                    "data is current.",
                    tool="bw_get_chain_runtimes",
                    arguments={"chain_id": load.loading_chains[0].chain_id},
                )
            )
        if health is None:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action="Establish volume and currency another way",
                    why="Provider health was not available in this analysis, so how much data this "
                    "object holds and when it last loaded are unknown.",
                    tool="bw_get_provider_health",
                    arguments={"provider": definition.name},
                )
            )
        return actions

    def _query_actions(
        self,
        definition: Query,
        *,
        usage: QueryUsage | None,
        security: QueryAuthExposure | None,
    ) -> list[NextAction]:
        name = definition.compid or definition.compuid
        actions = [
            NextAction(
                order=1,
                action="Trace one figure to its DataSource field",
                why="Field-level lineage is what answers 'where does this number come from'.",
                tool="bw_get_query_lineage",
                arguments={"query": name},
            )
        ]
        if security is not None and security.user_specific_result:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action="Read the analysis authorisation behind the restriction",
                    why="This is the only path that returns concrete permission values, which is "
                    "what settles a two-users-two-numbers dispute.",
                    tool="bw_get_analysis_auth",
                    arguments={"name": "<the authorisation covering the characteristic>"},
                )
            )
        if usage is not None and usage.decommission_candidate:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action="Confirm with the owner before decommissioning",
                    why="Last-used only records executions BW observed, so it is evidence of "
                    "disuse rather than proof of it.",
                    tool="bw_get_query_usage",
                    arguments={"query": name, "stale_days": "730"},
                )
            )
        return actions

    def _chain_actions(self, chain: Chain, *, runtimes: ChainRuntimes | None) -> list[NextAction]:
        actions = [
            NextAction(
                order=1,
                action="See what this chain loads, walked through its sub-chains",
                why="Most loads live in nested sub-chains rather than the top-level step list.",
                tool="bw_get_load_closure",
                arguments={"chain_id": chain.chain_id},
            )
        ]
        if runtimes is not None and runtimes.bottleneck_steps:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action="Check where this chain sits against everything else",
                    why="A slow step matters differently when another chain runs at the same time.",
                    tool="bw_get_schedule_matrix",
                    arguments={},
                )
            )
        if chain.subchain_ids:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action=f"Analyse sub-chain {chain.subchain_ids[0]} on its own",
                    why="A parent chain's schedule governs the load, but the failing step is "
                    "usually inside a sub-chain.",
                    tool="bw_analyze_process_chain",
                    arguments={"chain_id": chain.subchain_ids[0]},
                )
            )
        return actions

    def _change_actions(
        self, run: _Run, name: str, *, load: LoadClosure | None
    ) -> list[NextAction]:
        actions = [
            NextAction(
                order=1,
                action="Take a snapshot before the change",
                why="It is the only way to prove afterwards what the change actually altered, and "
                "a transport log does not say.",
                tool="bw_create_snapshot",
                arguments={},
            ),
            NextAction(
                order=2,
                action="Check the change does not create a layer violation",
                why="A CompositeProvider feeding a DSO, or a circular dependency, makes the loaded "
                "result depend on load order.",
                tool="bw_find_layer_violations",
                arguments={},
            ),
            NextAction(
                order=3,
                action="Check the change does not create a stale-lookup load",
                why="A full-update load reading a less-frequently-refreshed object enriches new "
                "data against old.",
                tool="bw_check_load_latency",
                arguments={},
            ),
        ]
        advisory = [c for c in run.consumers if c.advisory]
        if advisory:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action=f"Read the routine in {advisory[0].ref.name} by hand",
                    why="Routine-embedded consumers are a heuristic lower bound and BW will not "
                    "warn about them, so each one needs eyes.",
                    tool="bw_analyze_routine",
                    arguments={"transformation_id": advisory[0].via or "<transformation id>"},
                )
            )
        if load is not None and load.loading_chains:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action="Re-run the loading chains after re-activation",
                    why="Re-activation does not reload data; the chains that load this object have "
                    "to run before anything downstream is correct.",
                    tool="bw_get_load_closure",
                    arguments={"provider": name},
                )
            )
        actions.append(
            NextAction(
                order=len(actions) + 1,
                action="Compare a snapshot after the change",
                why="Confirms the change altered exactly what was intended and nothing else.",
                tool="bw_compare_snapshots",
                arguments={"left": "<the snapshot id taken before>"},
            )
        )
        return actions

    def _missing_data_actions(
        self, run: _Run, target: str, *, providers: Sequence[str]
    ) -> list[NextAction]:
        actions: list[NextAction] = []
        blocking = [r for r in run.risks if r.severity in ("critical", "high")]
        if blocking:
            actions.append(
                NextAction(
                    order=1,
                    action=f"Resolve first: {blocking[0].title}",
                    why=blocking[0].recommendation,
                    tool="bw_get_provider_health",
                    arguments={"provider": blocking[0].affected_objects[0]}
                    if blocking[0].affected_objects
                    else {},
                )
            )
        if providers:
            actions.append(
                NextAction(
                    order=len(actions) + 1,
                    action=f"Check the chain that loads {providers[0]}",
                    why="A late or failed run of the loading chain is the most common cause of a "
                    "report showing old numbers.",
                    tool="bw_get_load_closure",
                    arguments={"provider": providers[0]},
                )
            )
        actions.append(
            NextAction(
                order=len(actions) + 1,
                action="Check the landscape-wide stale-lookup risk",
                why="A full-update load enriching against once-daily master data produces wrong "
                "numbers with every load succeeding.",
                tool="bw_check_load_latency",
                arguments={},
            )
        )
        actions.append(
            NextAction(
                order=len(actions) + 1,
                action=f"Read the transformation logic feeding {target}",
                why="Once loads and authorisations are ruled out, routine logic is the remaining "
                "place a record can be dropped or overwritten.",
                tool="bw_analyze_routine",
                arguments={"transformation_id": "<from the logic section above>"},
            )
        )
        return actions


def _as_list(provenance: Provenance | list[Provenance] | None) -> list[Provenance]:
    if provenance is None:
        return []
    if isinstance(provenance, list):
        return provenance
    return [provenance]


def _ref_for(graph: LineageGraph, node_id: str) -> BwObjectRef:
    for node in graph.nodes:
        if node.id == node_id:
            return _node_ref(node)
    return BwObjectRef(object_type="unknown", name=node_id.split(":", 1)[-1])


def _node_ref(node: Any) -> BwObjectRef:
    ref = getattr(node, "ref", None)
    if isinstance(ref, BwObjectRef):
        return ref
    return BwObjectRef(
        object_type=normalise_object_type(getattr(node, "node_type", "unknown")),
        name=getattr(node, "name", ""),
    )
