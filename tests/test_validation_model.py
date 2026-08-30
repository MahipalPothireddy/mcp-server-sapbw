"""The validation record's rules, enforced by the model rather than by review.

Two claims are the ones a validation exercise is most tempted to fudge, so neither is left to a
reviewer's memory: awarding `CUSTOMER_VALIDATED` to yourself, and calling an incomplete answer a
pass without the answer having said it was incomplete.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mcp_server_sapbw.models.validation import (
    PASSING,
    VALIDATION_STATE_RANK,
    GroundTruth,
    ScenarioMeasurement,
    ValidationScenario,
    ValidationSuite,
)

#: A ground truth good enough to support a verified state: independent, and dated.
_VERIFIED_AT = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)


def truth(**kwargs: object) -> GroundTruth:
    base: dict[str, object] = {
        "method": "RSA1 data-flow display, read by a BW developer",
        "independent": True,
        "expected": "three transformations feed the target",
        "established_by": "BW developer",
        "established_at": _VERIFIED_AT,
        "established_before_answer": True,
    }
    base.update(kwargs)
    return GroundTruth(**base)  # type: ignore[arg-type]


def verified(**kwargs: object) -> ValidationScenario:
    """A scenario satisfying every condition for REAL_BW_VALIDATED, for negative tests to break."""
    base: dict[str, object] = {
        "state": "REAL_BW_VALIDATED",
        "correctness": "correct",
        "ground_truth": truth(),
        "human_expert": "A. Verifier, BW developer",
        "evidence_quality": "cited",
    }
    base.update(kwargs)
    return scenario(**base)


def scenario(**kwargs: object) -> ValidationScenario:
    base: dict[str, object] = {
        "scenario_id": "S01",
        "title": "Explain a provider",
        "question": "What feeds this provider and what depends on it?",
        "tool": "bw_analyze_object",
        "bw_system": "qa",
        "bw_release": "BW 7.50",
    }
    base.update(kwargs)
    return ValidationScenario(**base)  # type: ignore[arg-type]


# --- the ladder ----------------------------------------------------------------------------


def test_the_ladder_is_ordered() -> None:
    ranks = [
        VALIDATION_STATE_RANK[s]
        for s in (
            "CODE_SUPPORTED",
            "UNIT_TESTED",
            "INTEGRATION_TESTED",
            "REAL_BW_VALIDATED",
            "CUSTOMER_VALIDATED",
        )
    ]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_a_new_scenario_starts_at_the_bottom_and_unrun() -> None:
    """A recorded intention must not read as a result."""
    entry = scenario()
    assert entry.state == "CODE_SUPPORTED"
    assert entry.correctness == "not_run"
    assert entry.passed is False


# --- the two rules the model enforces -------------------------------------------------------


def test_customer_validated_requires_an_external_validator() -> None:
    """This project cannot award itself a customer's sign-off."""
    with pytest.raises(ValidationError, match="external validator"):
        verified(
            state="CUSTOMER_VALIDATED", human_expert="the maintainer", external_validator=False
        )


def test_customer_validated_is_accepted_with_an_external_validator() -> None:
    entry = verified(
        state="CUSTOMER_VALIDATED",
        human_expert="customer BW lead",
        external_validator=True,
    )
    assert entry.rank == VALIDATION_STATE_RANK["CUSTOMER_VALIDATED"]


def test_real_bw_validated_requires_the_answer_to_have_been_right() -> None:
    """Running without error is INTEGRATION_TESTED. This rung means the answer was checked."""
    with pytest.raises(ValidationError, match="requires the answer to have been checked"):
        verified(correctness="partially_correct")


def test_real_bw_validated_requires_recorded_ground_truth() -> None:
    """Without ground truth there is nothing the answer was compared against."""
    with pytest.raises(ValidationError, match="no ground truth"):
        verified(ground_truth=None)


