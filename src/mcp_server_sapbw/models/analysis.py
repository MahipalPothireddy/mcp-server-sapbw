"""One shape for a composed answer, and the audit trail that makes it checkable.

**Why these exist.** The granular tools each answer one narrow question well, and a real analyst
question needs six or seven of them. Sequencing those calls is work the caller should not have to
do, and doing it in a model's head costs a round trip per reader plus the context to hold each
payload. A compound tool does the sequencing server-side and returns one answer.

The risk in doing that is the reason for everything in this module: **a merged answer is
unverifiable unless it says how it was assembled.** Five payloads flattened into prose read as one
authoritative statement, and a reader cannot tell which part came from a metadata row, which came
from a heuristic, and which section was silently absent because the release could not report it. So
every composed answer carries:

* :class:`AnalysisStep` per section - the reader that ran, its status, the physical tables it read,
  and **the granular tool that reproduces it**. That last field is what makes the answer auditable
  rather than merely detailed: any section can be re-run on its own and compared.
* :class:`AnalysisLimitation` per thing that cannot be concluded, with a machine-readable reason,
  so "no consumers found" and "consumers could not be read here" are never the same answer.
* :class:`AnalysisConfidence` - deliberately **not** a percentage. A single number would merge the
  question "how much did we manage to read" with "how firmly is each fact established", and those
  fail differently and are fixed differently.

The envelope is shared across all five analyses on purpose: a client learns one contract, and the
subject-specific payloads are typed optional fields. A ``None`` payload is never ambiguous, because
``steps`` records whether that section was ``not_applicable`` for this subject or ``unsupported`` on
this release.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capability import ValidationStatus
from .chains import Chain, ChainCadence, ChainRuntimes, LoadClosure
from .completeness import BoundedResult
from .evidence import Evidence, EvidenceBasis, EvidenceCompleteness, EvidenceSummary
from .findings import Finding, severity_rank
from .health import ProviderHealth
from .lineage import ImpactAnalysis, LineageGraph, TraceToSource
from .objects import BwObjectRef
from .providers import Provider
from .queries import Query, QueryLineage, QueryUsage
from .security import QueryAuthExposure

#: Which question was asked. One value per compound tool.
AnalysisKind = Literal[
    "object",
    "query",
    "process_chain",
    "change_impact",
    "missing_data",
]

#: What became of one section of the analysis.
#:
#: The three failure values are kept apart because they have different remedies and only one of them
#: is a gap in this server: ``unsupported`` means the release lacks the metadata,
#: ``connector_required`` means the answer lives outside BW, and ``failed`` means the read broke.
SectionStatus = Literal[
    "complete",
    "partial",
    "empty",
    "not_applicable",
    "unsupported",
    "connector_required",
    "skipped_budget",
    "failed",
]

#: Why something cannot be concluded. Machine-readable so a caller can filter on the ones it can act
#: on, rather than parsing sentences.
LimitationReason = Literal[
    "unsupported_on_release",
    "connector_not_configured",
    "heuristic_lower_bound",
    "budget_exhausted",
    "truncated",
    "metadata_dead_end",
    "reader_caveat",
]

#: How an object relates to the subject. ``consumer_routine`` is the one BW's own where-used list
#: does not have, and it is always advisory. ``consumer_lookup`` is its exact counterpart - a read
#: BW declares in a typed rule-step table - and is never advisory; the two are separate values
#: because a caller deciding whether to act on an edge needs to know whether BW stated it or we
#: inferred it from ABAP text.
Relationship = Literal[
    "upstream",
    "downstream",
    "part_provider",
    "consumer_query",
    "consumer_calcview",
    "consumer_lookup",
    "consumer_routine",
    "loading_chain",
    "source_datasource",
    "loaded_provider",
]


class RelatedObject(BaseModel):
    """One object connected to the subject, and how firmly that connection is established."""

    model_config = ConfigDict(extra="forbid")

    ref: BwObjectRef
    relationship: Relationship
    #: The transformation, DTP, calc view or chain the relationship runs through.
    via: str | None = None
    #: True when the link was derived rather than declared - a routine's parsed reads, or a name
    #: resolved by convention. Never merged with a declared edge.
    advisory: bool = False
    evidence: Evidence | None = None
    note: str | None = None

    @property
    def id(self) -> str:
        return self.ref.id


class AnalysisStep(BaseModel):
    """One reader's contribution, as an audit row.

    ``tool`` names the granular tool that reproduces this section alone. That is the field that
    turns a composed answer into a checkable one: a reader who doubts a section can re-run exactly
    that call and compare, without reverse-engineering what the compound tool did.
    """

    model_config = ConfigDict(extra="forbid")

    section: str
    tool: str
    status: SectionStatus
    #: Resolved physical tables read, so the answer's provenance is visible at section level as well
    #: as on each record.
    source_tables: list[str] = Field(default_factory=list)
    #: How many records the section contributed, where counting one is meaningful.
    record_count: int | None = None
    detail: str | None = None


class AnalysisLimitation(BaseModel):
    """Something this answer cannot establish, and why."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    limitation: str
    reason: LimitationReason


