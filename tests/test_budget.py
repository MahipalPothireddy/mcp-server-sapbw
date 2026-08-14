"""Tests for per-call query budgets.

The budget is what stops one tool call from running unbounded against a customer system. It has to
hold under three conditions that are easy to get wrong: concurrent charging from several threads, a
nested scope trying to grant itself a fresh allowance, and the connection layer charging *before*
spending a session rather than after.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_server_sapbw.core.budget import (
    DEFAULT_MAX_QUERIES,
    DEFAULT_MAX_SECONDS,
    BudgetExceeded,
    QueryBudget,
    charge_query,
    current_budget,
    query_budget,
)
from mcp_server_sapbw.core.connection import (
    ReadOnlyConnection,
    ReadOnlyViolation,
    SecretScrubber,
)


class FakeCursor:
    def execute(self, operation: str, parameters: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return [("row",)]

    def close(self) -> None:
        return None


class FakeConnection:
    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def close(self) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- the budget itself ---------------------------------------------------------------------


def test_charging_counts_statements() -> None:
    budget = QueryBudget(max_queries=5)
    for _ in range(3):
        budget.charge()
    assert budget.queries == 3
    assert budget.snapshot()["queries"] == 3


def test_query_ceiling_raises_with_what_was_spent() -> None:
    budget = QueryBudget(max_queries=2)
    budget.charge()
    budget.charge()
    with pytest.raises(BudgetExceeded) as excinfo:
        budget.charge()
    assert excinfo.value.queries == 2
    assert "query budget" in excinfo.value.reason
    assert "narrow the request" in str(excinfo.value)


def test_time_ceiling_raises() -> None:
    clock = Clock()
    budget = QueryBudget(max_queries=1000, max_seconds=10.0, clock=clock)
    budget.charge()
    clock.t = 11.0
    with pytest.raises(BudgetExceeded) as excinfo:
        budget.charge()
    assert "time budget" in excinfo.value.reason


def test_zero_means_unbounded() -> None:
    budget = QueryBudget(max_queries=0, max_seconds=0)
    for _ in range(50):
        budget.charge()
    assert budget.queries == 50


def test_concurrent_charging_is_accounted_exactly() -> None:
    budget = QueryBudget(max_queries=0)
    workers, per_worker = 8, 50

    def worker() -> None:
        for _ in range(per_worker):
            budget.charge()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(worker) for _ in range(workers)]:
            future.result()
    assert budget.queries == workers * per_worker


def test_concurrent_charging_respects_the_ceiling() -> None:
    budget = QueryBudget(max_queries=20)
    granted = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal granted
        for _ in range(20):
            try:
                budget.charge()
            except BudgetExceeded:
                return
            with lock:
                granted += 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in [pool.submit(worker) for _ in range(6)]:
            future.result()
    assert granted == 20, "the ceiling leaked under concurrency"


# --- scoping -------------------------------------------------------------------------------


def test_scope_installs_and_removes_the_budget() -> None:
    assert current_budget() is None
    with query_budget(max_queries=3) as budget:
        assert current_budget() is budget
        charge_query()
        assert budget.queries == 1
    assert current_budget() is None


def test_charge_without_a_scope_is_a_no_op() -> None:
    charge_query()  # must not raise when nothing is budgeted


def test_nested_scope_cannot_grant_a_fresh_allowance() -> None:
    """Otherwise a service opening its own scope would escape the caller's bound."""
    with query_budget(max_queries=2) as outer:
        charge_query()
        with query_budget(max_queries=1000) as inner:
            assert inner is outer
            charge_query()
        with pytest.raises(BudgetExceeded):
            charge_query()


def test_defaults_are_generous_but_finite() -> None:
    with query_budget() as budget:
        assert budget.max_queries == DEFAULT_MAX_QUERIES > 0
        assert budget.max_seconds == DEFAULT_MAX_SECONDS > 0


# --- integration with the connection layer -------------------------------------------------


def test_execute_select_charges_the_budget() -> None:
    connection = ReadOnlyConnection(FakeConnection(), scrubber=SecretScrubber([]))
    with query_budget(max_queries=10) as budget:
        connection.execute_select("SELECT 1 FROM DUMMY")
        connection.execute_select("SELECT 2 FROM DUMMY")
    assert budget.queries == 2


def test_exhausted_budget_stops_the_next_query_before_the_driver() -> None:
    """The charge happens before checkout, so an exhausted budget never spends a session."""

    class Exploding:
        def cursor(self) -> FakeCursor:  # pragma: no cover - must not be reached
            raise AssertionError("a query ran after the budget was exhausted")

        def close(self) -> None:
            return None

    spender = ReadOnlyConnection(FakeConnection(), scrubber=SecretScrubber([]))
    blocked = ReadOnlyConnection(Exploding(), scrubber=SecretScrubber([]))
    with query_budget(max_queries=1):
        spender.execute_select("SELECT 1 FROM DUMMY")  # spends the single allowance
        with pytest.raises(BudgetExceeded):
            blocked.execute_select("SELECT 2 FROM DUMMY")


def test_the_statement_guard_still_runs_first() -> None:
    """A refused write must not consume budget: it never reaches the database."""
    connection = ReadOnlyConnection(FakeConnection(), scrubber=SecretScrubber([]))
    with query_budget(max_queries=5) as budget, pytest.raises(ReadOnlyViolation):
        connection.execute_select("DELETE FROM T")
    assert budget.queries == 0