# --- defect V1: a verified state asserts that a *person* checked the answer -------------------
#
# Before this, REAL_BW_VALIDATED needed only a passing verdict and a recorded ground truth. It could
# therefore be claimed on a truth nobody is named as having established, on no date, by a method
# reading the same tables this server reads - which is a round trip, not verification.


def test_real_bw_validated_is_rejected_without_a_named_human_expert() -> None:
    with pytest.raises(ValidationError, match="no named human_expert"):
        verified(human_expert=None)


def test_real_bw_validated_is_rejected_when_the_expert_name_is_blank() -> None:
    """An empty string is not a name, and would otherwise pass a presence check."""
    with pytest.raises(ValidationError, match="no named human_expert"):
        verified(human_expert="   ")


def test_real_bw_validated_is_rejected_without_a_verification_date() -> None:
    """Undated verification cannot be tied to a system state; BW metadata moves with transports."""
    with pytest.raises(ValidationError, match="no verification date"):
        verified(ground_truth=truth(established_at=None))


def test_real_bw_validated_is_rejected_when_verification_was_not_independent() -> None:
    """A truth from the same tables shares our assumptions, so it cannot test them."""
    with pytest.raises(ValidationError, match="NOT independent"):
        verified(ground_truth=truth(method="SE16 on RSTRAN", independent=False))


def test_real_bw_validated_succeeds_only_with_every_condition_satisfied() -> None:
    """The positive case, so the rules are proven to be satisfiable and not merely strict."""
    entry = verified()
    assert entry.state == "REAL_BW_VALIDATED"
    assert entry.passed is True
    assert entry.human_expert
    assert entry.ground_truth is not None
    assert entry.ground_truth.established_at == _VERIFIED_AT
    assert entry.ground_truth.independent is True


def test_the_same_conditions_apply_to_customer_validated() -> None:
    """The stricter state cannot be reachable by a weaker route than the one below it."""
    with pytest.raises(ValidationError, match="no named human_expert"):
        verified(state="CUSTOMER_VALIDATED", external_validator=True, human_expert=None)
    with pytest.raises(ValidationError, match="no verification date"):
        verified(
            state="CUSTOMER_VALIDATED",
            external_validator=True,
            ground_truth=truth(established_at=None),
        )
    with pytest.raises(ValidationError, match="NOT independent"):
        verified(
            state="CUSTOMER_VALIDATED",
            external_validator=True,
            ground_truth=truth(independent=False),
        )


def test_an_unverified_state_needs_none_of_this() -> None:
    """The rules gate the *claim*, not the recording. An in-progress scenario stays writable."""
    entry = scenario(
        state="INTEGRATION_TESTED",
        correctness="correct",
        ground_truth=truth(established_at=None, independent=False),
    )
    assert entry.state == "INTEGRATION_TESTED"
    assert entry.human_expert is None


def test_an_incomplete_answer_passes_only_if_it_declared_itself_incomplete() -> None:
    """The distinction that matters for routine-derived lineage.

    A lower-bound answer that says so is a pass; the same answer presented as complete is a defect.
    """
    with pytest.raises(ValidationError, match="did not declare a lower bound"):
        scenario(
            correctness="correct_but_incomplete",
            claimed_completeness="complete",
            ground_truth=truth(),
        )

    ok = scenario(
        correctness="correct_but_incomplete",
        claimed_completeness="lower_bound",
        claimed_basis="inferred",
        ground_truth=truth(),
    )
    assert ok.passed is True


def test_unverifiable_is_not_a_pass() -> None:
    """No ground truth established means the scenario proves nothing, either way."""
    entry = scenario(correctness="unverifiable", ground_truth=truth())
    assert entry.passed is False
    assert "unverifiable" not in PASSING


@pytest.mark.parametrize("bad", ["1", "SX", "scenario-1", "s01", "S1"])
def test_scenario_ids_are_constrained(bad: str) -> None:
    """A free-text id makes the matrix unjoinable to anything."""
    with pytest.raises(ValidationError):
        scenario(scenario_id=bad)


