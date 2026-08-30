"""One vocabulary for *why* an answer is bounded, shared by every reader (defect D6).

**The problem this solves.** Readers reported ``truncated: bool`` - that something stopped them,
never what. A caller cannot act on that. "This provider has two consumers" and "we stopped looking
after two" are opposite readings, and the bool renders them identically; worse, most readers set the
bool in exactly one place, so a read that stopped for any *other* reason reported
``truncated=False`` and presented a bounded answer as a complete one.

:class:`LineageGraph` was fixed first and in isolation, gaining a ``completeness`` scalar. That left
the same defect in every other reader - the calc-view lineage, the query element tree, the routine
register, the security overview, the scenario reports, the chain recursion - so the vocabulary now
lives here and they all use it.

**What a bound is not.** A bound is not an error and not a lack of evidence. :class:`Evidence` says
how firmly one fact was established; completeness says whether the *set* is all of them. Both can
hold at once - a routine's table reads are each inferred *and* the list is a lower bound - and
collapsing them loses one.

**The design rule that makes this honest.** ``unspecified`` exists so that a reader which knows it
was bounded but did not record which bound can still say the answer is partial. It is deliberately
ugly to read, because a reader reaching it is under-reporting: the alternative was to guess a bound,
which would state a reason nobody established. A test asserts that the readers wired here name a
real bound rather than falling back to it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Why a result is not the whole answer.
#:
#: ``complete``             nothing stopped the read; the set is everything that matched.
#: ``row_cap``              a stated per-read row cap stopped it.
#: ``semantic_limit``       a cap on distinct *resolved objects* stopped discovery.
#: ``page_limit``           the caller's own limit/offset window; more rows exist behind it.
#: ``depth_limit``          a traversal depth bound stopped it.
#: ``node_limit``           a graph-wide node cap stopped expansion.
#: ``recursion_limit``      nested resolution stopped on depth or a cycle guard.
#: ``parse_budget``         only part of a set was parsed; the rest is counted but not analysed.
#: ``query_budget``         the per-call statement allowance would not cover another read.
#: ``time_budget``          the per-call wall-clock allowance would not cover another read.
#: ``error_degraded``       a branch failed and this is what survived.
#: ``unsupported_branch``   this release lacks the metadata one branch needs.
#: ``unspecified``          bounded, but the reason was not recorded. See the module docstring.
Bound = Literal[
    "complete",
    "row_cap",
    "semantic_limit",
    "page_limit",
    "depth_limit",
    "node_limit",
    "recursion_limit",
    "parse_budget",
    "query_budget",
    "time_budget",
    "error_degraded",
    "unsupported_branch",
    "unspecified",
]

#: Most-to-least limiting. A read that hit several bounds reports the one that most limits the
#: answer, because that is the one a caller has to act on first. ``unspecified`` sorts last of the
#: real bounds: any named reason is more useful than "something".
BOUND_PRECEDENCE: tuple[Bound, ...] = (
    "error_degraded",
    "time_budget",
    "query_budget",
    "unsupported_branch",
    "node_limit",
    "depth_limit",
    "recursion_limit",
    "semantic_limit",
    "parse_budget",
    "row_cap",
    "page_limit",
    "unspecified",
)

#: What each bound means for the answer, in the caller's terms rather than the implementation's.
BOUND_MEANING: dict[Bound, str] = {
    "complete": "nothing stopped this read; the set is everything that matched.",
    "row_cap": (
        "a stated row cap stopped the read, so further matching rows may exist. The rows returned "
        "are ordered, so they are a deterministic prefix rather than an arbitrary sample."
    ),
    "semantic_limit": (
        "a cap on distinct resolved objects stopped discovery, so further related objects may "
        "exist. The subset returned is deterministic, not sampled."
    ),
    "page_limit": (
        "this is one page of a larger result; the total count says how much is behind it."
    ),
    "depth_limit": "a traversal depth bound stopped the walk, so objects further out are absent.",
    "node_limit": (
        "a node cap stopped expansion, so objects beyond it are absent. A smaller depth gives a "
        "complete reading of a smaller neighbourhood."
    ),
    "recursion_limit": (
        "nested resolution stopped on a depth bound or a cycle guard, so deeper nesting is not "
        "represented."
    ),
    "parse_budget": (
        "only part of the set was parsed. Entries that were not analysed have unknown detail "
        "rather than none, and counts still cover the whole set."
    ),
    "query_budget": (
        "the per-call statement allowance ran low and the read stopped early. Narrow the request "
        "or raise SAPBW_MAX_QUERIES_PER_CALL."
    ),
    "time_budget": (
        "the per-call time allowance ran low and the read stopped early. Narrow the request or "
        "raise SAPBW_MAX_SECONDS_PER_CALL."
    ),
    "error_degraded": (
        "at least one read failed and this is what survived, so the absence of something here is "
        "not evidence that it does not exist."
    ),
    "unsupported_branch": (
        "this release lacks the metadata one branch needs, so that class of fact is missing "
        "entirely rather than absent."
    ),
    "unspecified": (
        "this answer is bounded but the reason was not recorded, so how much is missing is unknown."
    ),
}


class BoundHit(BaseModel):
    """One bound that actually bound, with the limit it stopped at."""

    model_config = ConfigDict(extra="forbid")

    bound: Bound
    #: What was bounded - a table, a section, a branch - so several hits stay tellable apart.
    scope: str | None = None
    #: The numeric limit reached, where there is one. Reporting it lets a caller decide whether
    #: raising it is worth the cost instead of guessing.
    limit: int | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _fill_detail(self) -> BoundHit:
        if self.detail is None:
            self.detail = BOUND_MEANING.get(self.bound, BOUND_MEANING["unspecified"])
        return self


class Completeness(BaseModel):
    """Whether a result is the whole answer, and if not, which bounds stopped it.

    ``status`` is the single most limiting bound, for a caller that wants one value; ``bounds``
    keeps every bound that bound, because a read can be cut short by more than one and they need
    different follow-up.
    """

    model_config = ConfigDict(extra="forbid")

    status: Bound = "complete"
    bounds: list[BoundHit] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def meaning(self) -> str:
        return BOUND_MEANING.get(self.status, BOUND_MEANING["unspecified"])

    @model_validator(mode="after")
    def _derive_status(self) -> Completeness:
        """``status`` follows from ``bounds`` unless a caller set it explicitly.

        Keeping the scalar derived rather than separately assigned is what stops the two from
        disagreeing - which is the same failure ``truncated`` had against reality.
        """
        if self.bounds:
            hit = {entry.bound for entry in self.bounds}
            for candidate in BOUND_PRECEDENCE:
                if candidate in hit:
                    self.status = candidate
                    break
        return self


def bounded(bound: Bound, *, scope: str | None = None, limit: int | None = None) -> Completeness:
    """A :class:`Completeness` naming one bound. The common case, in one call."""
    return Completeness(bounds=[BoundHit(bound=bound, scope=scope, limit=limit)])


COMPLETE = Completeness()


class BoundedResult(BaseModel):
    """Base for any result that can be cut short, carrying both the flag and the reason.

    ``truncated`` stays because it is a published field and removing it would break callers for no
    gain. The two are reconciled here rather than assigned independently, so a result cannot say
    ``truncated=False`` while naming a bound, or claim completeness while the flag is set - the
    exact inconsistency that made the bool untrustworthy.
    """

    model_config = ConfigDict(extra="forbid")

    truncated: bool = False
    completeness: Completeness = Field(default_factory=Completeness)

    @model_validator(mode="after")
    def _reconcile_bound(self) -> BoundedResult:
        if not self.completeness.is_complete:
            self.truncated = True
        elif self.truncated:
            # A producer set only the bool. Say the answer is partial without inventing a reason.
            self.completeness = bounded("unspecified")
        return self
