"""Validation models: the record of what was actually asked of a real BW system, and whether the
answer was right.

**Why this is a separate artefact from the capability contract.** The contract records what the test
suite measurably touched — code exists, a test exercised it, it ran against a live system. None of
that says the *answer was correct*. "The call returned a lineage graph without erroring" and "the
lineage graph was right" are different claims, and only the second is worth anything to a customer
deciding whether to trust an impact analysis before a transport.

So this module records the second, and the ladder has a rung the contract cannot emit:

```
CODE_SUPPORTED       a reader exists
UNIT_TESTED          the offline suite exercised it against synthetic fixtures
INTEGRATION_TESTED   it ran against a real BW system and returned
REAL_BW_VALIDATED    a recorded scenario's answer was checked against human-verified ground truth
CUSTOMER_VALIDATED   a customer verified it on their own system
```

**`CUSTOMER_VALIDATED` is not ours to award.** It means an external customer, on their own
landscape, confirmed the answer. This project can reach `REAL_BW_VALIDATED` on a reference system
and no further; a validator field records who signed off, and a scenario claiming customer
validation without an external validator is rejected by the model itself rather than by review.

**Correctness is not a boolean.** A lineage answer can be right about every edge it reports and
still be an incomplete set - that is `correct_but_incomplete`, and flattening it to "pass" hides the
exact property that matters about routine-derived lineage. Equally `unverifiable` is a real outcome:
if no ground truth could be established, the scenario proves nothing and must not count as a pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .completeness import BoundedResult
from .evidence import EvidenceBasis, EvidenceCompleteness

#: The validation ladder, weakest first. Mirrors ``models.capability.ValidationStatus`` in meaning;
#: spelled in upper case here because these are the states a validation *record* asserts; keeping
#: the two spellings distinct stops a contract value being pasted into a scenario and vice versa.
ValidationState = Literal[
    "CODE_SUPPORTED",
    "UNIT_TESTED",
    "INTEGRATION_TESTED",
    "REAL_BW_VALIDATED",
    "CUSTOMER_VALIDATED",
]

VALIDATION_STATE_RANK: dict[str, int] = {
    "CODE_SUPPORTED": 0,
    "UNIT_TESTED": 1,
    "INTEGRATION_TESTED": 2,
    "REAL_BW_VALIDATED": 3,
    "CUSTOMER_VALIDATED": 4,
}

#: The state a scenario may reach without an external customer signing off.
MAX_INTERNAL_STATE: ValidationState = "REAL_BW_VALIDATED"

#: States asserting that a human checked the answer. Both therefore require a named verifier, a
#: verification date, and a ground truth recorded as independent - see
#: :meth:`ValidationScenario._require_human_verification`.
_VERIFIED_STATES: frozenset[str] = frozenset({"REAL_BW_VALIDATED", "CUSTOMER_VALIDATED"})

#: How the MCP's answer compared with ground truth.
#:
#:   correct                 matched ground truth on every point checked
#:   correct_but_incomplete  everything reported was right, and the set was known to be short.
#:                           The expected outcome for anything routine-derived, and a pass *only*
#:                           when the answer said so itself.
#:   partially_correct       some points right, at least one wrong
#:   incorrect               contradicted ground truth
#:   unverifiable            no ground truth could be established, so the scenario proves nothing
#:   not_run                 recorded but not yet executed
Correctness = Literal[
    "correct",
    "correct_but_incomplete",
    "partially_correct",
    "incorrect",
    "unverifiable",
    "not_run",
]

#: Correctness values that count as a pass. ``correct_but_incomplete`` counts only because the
#: envelope must have declared the incompleteness - asserted by :class:`ValidationScenario`.
PASSING: frozenset[str] = frozenset({"correct", "correct_but_incomplete"})

#: How well the answer's own evidence held up, judged separately from whether it was right. An
#: answer can be correct with weak evidence (lucky) or wrong with strong evidence (a reasoning bug),
#: and the two failures need different fixes.
EvidenceQuality = Literal["cited", "partial", "absent", "misattributed"]

#: Evidence qualities that disqualify a verified state however right the answer was.
#:
#: ``misattributed`` is the pointed one, and it is defect D10's exact shape: an edge read out of
#: SYS.OBJECT_DEPENDENCIES that described itself as parsed out of ABAP. The *answer* was correct.
#: What was wrong was the account of how it was obtained - and REQ-18 makes that account the thing
#: the product's trustworthiness rests on, so a scenario cannot be held up as validating the server
#: while recording that the server misdescribed its own reasoning. ``absent`` fails for the weaker
#: reason that there is nothing to have checked.
_DISQUALIFYING_EVIDENCE: frozenset[str] = frozenset({"absent", "misattributed"})

#: Names that are not a human verifier. A verified state asserts a *person* checked the answer, and
#: the one thing that cannot do the checking is the thing under test. Matched on the whole
#: normalised name, not as a substring, so a real person is not caught by coincidence.
_NOT_A_HUMAN: frozenset[str] = frozenset(
    {
        "mcp-server-sapbw",
        "mcp server sapbw",
        "sapbw",
        "the server",
        "this server",
        "this project",
        "the project",
        "kiro",
        "ai",
        "the ai",
        "assistant",
        "the assistant",
        "agent",
        "the agent",
        "self",
        "n/a",
        "na",
        "none",
        "unknown",
        "tbd",
        "-",
    }
)


def _is_human_name(value: str | None) -> bool:
    """Whether ``value`` plausibly names a person rather than this software or a placeholder."""
    normalised = " ".join((value or "").strip().lower().split())
    return bool(normalised) and normalised not in _NOT_A_HUMAN


class GroundTruth(BaseModel):
    """What a human established independently, and how — the thing the MCP is measured against.

    ``method`` is the load-bearing field. A ground truth taken from the same metadata table the
    server reads proves only that the SQL round-tripped; one taken from BW's own transaction UI, or
    from a person who maintains the object, is independent evidence. Both are recorded, and which it
    was is never left implicit.
    """

    model_config = ConfigDict(extra="forbid")

    #: How the truth was established, e.g. "RSA1 data-flow display", "SE16 on RSTRAN by a BW
    #: developer", "the object's owner confirmed by email", "BEx Query Designer".
    method: str = Field(min_length=1)
    #: True when the method does not read the same metadata this server reads. A dependent ground
    #: truth still catches SQL and decoding errors; it cannot catch a wrong assumption about what a
    #: table means, because it shares the assumption.
    independent: bool
    #: The expected answer, in whatever form the scenario checks: a set of names, a count, a value.
    expected: str = Field(min_length=1)
    established_by: str | None = None
    #: When the human established it. This **is** the verification date, and a verified state
    #: requires it: undated verification cannot be tied to a system state, and BW metadata changes
    #: with every transport. Kept here rather than on the scenario so it stays attached to the thing
    #: it dates.
    established_at: datetime | None = None
    #: Whether the human wrote their reading down **before** being shown this server's answer.
    #:
    #: A second axis of independence, and not the same as ``independent``. That one is about
    #: *method* - whether the truth came from the same tables the server reads. This one is about
    #: *order*: a verifier who sees our answer first is no longer checking it blind, they are
    #: agreeing with it, and the two failure modes are unrelated. A ground truth can be
    #: method-independent and still not blind.
    #:
    #: Recorded explicitly rather than inferred, because the timestamps can be absent - but where
    #: both are present the model checks the claim against them rather than taking it on trust.
    established_before_answer: bool | None = None
    notes: str | None = None


class ScenarioMeasurement(BoundedResult):
    """What the call cost, taken from the compound envelope rather than a stopwatch."""

    #: From ``Analysis.execution.queries_executed``. ``None`` when nothing counted — never 0, which
    #: would claim the call issued no statements.
    sap_queries_executed: int | None = None
    response_time_ms: int | None = None
    payload_bytes: int | None = None
    # ``truncated`` and ``completeness`` come from BoundedResult: whether a bound stopped the answer
    # short, and which one. Copied from ``Analysis.budget``.


class ValidationScenario(BaseModel):
    """One real-BW question, its ground truth, the answer given, and the verdict.

    The model enforces the two rules that a review process would otherwise have to remember:
    ``CUSTOMER_VALIDATED`` requires an external validator, and ``correct_but_incomplete`` only
    passes if the answer itself declared the incompleteness.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1, pattern=r"^S\d{2}(-[a-z0-9-]+)?$")
    title: str = Field(min_length=1)
    #: The analyst question in plain words — what someone actually wanted to know.
    question: str = Field(min_length=1)
    #: The MCP capability under test, e.g. ``bw_analyze_object``.
    tool: str = Field(min_length=1)
    #: Profile alias, never a host name.
    bw_system: str = Field(min_length=1)
    bw_release: str = Field(min_length=1)
    #: BW objects involved. Real technical names live only in the git-ignored results file, never in
    #: a committed fixture — the customer-metadata rule applies to validation records too.
    objects: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)

    ground_truth: GroundTruth | None = None
    #: What the MCP answered, summarised to the points being checked.
    actual_result: str | None = None
    correctness: Correctness = "not_run"
    evidence_quality: EvidenceQuality = "absent"
    #: The basis the answer claimed for its central fact, and whether it declared a bound set. These
    #: are copied from the response, so a scenario records what the server *said* about its own
    #: certainty, which is itself under test.
    claimed_basis: EvidenceBasis | None = None
    claimed_completeness: EvidenceCompleteness | None = None

    measurement: ScenarioMeasurement = Field(default_factory=ScenarioMeasurement)
    state: ValidationState = "CODE_SUPPORTED"
    #: Who verified the answer. Required for ``CUSTOMER_VALIDATED`` and expected to name a person or
    #: role, not this project.
    human_expert: str | None = None
    #: True only when the validator is outside this project.
    external_validator: bool = False

    limitations: list[str] = Field(default_factory=list)
    #: Defects this scenario surfaced. A scenario that found a bug is worth more than one that
    #: passed, and this is where that shows.
    defects_found: list[str] = Field(default_factory=list)
    #: Regression tests added because of this scenario. The standing rule: a wrong assumption about
    #: BW metadata gets a generalised rule and a test, not a one-off patch.
    regression_tests: list[str] = Field(default_factory=list)
    executed_at: datetime | None = None

    @model_validator(mode="after")
    def _enforce_claims(self) -> ValidationScenario:
        if self.state == "CUSTOMER_VALIDATED" and not self.external_validator:
            raise ValueError(
                f"{self.scenario_id} claims CUSTOMER_VALIDATED without an external validator. That "
                "state means a customer confirmed it on their own system; this project cannot "
                "award it to itself."
            )
        if self.state == "REAL_BW_VALIDATED" and self.correctness not in PASSING:
            raise ValueError(
                f"{self.scenario_id} claims REAL_BW_VALIDATED with correctness "
                f"{self.correctness!r}. That state requires the answer to have been checked and "
                "found right; running without error is INTEGRATION_TESTED."
            )
        if self.state in _VERIFIED_STATES:
            self._require_human_verification()
        if (
            self.correctness == "correct_but_incomplete"
            and self.claimed_completeness != "lower_bound"
        ):
            raise ValueError(
                f"{self.scenario_id} is correct_but_incomplete but the answer did not declare a "
                "lower bound. An incomplete answer presented as complete is a defect, not a pass."
            )
        return self

    def _require_human_verification(self) -> None:
        """Everything a verified state needs beyond the call having returned.

        Defect V1 from the pre-validation assessment: the state required a passing verdict and a
        recorded ground truth, but not a *person*. So it could be claimed on a truth nobody is named
        as having established, on no date, by a method reading the same tables this server reads -
        which is not verification, it is a round trip.

        Each condition raises its own message so a rejection says which one failed rather than
        leaving the author to guess.
        """
        truth = self.ground_truth
        if truth is None:
            raise ValueError(
                f"{self.scenario_id} claims {self.state} with no ground truth recorded, so "
                "there is nothing the answer was checked against."
            )
        if not (self.human_expert or "").strip():
            raise ValueError(
                f"{self.scenario_id} claims {self.state} with no named human_expert. A verified "
                "state asserts that a person checked the answer, so that person has to be named."
            )
        if truth.established_at is None:
            raise ValueError(
                f"{self.scenario_id} claims {self.state} with no verification date "
                "(ground_truth.established_at). Undated verification cannot be tied to a system "
                "state, and BW metadata changes with every transport."
            )
        if not truth.independent:
            raise ValueError(
                f"{self.scenario_id} claims {self.state} on a ground truth recorded as NOT "
                "independent. A truth read from the same metadata this server reads proves the SQL "
                "round-tripped; it cannot catch a wrong assumption about what a table means, "
                "because it shares the assumption. Record an independent method, or keep the "
                "scenario at INTEGRATION_TESTED."
            )
        self._require_blind_verification(truth)
        if not _is_human_name(self.human_expert):
            raise ValueError(
                f"{self.scenario_id} claims {self.state} with human_expert "
                f"{self.human_expert!r}, which does not name a person. The one thing that cannot "
                "verify this server's answer is this server, and a placeholder is not a verifier."
            )
        if self.evidence_quality in _DISQUALIFYING_EVIDENCE:
            raise ValueError(
                f"{self.scenario_id} claims {self.state} with evidence_quality "
                f"{self.evidence_quality!r}. A correct answer that misdescribes how it was "
                "obtained "
                "is defect D10's shape, and REQ-18 makes that account the thing the product rests "
                "on - so it cannot be cited as validating the server. Record the evidence defect "
                "and keep the scenario at INTEGRATION_TESTED."
            )

    def _require_blind_verification(self, truth: GroundTruth) -> None:
        """The verifier must have written their reading down before seeing ours.

        The second axis of independence, and the one the form asks about in words but nothing
        checked: someone shown our answer first is agreeing with it, not checking it. Method
        independence does not cover this - a truth can come from RSA1 and still be recorded after
        reading our output.

        Where both timestamps exist the claim is checked *against* them, so a record cannot assert
        blindness the dates contradict. That is the part worth having: a flag on its own is only as
        good as the author's memory.
        """
        if truth.established_before_answer is None:
            raise ValueError(
                f"{self.scenario_id} claims {self.state} without recording whether the ground "
                "truth was established before this server's answer was shown "
                "(ground_truth.established_before_answer). A verifier who saw our answer first is "
                "agreeing with it rather than checking it, and that has to be on the record."
            )
        if not truth.established_before_answer:
            raise ValueError(
                f"{self.scenario_id} claims {self.state} on a ground truth established *after* the "
                "answer was shown. The comparison still has value, but it is not blind, so it "
                "cannot support a verified state. Keep the scenario at INTEGRATION_TESTED and say "
                "so in the notes."
            )
        if (
            self.executed_at is not None
            and truth.established_at is not None
            and truth.established_at > self.executed_at
        ):
            raise ValueError(
                f"{self.scenario_id} records the ground truth as established before the "
                "answer, but "
                f"its timestamps say otherwise: established_at {truth.established_at.isoformat()} "
                f"is after executed_at {self.executed_at.isoformat()}. One of the two is "
                "wrong, and "
                "the model will not pick which."
            )

    @property
    def passed(self) -> bool:
        return self.correctness in PASSING

    @property
    def rank(self) -> int:
        return VALIDATION_STATE_RANK.get(self.state, 0)


