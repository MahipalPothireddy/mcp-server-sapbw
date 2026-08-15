"""Aggregation semantics: how a number is combined, and therefore whether it can be re-added.

**Why this exists.** Everything else this server reports about a key figure - its lineage, its
provider, its restrictions - is silent about the one property that decides what the number *is*.
A key figure with exception aggregation ``CNT`` over ``0MATERIAL`` is a count of materials, not a
sum of anything; ``LAS`` over ``0CALDAY`` is a closing balance. Report such a figure without saying
so and two people comparing totals can both be right with no way to explain the difference. On the
reference system 1,819 query elements and 4,800+ key figures carry an aggregation setting.

**Two independent reasons a number cannot simply be added up:**

1. *Exception aggregation* - the figure is aggregated differently along one or more reference
   characteristics (:class:`ExceptionAggregation`).
2. *Non-cumulative key figures* - a stock/inventory figure, which has no meaningful sum over time
   at all, whatever its exception aggregation says (:attr:`KeyFigureAggregation.non_cumulative`).

Both are surfaced, because they fail differently and a caller checking only one will be wrong.

**A dictionary defect this had to work around.** The query-level domain ``RSAGGRGEN`` has shifted
texts in SAP's own dictionary on the reference system: ``DD07L`` and ``DD07T`` each hold 8 rows at
``VALPOS`` 1-8 with a single ``AS4LOCAL``/``AS4VERS``, so the join is exact, yet ``SUM`` carries the
text *"No Aggregation (X, If More Than One Value Uneq. to 0 Occurs)"* and ``NGA`` reads *"Minimum"*.
Those texts are therefore never used. Codes shared with the internally consistent domains
``RSDAGGRGEN`` and ``RSDAGGREXC`` are decoded from those instead and labelled ``cross_domain``, so a
corroborated reading is never mistaken for a directly documented one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .provenance import Provenance

# Where a decoded label came from.
#   dictionary   - the column's own domain documents this value and its texts are self-consistent.
#   cross_domain - the column's own domain is unusable (see the module docstring); the label comes
#                  from another domain that documents the same code and is internally consistent.
#   advisory     - the value occurs in data but no domain documents it. Reported, never asserted.
DecodeConfidence = Literal["dictionary", "cross_domain", "advisory"]

# What an AGGRCHA* reference value actually names. BW stores three different shapes in one column.
#   infoobject     - a plain InfoObject name, e.g. "0PLANT".
#   provider_field - a provider-qualified field, e.g. "4ZPP_L12-D30_P_INDEX".
#   unresolved     - present but not a usable object reference (the bare "0" sentinel occurs 109
#                    times on the reference system and is not an InfoObject).
ReferenceShape = Literal["infoobject", "provider_field", "unresolved"]

# RSDKYF.NCUMFL - decoded from domain RSNCUMFL.
NonCumulativeKind = Literal[
    "cumulative",
    "stock_with_change",
    "stock_with_movements",
]


class AggregationRule(BaseModel):
    """One decoded aggregation code: the raw value, its meaning, and how well that is backed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    label: str | None = None
    confidence: DecodeConfidence
    # True only for plain summation. Every other behaviour means re-adding the figure is wrong.
    is_summation: bool = False


class ReferenceCharacteristic(BaseModel):
    """A characteristic that exception aggregation is performed along."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    shape: ReferenceShape
    # Which AGGRCHA column it came from (1-5), so a caller can see the declared order.
    position: int = Field(ge=1, le=5)


class ExceptionAggregation(BaseModel):
    """Aggregation that differs along one or more reference characteristics.

    ``reproducible_by_summation`` is the field to read: when it is false, adding the figure up
    across the reference characteristic does not reproduce the reported number, and any total shown
    without that context is unexplainable rather than merely imprecise.
    """

    model_config = ConfigDict(extra="forbid")

    behaviour: AggregationRule
    # Up to five on this release; four are in use on the reference system. Order is as declared.
    reference_characteristics: list[ReferenceCharacteristic] = Field(default_factory=list)
    # RSZCALC.AGGREXCLUDE: the reference characteristic is excluded rather than aggregated along.
    excludes_reference: bool = False
    reproducible_by_summation: bool = False
    note: str | None = None


class KeyFigureAggregation(BaseModel):
    """The complete "how does this number combine, and in what unit" record for a key figure.

    Read from ``RSDKYF`` in one pass. The unit fields are here rather than in a separate currency
    model because they are inseparable from aggregation in practice: summing an amount across rows
    denominated in different currencies is meaningless, so ``unit_infoobject`` is part of whether a
    total is valid at all - not decoration.
    """

    model_config = ConfigDict(extra="forbid")

    key_figure: str
    key_figure_type: AggregationRule | None = None
    data_type: AggregationRule | None = None
    default_aggregation: AggregationRule | None = None
    exception_aggregation: ExceptionAggregation | None = None
    # A stock figure has no meaningful sum over time regardless of exception aggregation.
    non_cumulative: NonCumulativeKind = "cumulative"
    non_cumulative_label: str | None = None
    # Currency/unit. A fixed value applies to every record; an InfoObject means it varies per row,
    # and a total across differing values is invalid.
    fixed_currency: str | None = None
    fixed_unit: str | None = None
    unit_infoobject: str | None = None
    stock_coverage: bool = False
    # The one-line answer to "can I add this up?", with the reasons that made it false.
    summable: bool = True
    summability_caveats: list[str] = Field(default_factory=list)
    provenance: Provenance
