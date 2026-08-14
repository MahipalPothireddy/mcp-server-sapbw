"""Per-call query budgets — the bound that stops one tool call from running away.

Without this, a single tool call against a large production system is unbounded: the metadata tables
this server reads run to millions of rows (``RSAABAP``, ``RSPCPROCESSLOG``, ``RSZELTTXT``), and a
broad request can issue thousands of statements. On someone else's landscape that is worse than
slow — it holds a database session, and the caller has no idea whether to keep waiting.

A budget is scoped to one MCP tool call and charged per statement. Exceeding it raises
:class:`BudgetExceeded`, which the tool layer turns into a structured, honest result: *this is how
far the analysis got and why it stopped*, rather than a hang or a truncation that looks like an
answer.

The budget lives in a :class:`contextvars.ContextVar` so it follows a tool call into the worker
thread the server runs it on, without every repository having to thread it through its signature.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

# Defaults chosen to be generous for legitimate work and still bound a runaway: a whole-system
# documentation run issues far more than this and is expected to page, not to run unbounded.
DEFAULT_MAX_QUERIES = 5_000
DEFAULT_MAX_SECONDS = 300.0


class BudgetExceeded(Exception):
    """A tool call hit its query or time budget. Carries what was spent, for an honest report."""

    def __init__(self, *, reason: str, queries: int, elapsed_seconds: float) -> None:
        self.reason = reason
        self.queries = queries
        self.elapsed_seconds = round(elapsed_seconds, 3)
        super().__init__(
            f"{reason} (spent {queries} queries in {self.elapsed_seconds}s); "
            "narrow the request or raise the budget"
        )


@dataclass
class QueryBudget:
    """A statement and wall-clock allowance for one operation.

    Thread-safe: a single tool call may fan out across threads, and every one of them charges the
    same budget.
    """

    max_queries: int = DEFAULT_MAX_QUERIES
    max_seconds: float = DEFAULT_MAX_SECONDS
    clock: Callable[[], float] = time.monotonic
    queries: int = 0
    started_at: float = field(default=0.0)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.started_at == 0.0:
            self.started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return self.clock() - self.started_at

    def charge(self) -> None:
        """Account for one statement. Raises :class:`BudgetExceeded` when the allowance is gone."""
        with self._lock:
            elapsed = self.elapsed_seconds
            if self.max_seconds > 0 and elapsed > self.max_seconds:
                raise BudgetExceeded(
                    reason=f"time budget of {self.max_seconds}s exhausted",
                    queries=self.queries,
                    elapsed_seconds=elapsed,
                )
            if self.max_queries > 0 and self.queries >= self.max_queries:
                raise BudgetExceeded(
                    reason=f"query budget of {self.max_queries} statements exhausted",
                    queries=self.queries,
                    elapsed_seconds=elapsed,
                )
            self.queries += 1

    def snapshot(self) -> dict[str, float | int]:
        """What was spent, for logging and for reporting on a partial result."""
        with self._lock:
            return {
                "queries": self.queries,
                "elapsed_seconds": round(self.elapsed_seconds, 3),
                "max_queries": self.max_queries,
                "max_seconds": self.max_seconds,
            }


_current: contextvars.ContextVar[QueryBudget | None] = contextvars.ContextVar(
    "sapbw_query_budget", default=None
)


def current_budget() -> QueryBudget | None:
    """The budget for the operation in progress, or ``None`` when running unbudgeted."""
    return _current.get()


def charge_query() -> None:
    """Charge one statement against the active budget. A no-op when there is none."""
    budget = _current.get()
    if budget is not None:
        budget.charge()


@contextmanager
def query_budget(
    *,
    max_queries: int = DEFAULT_MAX_QUERIES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[QueryBudget]:
    """Scope a budget to a block. Nested scopes keep the outer (stricter) budget in force."""
    existing = _current.get()
    if existing is not None:
        # An inner scope must not hand itself a fresh allowance and escape the outer bound.
        yield existing
        return
    budget = QueryBudget(max_queries=max_queries, max_seconds=max_seconds, clock=clock)
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)
