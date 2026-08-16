"""The validation record's rules, enforced by the model rather than by review.

Two claims are the ones a validation exercise is most tempted to fudge, so neither is left to a
reviewer's memory: awarding `CUSTOMER_VALIDATED` to yourself, and calling an incomplete answer a
pass without the answer having said it was incomplete.
"""

from __future__ import annotations

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


def truth(**kwargs: object) -> GroundTruth:
    base: dict[str, object] = {
        "method": "RSA1 data-flow display, read by a BW developer",
        "independent": True,
        "expected": "three transformations feed the target",
    }
    base.update(kwargs)
    return GroundTruth(**base)  # type: ignore[arg-type]


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
        scenario(
            state="CUSTOMER_VALIDATED",
            correctness="correct",
            ground_truth=truth(),
            human_expert="the maintainer",
            external_validator=False,
        )


def test_customer_validated_is_accepted_with_an_external_validator() -> None:
    entry = scenario(
        state="CUSTOMER_VALIDATED",
        correctness="correct",
        ground_truth=truth(),
        human_expert="customer BW lead",
        external_validator=True,
    )
    assert entry.rank == VALIDATION_STATE_RANK["CUSTOMER_VALIDATED"]


def test_real_bw_validated_requires_the_answer_to_have_been_right() -> None:
    """Running without error is INTEGRATION_TESTED. This rung means the answer was checked."""
    with pytest.raises(ValidationError, match="requires the answer to have been checked"):
        scenario(state="REAL_BW_VALIDATED", correctness="partially_correct", ground_truth=truth())


def test_real_bw_validated_requires_recorded_ground_truth() -> None:
    """Without ground truth there is nothing the answer was compared against."""
    with pytest.raises(ValidationError, match="no ground truth"):
        scenario(state="REAL_BW_VALIDATED", correctness="correct")


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
            scenario(
                scenario_id="S01",
                correctness="correct",
                ground_truth=truth(),
                state="REAL_BW_VALIDATED",
            ),
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
            scenario(
                scenario_id="S02",
                tool="bw_analyze_object",
                correctness="correct",
                ground_truth=truth(),
                state="REAL_BW_VALIDATED",
            ),
        ],
    )
    assert suite.capability_states() == {"bw_analyze_object": "REAL_BW_VALIDATED"}


def test_an_empty_suite_claims_nothing() -> None:
    suite = ValidationSuite(server_version="0.1.0")
    assert suite.executed == 0
    assert suite.real_bw_validated == 0
    assert suite.capability_states() == {}
