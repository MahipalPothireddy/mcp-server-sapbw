"""Performance models: what a tool costs, and what happens to that cost on a bigger system.

**The question this answers.** *We have 40,000 InfoObjects and 1,200 chains. Which of these calls
will still return, and which will hit a bound?* Nothing here could answer that. The server had a
per-call budget, which stops a runaway, but a bound that is only visible when you hit it is not a
performance expectation - it is a surprise with a good error message.

**Two different facts, kept apart, because only one of them is measurable offline.**

*Payload size* is measured. It is the cost that matters most for an MCP server: a reply competes for
the model's context window, and a 200 KiB answer is expensive even when the database work was
trivial. It is measured deterministically against the synthetic fixtures, so CI can check it.

*Growth* is declared, with the constant that bounds it named. Whether a tool's work scales with the
size of the customer's system is a property of the code, not of a fixture - a fixture with one DSO
cannot demonstrate what happens with four thousand. Declaring it and naming the cap is honest;
extrapolating a curve from a one-object fixture would not be.

The two are never blended into a single score. A tool can be cheap on a fixture and unbounded in
principle, and that combination is exactly what a customer sizing this needs to see.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How a tool's cost responds to a larger system.
#:
#:   constant        bounded by the server itself, not by the customer's system. A capability
#:                   report is the same size on a small landscape and a huge one.
#:   per_page        one page of rows; cost is set by `limit`, not by the system size.
#:   per_object      proportional to the one object asked about (its fields, rules, elements).
#:   per_graph_node  proportional to the walked subgraph, capped by a node/depth bound.
#:   per_system      scans a whole class of objects. These are the calls to watch on a large
#:                   system, and each names the cap that stops it running away.
GrowthClass = Literal["constant", "per_page", "per_object", "per_graph_node", "per_system"]

#: Whether a number here was observed or declared. Same distinction the support matrix draws, for
#: the same reason: a reader has to know which claims rest on evidence.
CostMeasurement = Literal["measured", "declared", "not_measured"]


class CostBound(BaseModel):
    """What actually stops a tool from running away, named rather than described."""

    model_config = ConfigDict(extra="forbid")

    #: Human-readable bound, e.g. "500 rows per page" or "400 graph nodes".
    bound: str = Field(min_length=1)
    #: The constant in the code that sets it, so the claim can be checked against the source.
    constant: str | None = None
    #: What the caller sees when the bound binds. A bound that truncates silently is a defect; every
    #: entry here states how the reply says so.
    on_hit: str = Field(min_length=1)


class ToolCost(BaseModel):
    """One tool's measured payload, declared growth, and the bounds that hold it."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    growth: GrowthClass
    #: Why this growth class, in terms of what the tool reads. Required: an unexplained class is a
    #: label, and a label is not something a customer can check.
    growth_basis: str = Field(min_length=1)
    #: Serialised reply size against the synthetic fixtures, in bytes. A floor, not a forecast: the
    #: fixture holds roughly one object per type, so this isolates the fixed overhead of the reply
    #: shape from the per-row cost.
    fixture_payload_bytes: int | None = None
    payload_measurement: CostMeasurement = "not_measured"
    #: Statements issued against the fixtures. Structural rather than volumetric - it shows which
    #: tools fan out per node, which is the shape that matters, not the magnitude.
    fixture_statements: int | None = None
    statements_measurement: CostMeasurement = "not_measured"
    bounds: list[CostBound] = Field(default_factory=list)
    #: Whether the reply is shaped (summarised with a resource URI for the full record) when large.
    shaped: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def unbounded(self) -> bool:
        """True when nothing in the code caps this tool's cost. Worth knowing before a big run."""
        return not self.bounds


class PerformanceProfile(BaseModel):
    """The whole profile: per-tool cost, the global budget, and what was actually observed."""

    model_config = ConfigDict(extra="forbid")

    #: Build that produced it, so two customers comparing numbers can tell whether they match.
    server_version: str
    #: The per-call query and time allowance every tool runs inside, and how to change it.
    budget: dict[str, str] = Field(default_factory=dict)
    tools: list[ToolCost] = Field(default_factory=list)
    #: Counts by growth class, so the headline is readable without walking 57 rows.
    totals: dict[str, int] = Field(default_factory=dict)
    #: Latencies actually observed on the reference system, keyed by tool. Named separately from the
    #: fixture numbers because they come from one system on one day and are not a guarantee.
    observed: dict[str, str] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)

    def tool(self, name: str) -> ToolCost | None:
        return next((entry for entry in self.tools if entry.tool == name), None)