# --- ground truth ---------------------------------------------------------------------------


def test_ground_truth_records_whether_it_was_independent() -> None:
    """A truth read from the same table proves the SQL round-tripped, not that the reading is
    right."""
    dependent = truth(method="SE16 on RSTRAN", independent=False)
    assert dependent.independent is False
    assert truth().independent is True


def test_ground_truth_needs_a_method_and_an_expectation() -> None:
    with pytest.raises(ValidationError):
        GroundTruth(method="", independent=True, expected="x")
    with pytest.raises(ValidationError):
        GroundTruth(method="RSA1", independent=True, expected="")


# --- measurement ----------------------------------------------------------------------------


def test_query_count_absent_is_distinguishable_from_zero() -> None:
    """Same rule as the compound envelope: nobody counting is not the same as no queries."""
    assert ScenarioMeasurement().sap_queries_executed is None
    assert ScenarioMeasurement(sap_queries_executed=0).sap_queries_executed == 0


# --- the suite ------------------------------------------------------------------------------


def test_suite_counts_are_computed_not_stated() -> None:
    """A stated summary drifts from the scenarios beneath it; a computed one cannot."""
    suite = ValidationSuite(
        server_version="0.1.0",
        scenarios=[
            verified(scenario_id="S01"),
            scenario(scenario_id="S02", correctness="incorrect", defects_found=["wrong join"]),
            scenario(scenario_id="S03"),
        ],
    )
    assert suite.executed == 2
    assert suite.real_bw_validated == 1
    assert suite.defect_count == 1
    assert suite.by_correctness == {"correct": 1, "incorrect": 1, "not_run": 1}
    assert suite.by_state["CODE_SUPPORTED"] == 2


def test_capability_state_is_the_best_reached_per_tool() -> None:
    """Best, unlike the compound envelope's weakest-link rule - and the docstring says why."""
    suite = ValidationSuite(
        server_version="0.1.0",
        scenarios=[
            scenario(scenario_id="S01", tool="bw_analyze_object"),
            verified(scenario_id="S02", tool="bw_analyze_object"),
        ],
    )
    assert suite.capability_states() == {"bw_analyze_object": "REAL_BW_VALIDATED"}


def test_an_empty_suite_claims_nothing() -> None:
    suite = ValidationSuite(server_version="0.1.0")
    assert suite.executed == 0
    assert suite.real_bw_validated == 0
    assert suite.capability_states() == {}


# --- V1: the conditions the model did not previously enforce -------------------------------


def test_a_verified_state_requires_the_verification_to_have_been_blind() -> None:
    """Whether the truth was written down before our answer was shown has to be on the record.

    The second axis of independence, and the one nothing checked. ``independent`` is about
    *method* - did the truth come from the same tables the server reads. This is about
    *order*: a verifier shown
    our answer first is agreeing with it, not checking it, and a ground truth can be
    method-independent while still not being blind.
    """
    with pytest.raises(ValidationError, match="established before this server's answer"):
        verified(ground_truth=truth(established_before_answer=None))


def test_a_verification_made_after_seeing_our_answer_is_not_a_verified_state() -> None:
    """It still has value. It just cannot support the rung that asserts the answer was checked."""
    with pytest.raises(ValidationError, match="not blind"):
        verified(ground_truth=truth(established_before_answer=False))


def test_the_blindness_claim_is_checked_against_the_timestamps() -> None:
    """A flag on its own is only as good as the author's memory, so the dates get a vote.

    This is the part worth having over a bare checkbox: a record cannot assert it was blind
    while its
    own timestamps say the truth was established after the call ran.
    """
    with pytest.raises(ValidationError, match="timestamps say otherwise"):
        verified(
            ground_truth=truth(established_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC)),
            executed_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        )


