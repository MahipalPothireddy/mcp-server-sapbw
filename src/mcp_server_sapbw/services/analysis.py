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
from ..models.chains import Chain, ChainRuntimes, LoadClosure
from ..models.completeness import BoundHit, Completeness
from ..models.ecc import ConnectorUnavailable
from ..models.evidence import Evidence, EvidenceSummary, evidence_for, summarise
from ..models.findings import Finding, Severity
from ..models.health import ProviderHealth
from ..models.lineage import ImpactAnalysis, LineageGraph
from ..models.objects import BwObjectRef, normalise_object_type
from ..models.provenance import Provenance, UnsupportedResult
from ..models.providers import ObjectNotFound, Provider
from ..models.queries import Query, QueryLineage, QueryUsage
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

        runtimes = self._add_runtimes(run, chain_id, days=days)
        load = self._add_chain_loads(run, chain_id)
        self._chain_risks(run, chain, runtimes=runtimes, load=load)

        return run.build(
            kind="process_chain",
            subject=subject,
            subject_name=chain.chain_id,
            title=f"Process chain analysis: {chain.chain_id}",
            chain=chain,
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

        checked = 0
        for provider in providers[:_DIAGNOSE_CAP]:
            checked += 1
            self._diagnose_provider(run, provider)
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
            if edge.dst == root and edge.src != root:
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
        run.done("loading_chains", count=len(load.loading_chains))
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
            run.say(
                "It is loaded by "
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
            tables=("auth_value",),
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

    def _diagnose_provider(self, run: _Run, provider: str) -> None:
        """The load-failure question, per feeding provider: did the data arrive, and did it load?"""
        gate = self._r.health.require_health()
        if gate is not None:
            run.step("currency", "bw_get_provider_health", lambda: gate, tables=("request_status",))
            return
        health = run.step(
            "currency",
            "bw_get_provider_health",
            lambda: self._r.health.get_health(provider),
            tables=("request_status", "cs_tables"),
        )
        if health is None:
            return
        run.done("currency", count=health.request_count, detail=provider)
        run.absorb(f"currency:{provider}", health.caveats)
        last = health.last_request
        if health.unloaded:
            run.risk(
                "missing_data",
                "critical",
                f"{provider} holds no rows at all",
                f"Check whether {provider} has ever been loaded, and run its DTP before looking "
                "at anything downstream.",
                objects=[provider],
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
                metrics={"data_age_days": health.data_age_days},
            )
        if health.failed_request_count:
            run.risk(
                "missing_data",
                "medium",
                f"{provider} has {health.failed_request_count} failed request(s) on record",
                "Check whether a failed request left a partial load in place.",
                objects=[provider],
                metrics={"failed_request_count": health.failed_request_count},
            )
        load = self._add_loading_chains(run, provider)
        if load is not None and not load.loading_chains:
            run.risk(
                "missing_data",
                "high",
                f"No walked process chain loads {provider}",
                "Confirm how it is loaded: a DTP run outside a chain has no schedule, so nothing "
                "guarantees the data is ever refreshed.",
                objects=[provider],
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
            run.risk(
                "object",
                "medium",
                f"No walked process chain loads {definition.name}",
                "Confirm how it is loaded. A DTP run outside a chain has no observable schedule, "
                "so nothing here can say when its data is current.",
                objects=[definition.name],
            )
        modes = {c.frequency for c in (load.loading_chains if load else [])}
        if len(modes) > 1:
            run.risk(
                "object",
                "medium",
                f"{definition.name} is loaded by chains on different cadences",
                "Check which chain wins on a day when both run; a provider fed at two cadences can "
                "hold data of two different ages at once.",
                objects=[definition.name],
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
            )
        if not definition.providers:
            run.risk(
                "query",
                "medium",
                f"No InfoProvider resolved for {name}",
                "Without a provider nothing can say what this report reads or when its data is "
                "current; check RSZCOMPIC for this COMPUID.",
                objects=[name],
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
            run.risk(
                "process_chain",
                "critical" if rate < _FAILING_RATE else "high",
                f"{chain.chain_id} succeeded on only {rate:.0%} of its runs",
                "Investigate the failing step before relying on anything this chain loads.",
                objects=[chain.chain_id],
                evidence=list(_as_list(runtimes.provenance)),
                metrics={
                    "success_rate": rate,
                    "total_runs": runtimes.total_runs,
                    "successful_runs": runtimes.successful_runs,
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
                "observed overlaps: a chain whose duration triples is usually contending with "
                "another one.",
                objects=[chain.chain_id],
                metrics={"median_s": median, "p95_s": p95},
            )
        if runtimes.observed_overlap_runs:
            run.risk(
                "process_chain",
                "medium",
                f"{chain.chain_id} was observed running at the same time as another chain",
                "Its measured durations reflect contention rather than intrinsic cost; do not read "
                "them as a fixed property.",
                objects=[chain.chain_id],
                metrics={"observed_overlap_runs": runtimes.observed_overlap_runs},
            )
        if runtimes.bottleneck_steps:
            slowest = runtimes.bottleneck_steps[0]
            run.say(
                f"The slowest step is {slowest.variant or slowest.process_type} at "
                f"{slowest.duration_s:.0f}s."
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
            )
        if impact is None:
            run.risk(
                "change_impact",
                "high",
                "The downstream blast radius could not be established",
                "Do not transport on the strength of this analysis; resolve the reported "
                "limitation first.",
                objects=[name],
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
