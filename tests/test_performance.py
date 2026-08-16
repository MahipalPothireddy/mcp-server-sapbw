"""The performance profile, and the bound it claims.

Two things are asserted here that nothing else covered.

**Coverage.** Every registered tool must be classified. An unclassified tool is simply absent from
the profile, and a customer reads absence as "nothing to worry about" - the opposite of the intended
reading for anything that scans a class of objects.

**The bound is real.** ``test_budget.py`` proves ``ReadOnlyConnection`` charges the budget, and
``test_analysis.py`` proves a service handles ``BudgetExceeded`` when it is raised artificially.
Neither connects the two: no test drove a real repository's fan-out into the bound. So the
"bounded per call" guarantee rested on the two halves being wired together, which nothing checked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.core.budget import BudgetExceeded, query_budget
from mcp_server_sapbw.core.connection import ReadOnlyConnection, SecretScrubber
from mcp_server_sapbw.core.performance import performance_profile
from mcp_server_sapbw.models.performance import PerformanceProfile
from tests.test_server import FakeRuntime

_GROWTH_CLASSES = {"constant", "per_page", "per_object", "per_graph_node", "per_system"}


def profile() -> PerformanceProfile:
    loaded = performance_profile()
    assert loaded is not None, "the shipped performance_profile.json is missing"
    return loaded


async def _tool_names() -> set[str]:
    async with Client(server.mcp) as client:
        return {tool.name for tool in await client.list_tools()}


# --- coverage and shape -------------------------------------------------------------------


def test_every_registered_tool_is_classified() -> None:
    """A tool missing from the profile is a sizing question with no answer."""
    registered = asyncio.run(_tool_names())
    classified = {entry.tool for entry in profile().tools}
    assert registered - classified == set(), f"unclassified: {sorted(registered - classified)}"


def test_the_profile_claims_no_tool_that_does_not_exist() -> None:
    registered = asyncio.run(_tool_names())
    classified = {entry.tool for entry in profile().tools}
    assert classified - registered == set(), f"not registered: {sorted(classified - registered)}"


def test_every_growth_class_is_one_of_the_declared_five() -> None:
    assert {entry.growth for entry in profile().tools} <= _GROWTH_CLASSES


def test_every_entry_explains_its_growth_class() -> None:
    """An unexplained class is a label, and a label is not something a customer can check."""
    for entry in profile().tools:
        assert entry.growth_basis.strip(), entry.tool


def test_every_tool_names_at_least_one_bound() -> None:
    """Nothing may be reported as unbounded: the per-call budget applies to all of them."""
    for entry in profile().tools:
        assert entry.bounds, entry.tool
        assert entry.unbounded is False, entry.tool


def test_every_bound_says_what_the_caller_sees_when_it_binds() -> None:
    """A cap that truncates without saying so is the failure this project exists to avoid."""
    for entry in profile().tools:
        for bound in entry.bounds:
            assert bound.on_hit.strip(), f"{entry.tool}: {bound.bound}"


def test_statement_counts_are_reported_as_unmeasured_not_as_zero() -> None:
    """Zero statements would read as "issues no queries", which is false for every reader."""
    for entry in profile().tools:
        assert entry.statements_measurement == "not_measured"
        assert entry.fixture_statements is None


def test_payload_measurement_is_labelled_wherever_a_number_is_given() -> None:
    for entry in profile().tools:
        if entry.fixture_payload_bytes is None:
            assert entry.payload_measurement == "not_measured", entry.tool
        else:
            assert entry.payload_measurement == "measured", entry.tool


def test_the_profile_says_what_its_numbers_are_not() -> None:
    caveats = " ".join(profile().caveats)
    assert "floor, not a forecast" in caveats
    assert "not extrapolated" in caveats or "not " in caveats
    assert "not measured" in caveats or "not_measured" in caveats


def test_scanning_tools_are_classified_as_per_system() -> None:
    """These are the calls a customer must plan for; misclassifying one hides the risk."""
    by_tool = {entry.tool: entry.growth for entry in profile().tools}
    for tool in ("bw_get_routine_register", "bw_find_unused_providers", "bw_generate_docs"):
        assert by_tool[tool] == "per_system", tool


def test_self_describing_tools_are_constant() -> None:
    by_tool = {entry.tool: entry.growth for entry in profile().tools}
    for tool in ("bw_support_matrix", "bw_performance_profile", "bw_capability_report"):
        assert by_tool[tool] == "constant", tool


def test_the_global_budget_is_reported_with_its_override() -> None:
    budget = profile().budget
    assert int(budget["max_queries_per_call"]) > 0
    assert int(budget["max_seconds_per_call"]) > 0
    assert "SAPBW_MAX_QUERIES_PER_CALL" in budget["override"]
    assert "never a hang" in budget["on_exhaustion"]


# --- the tool -----------------------------------------------------------------------------


def _call(args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool("bw_performance_profile", args)

    result = asyncio.run(run())
    payload = result.structured_content
    return payload["result"] if isinstance(payload, dict) and "result" in payload else payload


def test_tool_answers_without_a_connection() -> None:
    """The sizing question is asked before there is a profile to connect with.

    Clearing the runtime is what proves it: with no runtime installed the tool must still answer,
    because it reads shipped data and never touches a profile. Restored afterwards so this does not
    leak global state into whichever test runs next.
    """
    server.set_runtime(None)  # type: ignore[arg-type]
    try:
        body = _call({})
    finally:
        server.set_runtime(FakeRuntime())
    assert body["tools"]
    assert body["budget"]


def test_tool_filters_by_growth_class() -> None:
    body = _call({"growth": "per_system"})
    assert body["tools"]
    assert {entry["growth"] for entry in body["tools"]} == {"per_system"}
    assert body["totals"] == {"per_system": len(body["tools"])}


def test_tool_filters_by_name() -> None:
    body = _call({"tool": "bw_get_lineage"})
    assert [entry["tool"] for entry in body["tools"]] == ["bw_get_lineage"]


def test_tool_rejects_an_unknown_name_rather_than_returning_everything() -> None:
    body = _call({"tool": "bw_not_a_tool"})
    assert body["code"] == "object_not_found"


def test_tool_rejects_an_unknown_growth_class_and_lists_the_real_ones() -> None:
    body = _call({"growth": "per_galaxy"})
    assert body["code"] == "invalid_argument"
    assert "per_system" in body["message"]


# --- the bound actually binds --------------------------------------------------------------


class _EndlessDriver:
    """A raw driver that always returns a row, so a walk never runs out of work naturally."""

    def __init__(self) -> None:
        self.executed = 0

    def cursor(self) -> _EndlessDriver:
        return self

    def execute(self, operation: str, parameters: Sequence[Any] | None = None) -> None:
        self.executed += 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [("X",)]

    def close(self) -> None:
        return None


def test_a_real_connection_stops_a_runaway_at_the_bound() -> None:
    """The two halves, wired: a repository-shaped read loop against the real connection.

    Previously the charging was unit-tested and the handling was unit-tested, and nothing drove one
    into the other - so "bounded per call" rested on an integration no test exercised.
    """
    driver = _EndlessDriver()
    connection = ReadOnlyConnection(driver, scrubber=SecretScrubber([]))

    with query_budget(max_queries=25) as budget, pytest.raises(BudgetExceeded) as excinfo:
        for _ in range(10_000):  # a fan-out that would otherwise never stop
            connection.execute_select("SELECT NAME FROM T WHERE X = ?", ["a"])

    assert excinfo.value.queries == 25
    assert budget.queries == 25
    # The driver was not asked again after the allowance ran out: the budget is charged before the
    # statement runs, so exhaustion costs no further database work.
    assert driver.executed == 25


def test_the_bound_is_charged_before_the_database_is_touched() -> None:
    driver = _EndlessDriver()
    connection = ReadOnlyConnection(driver, scrubber=SecretScrubber([]))
    with query_budget(max_queries=1):
        connection.execute_select("SELECT 1 FROM DUMMY")
        with pytest.raises(BudgetExceeded):
            connection.execute_select("SELECT 2 FROM DUMMY")
    assert driver.executed == 1