class NextAction(BaseModel):
    """One thing to do next, expressed as a call the caller can actually make.

    Naming the tool and its arguments rather than describing the step in prose is the difference
    between advice and a runnable next step, and it keeps the compound tool from becoming a dead end
    that a caller has to translate back into tool calls by hand.
    """

    model_config = ConfigDict(extra="forbid")

    order: int
    action: str
    why: str
    tool: str | None = None
    arguments: dict[str, str] = Field(default_factory=dict)


class AnalysisConfidence(BaseModel):
    """How firmly the whole answer is established, kept as components rather than one number.

    A single percentage would merge two independent things: **coverage** (how many sections could be
    read at all) and **basis** (how firmly each fact that was read is established). A release
    missing one metadata table and a dependency that came from parsing ABAP are both "less certain",
    but one is fixed by a different BW release and the other cannot be fixed at all. Collapsing them
    would hide which.

    ``level`` is a summary for a human skim; ``reasons`` says what drove it, and the counts are the
    evidence for that. Nothing here is a probability and it is not presented as one.
    """

    model_config = ConfigDict(extra="forbid")

    level: Literal["high", "medium", "low"]
    sections_total: int = 0
    sections_complete: int = 0
    sections_unsupported: int = 0
    sections_failed: int = 0
    sections_skipped: int = 0
    #: Basis distribution over every fact that carried evidence, via ``summarise()``.
    evidence: EvidenceSummary | None = None
    #: Related objects whose link was derived rather than declared.
    advisory_relationships: int = 0
    reasons: list[str] = Field(default_factory=list)


#: Reserved ``AnalysisFinding.section`` for a finding derived from the normalised relationship view
#: rather than from one reader. Not a step name, and deliberately not disguised as one.
RELATIONSHIP_SECTION = "relationships"