class ValidationSuite(BaseModel):
    """Every recorded scenario, and what they add up to.

    Counts are computed rather than stated, so a summary cannot drift from the scenarios beneath it.
    """

    model_config = ConfigDict(extra="forbid")

    #: Build the scenarios were run against, so a result can be tied to a version.
    server_version: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    scenarios: list[ValidationScenario] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @property
    def by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for scenario in self.scenarios:
            counts[scenario.state] = counts.get(scenario.state, 0) + 1
        return counts

    @property
    def by_correctness(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for scenario in self.scenarios:
            counts[scenario.correctness] = counts.get(scenario.correctness, 0) + 1
        return counts

    @property
    def executed(self) -> int:
        return sum(1 for s in self.scenarios if s.correctness != "not_run")

    @property
    def real_bw_validated(self) -> int:
        return sum(
            1 for s in self.scenarios if s.rank >= VALIDATION_STATE_RANK["REAL_BW_VALIDATED"]
        )

    @property
    def defect_count(self) -> int:
        return sum(len(s.defects_found) for s in self.scenarios)

    def capability_states(self) -> dict[str, ValidationState]:
        """Best state reached per tool, so the suite can feed a support statement.

        Best rather than worst, deliberately and unlike the compound envelope's weakest-link rule:
        here the question is "has this tool ever been validated against a real system", and one
        passing scenario answers it. Whether *every* question it can answer was validated is what
        the scenario count says.
        """
        best: dict[str, ValidationState] = {}
        for scenario in self.scenarios:
            current = best.get(scenario.tool)
            if (
                current is None
                or VALIDATION_STATE_RANK[scenario.state] > VALIDATION_STATE_RANK[current]
            ):
                best[scenario.tool] = scenario.state
        return best