def test_consistent_timestamps_are_accepted() -> None:
    """The positive case, so the rule is satisfiable rather than merely strict."""
    entry = verified(
        ground_truth=truth(established_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC)),
        executed_at=datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
    )
    assert entry.state == "REAL_BW_VALIDATED"


def test_missing_timestamps_do_not_block_a_declared_blind_verification() -> None:
    """``executed_at`` is optional, and absence must not be read as a contradiction."""
    entry = verified(executed_at=None)
    assert entry.state == "REAL_BW_VALIDATED"


@pytest.mark.parametrize(
    "impostor",
    [
        "mcp-server-sapbw",
        "Kiro",
        "the server",
        "THIS PROJECT",
        "assistant",
        "AI",
        "n/a",
        "TBD",
        "-",
    ],
)
def test_the_thing_under_test_cannot_be_its_own_verifier(impostor: str) -> None:
    """A verified state asserts a person checked the answer. Software cannot, nor can a placeholder.

    The name was previously only required to be non-blank, so ``human_expert="the server"``
    satisfied
    it - which is the claim this rung exists to make impossible.
    """
    with pytest.raises(ValidationError, match="does not name a person"):
        verified(human_expert=impostor)


def test_a_real_name_is_accepted_even_when_it_contains_a_reserved_word() -> None:
    """Matched on the whole name, not as a substring, so a person is not caught by coincidence."""
    entry = verified(human_expert="Ai Nguyen, BW developer")
    assert entry.human_expert is not None


@pytest.mark.parametrize("quality", ["absent", "misattributed"])
def test_a_verified_state_is_rejected_on_disqualifying_evidence(quality: str) -> None:
    """``misattributed`` is defect D10's shape, and disqualifies however right the answer was.

    D10 was an edge read out of SYS.OBJECT_DEPENDENCIES that described itself as parsed out of ABAP.
    The answer was correct; the account of how it was obtained was not. REQ-18 makes that account
    the thing the product's trustworthiness rests on, so a scenario recording it cannot at the same
    time be cited as validating the server.
    """
    with pytest.raises(ValidationError, match="evidence_quality"):
        verified(evidence_quality=quality)


@pytest.mark.parametrize("quality", ["cited", "partial"])
def test_evidence_that_was_actually_checked_is_accepted(quality: str) -> None:
    entry = verified(evidence_quality=quality)
    assert entry.state == "REAL_BW_VALIDATED"


def test_none_of_the_new_rules_apply_below_a_verified_state() -> None:
    """They gate the *claim*, not the recording. An in-progress scenario stays writable."""
    entry = scenario(
        state="INTEGRATION_TESTED",
        correctness="correct",
        human_expert="the server",
        evidence_quality="misattributed",
        ground_truth=truth(established_before_answer=False),
    )
    assert entry.state == "INTEGRATION_TESTED"
    assert entry.passed is True


def test_every_verified_condition_has_its_own_message() -> None:
    """A rejection has to say which condition failed, or the author is left guessing.

    Asserted rather than assumed: one shared message across six conditions would make the model
    strict and unhelpful at the same time.
    """
    cases: dict[str, dict[str, object]] = {
        "no ground truth": {"ground_truth": None},
        "no named human_expert": {"human_expert": None},
        "no verification date": {"ground_truth": truth(established_at=None)},
        "NOT independent": {"ground_truth": truth(independent=False)},
        "established before this server's answer": {
            "ground_truth": truth(established_before_answer=None)
        },
        "does not name a person": {"human_expert": "kiro"},
        "evidence_quality": {"evidence_quality": "absent"},
    }
    seen: set[str] = set()
    for expected, override in cases.items():
        # `match=` already pins that this condition produced its own wording; the set then pins that
        # no two conditions produced the *same* wording.
        with pytest.raises(ValidationError, match=expected) as excinfo:
            verified(**override)
        seen.add(str(excinfo.value))
    assert len(seen) == len(cases), "two conditions share a rejection message"