class AnalysisFinding(BaseModel):
    """One thing the analysis established, with how it knows.

    Distinct from :class:`~.findings.Finding` in ``risks``, and deliberately so: a risk is a
    *judgement* that something is wrong, a finding is a *statement of fact* the analysis is prepared
    to defend. ``summary`` carries the same material as prose, which is what a person reads; this is
    the same material attributed, which is what a program reads and what answers "how do you know".

    **This is a mechanical digest, not authored insight.** Each entry is derived from a section that
    ran or from the normalised relationship lists, so it cannot claim more than the envelope already
    justifies. Nothing here is hand-written per analysis.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str
    #: Where it came from: either an :class:`AnalysisStep` ``section`` - so a doubted finding leads
    #: straight to the tool that reproduces it - or the reserved value
    #: :data:`RELATIONSHIP_SECTION` for a finding derived from the normalised ``dependencies`` /
    #: ``consumers`` lists. Those are assembled from several sections, so naming any one of them
    #: would point at a step that did not establish them.
    section: str
    basis: EvidenceBasis
    completeness: EvidenceCompleteness = "complete"
    #: Objects the statement is about, capped so a wide finding does not dominate the reply.
    objects: list[str] = Field(default_factory=list)
    #: The weakest evidence behind it, where the section supplied any.
    evidence: Evidence | None = None


class SkippedSection(BaseModel):
    """A section that produced no answer, and which of the four reasons applies.

    ``empty`` and ``not_applicable`` are deliberately **not** listed here. A section that ran and
    found nothing has answered; a section that does not apply to this subject was never owed. Only
    the four cases where an answer was owed and not delivered are skips, because that is the set a
    caller can act on.
    """

    model_config = ConfigDict(extra="forbid")

    section: str
    tool: str
    status: Literal["unsupported", "connector_required", "skipped_budget", "failed"]
    reason: str


class AnalysisExecution(BaseModel):
    """What the analysis actually did, measured rather than declared.

    ``steps`` is **not** repeated here. It is a published top-level field, and copying it would
    double the largest part of the envelope for no new information; ``step_count`` and
    ``tools_used`` summarise it, and the list itself stays where callers already read it.
    """

    model_config = ConfigDict(extra="forbid")

    #: Granular tools invoked, de-duplicated and ordered. Every one is a registered tool name, so
    #: any section can be re-run on its own.
    tools_used: list[str] = Field(default_factory=list)
    step_count: int = 0
    sections_answered: int = 0
    #: Statements charged to the per-call budget while this analysis ran. ``None`` means no budget
    #: was active to count against - never 0, because "issued no queries" is a different statement
    #: from "nobody was counting". Note that the offline fixtures substitute their own connection
    #: and do not charge, so this reads 0 there and is only meaningful against a real system.
    queries_executed: int | None = None
    duration_ms: int = 0


class AnalysisBudget(BoundedResult):
    """The allowance this call ran inside, and what it spent.

    Reported on success as well as on exhaustion. Previously the spend was visible only when the
    budget ran out, so an analysis that used 4,900 of 5,000 statements looked exactly like one that
    used three - the difference between "this scales" and "this falls over on the next system twice
    the size", invisible until it became a partial answer.
    """

    query_limit: int | None = None
    queries_used: int | None = None
    time_limit_ms: int | None = None
    time_used_ms: int | None = None
    #: True when a bound stopped the analysis short - the budget, or a section's own row cap.
    #: Which of the two it was. The bool could not tell them apart, and they need different
    #: False when no budget was active, so the other fields are absent rather than zero.
    measured: bool = False


class Analysis(BaseModel):
    """A composed answer to one analyst question, in the shape every compound tool returns.

    Subject payloads are typed optional fields rather than a union, so one contract covers all five
    questions. A ``None`` payload is disambiguated by ``steps``: a section that did not apply to
    this subject is recorded ``not_applicable``, one the release cannot report is ``unsupported``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: AnalysisKind
    system: str
    subject: BwObjectRef | None = None
    subject_name: str
    title: str
    #: The answer, as factual sentences. Every sentence must be supported by a section that ran;
    #: this is a summary of what was read, never an interpretation beyond it.
    summary: list[str] = Field(default_factory=list)

    # --- subject payloads, one per section that can apply ---------------------------------
    definition: Provider | None = None
    query: Query | None = None
    query_lineage: QueryLineage | None = None
    query_usage: QueryUsage | None = None
    chain: Chain | None = None
    #: How often the chain actually runs, derived from run history and never from its name (D52).
    #: Reported here rather than on :class:`~.chains.Chain` on purpose: cadence is a *runtime* fact
    #: and ``Chain`` is cached under the long structural TTL, so attaching it there would hold run
    #: history well past the one-hour cap mission Section 3 puts on runtime statistics. The analysis
    #: is not cached, so this is the honest place for it.
    cadence: ChainCadence | None = None
    runtimes: ChainRuntimes | None = None
    load: LoadClosure | None = None
    health: ProviderHealth | None = None
    security: QueryAuthExposure | None = None
    #: The **upstream** graph. Downstream lives in ``impact.graph``, which walks it anyway and adds
    #: the routine-derived consumers; asking for both directions here would traverse every
    #: downstream hop twice - measured on a real DSO, that one redundant call was 21s of 31s.
    lineage: LineageGraph | None = None
    impact: ImpactAnalysis | None = None
    trace: TraceToSource | None = None

    # --- the normalised relationship view -------------------------------------------------
    #: What the subject depends on. Normalised across sections so one list answers "what feeds this"
    #: regardless of which reader established each link.
    dependencies: list[RelatedObject] = Field(default_factory=list)
    #: What depends on the subject, including the routine-embedded consumers BW cannot list.
    consumers: list[RelatedObject] = Field(default_factory=list)

    # --- judgement, always separable from fact --------------------------------------------
    risks: list[Finding] = Field(default_factory=list)
    limitations: list[AnalysisLimitation] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)

    # --- what was established, attributed -------------------------------------------------
    #: The structured form of ``summary``: each statement with the section that established it and
    #: the basis it rests on. A mechanical digest of what the sections and relationship lists
    #: already justify, so it never claims more than the envelope supports.
    findings: list[AnalysisFinding] = Field(default_factory=list)
    #: Distinct evidence behind this answer, de-duplicated by (basis, method). The counts live on
    #: ``confidence.evidence``; this is the list, so "how does it know this" is answerable without
    #: walking every nested payload. Weakest basis first.
    evidence: list[Evidence] = Field(default_factory=list)

    # --- the audit trail ------------------------------------------------------------------
    steps: list[AnalysisStep] = Field(default_factory=list)
    #: What ran, what it cost, and how long it took. Measured, not declared.
    execution: AnalysisExecution = Field(default_factory=AnalysisExecution)
    #: The allowance and the spend, reported whether or not it ran out.
    budget: AnalysisBudget = Field(default_factory=AnalysisBudget)
    #: Sections that owed an answer and did not deliver one, with which of the four reasons applies.
    #: Never includes a section that ran and found nothing.
    sections_skipped: list[SkippedSection] = Field(default_factory=list)
    #: How far the capabilities behind *this* answer have been proven - the weakest across
    #: everything the tool reads, so one unproven reader is not hidden by four proven ones. Read
    #: ``validation_basis`` for which capability set it.
    validation_status: ValidationStatus = "not_validated"
    #: How ``validation_status`` was reached, and which capability is the weakest link. A bare
    #: status is not checkable; this is what makes it so.
    validation_basis: str = ""
    confidence: AnalysisConfidence
    #: True when the per-call budget ran out mid-composition. The sections already gathered are
    #: still returned - four of five is a better answer than none - and the rest are recorded
    #: ``skipped_budget`` rather than reported as empty.
    stopped_on_budget: bool = False

    @model_validator(mode="after")
    def _order_risks(self) -> Analysis:
        """Most severe risk first, and dedupe the relationship lists.

        A relationship can be established twice by two readers - the lineage graph and the impact
        analysis both see a declared downstream edge - and reporting it twice would inflate a count
        a caller takes from these lists. The declared reading is kept over the advisory one.
        """
        self.risks.sort(key=lambda f: severity_rank(f.severity), reverse=True)
        self.dependencies = _dedupe(self.dependencies)
        self.consumers = _dedupe(self.consumers)
        # Weakest evidence first: the inferred entries are what a reader has to weigh, and burying
        # them under the observed ones is how a lower bound gets read as a complete set.
        self.evidence.sort(key=lambda e: (-e.rank, e.method))
        return self

    @property
    def advisory_consumer_count(self) -> int:
        return sum(1 for item in self.consumers if item.advisory)


def _dedupe(items: list[RelatedObject]) -> list[RelatedObject]:
    """One entry per (object, relationship), preferring a declared link over an advisory one."""
    best: dict[tuple[str, str], RelatedObject] = {}
    for item in items:
        key = (item.id, item.relationship)
        current = best.get(key)
        if current is None or (current.advisory and not item.advisory):
            best[key] = item
    return [best[key] for key in sorted(best)]
