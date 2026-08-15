"""Decode maps for aggregation codes, and the pure functions that build the models.

Every code table here was read from the ABAP dictionary on a live BW 7.50 system
(``DD03L`` -> ``DOMNAME`` -> ``DD07L``/``DD07T``, joined on domain + ``AS4LOCAL`` + ``AS4VERS`` +
``VALPOS``), not recalled. The value distributions quoted in comments are from the same system and
exist to show which branches real data actually exercises.

**The one place a dictionary text is deliberately ignored.** Domain ``RSAGGRGEN``, behind
``RSZCALC.AGGRGEN``, has shifted texts: ``SUM`` reads as *"No Aggregation..."* and ``NGA`` as
*"Minimum"*. Verified as a data defect, not a join error - both ``DD07L`` and ``DD07T`` hold 8 rows
at ``VALPOS`` 1-8 for a single ``AS4LOCAL``/``AS4VERS``. Shared codes are therefore taken from
``RSDAGGRGEN`` and ``RSDAGGREXC``, which are internally consistent and agree with each other, and
the result is labelled ``cross_domain``.

Pure module: no connection, no I/O. The repositories supply raw column values.
"""

from __future__ import annotations

from typing import Any

from ..models.aggregation import (
    AggregationRule,
    ExceptionAggregation,
    KeyFigureAggregation,
    NonCumulativeKind,
    ReferenceCharacteristic,
    ReferenceShape,
)
from ..models.provenance import Provenance

# --- exception aggregation (RSDAGGREXC, 22 values; RSAGGREXC is a subset and agrees) ----------
# Live use, RSDKYF: SUM 4230, LAS 194, MAX 109, AV0 55, AVG 52, NO1 49, NO2 28, FIR 25, AV1 23,
# MIN 21, CN0 10, NOP 10.  RSZCALC: SUM 1457, LAS 230, AV0 58, AVG 30, CN0 19, CNT 15, AV1 5,
# MAX 4, FIR 1.
_EXCEPTION_AGGREGATION: dict[str, str] = {
    "AV0": "Average (values not equal to zero)",
    "AV1": "Average (weighted with number of days)",
    "AV2": "Average (weighted with number of working days; factory calendar)",
    "AVG": "Average (all values)",
    "CN0": "Counter (values unequal to zero)",
    "CNT": "Counter (all values)",
    "FIR": "First value",
    "LAS": "Last value",
    "MAX": "Maximum",
    "MIN": "Minimum",
    "NO1": "No aggregation (X if more than one record occurs)",
    "NO2": "No aggregation (X if more than one value occurs)",
    "NOP": "No aggregation (X if more than one value unequal to 0 occurs)",
    "STD": "Standard deviation",
    "SUM": "Summation",
    "VAR": "Variance",
    "NHA": "No aggregation along hierarchy",
    "NGA": "No aggregation of postable nodes along hierarchy",
    "MED": "Median",
    "SLS": "Simple linear regression: slope",
    "SLI": "Simple linear regression: y-intercept",
    "SLC": "Simple linear regression: correlation coefficient",
}

# --- standard aggregation, key-figure level (RSDAGGRGEN, 6 values) ----------------------------
# Live: SUM 4499, MAX 215, MIN 40, NOP 32, NO1 28. The dictionary marks NOP/NO1/NO2 as no longer
# used for this domain, which is reported as-is rather than substituted with the query-level
# meaning - a value SAP calls obsolete is itself the useful fact.
_KEYFIGURE_GENERAL: dict[str, str] = {
    "SUM": "Summation",
    "MAX": "Maximum",
    "MIN": "Minimum",
    "NOP": "No longer used (per dictionary)",
    "NO1": "No longer used (per dictionary)",
    "NO2": "No longer used (per dictionary)",
}

# --- standard aggregation, query level (RSZCALC.AGGRGEN) --------------------------------------
# Domain RSAGGRGEN documents MAX, MIN, NGA, NHA, NO1, NO2, NOP, SUM but its texts are shifted, so
# the meanings come from the consistent domains above. Live: blank 7898, SUM 839.
_QUERY_GENERAL_DICTIONARY = {"SUM", "MAX", "MIN"}  # corroborated by RSDAGGRGEN
_QUERY_GENERAL_CROSS = {"NO1", "NO2", "NOP", "NHA", "NGA"}  # meanings from RSDAGGREXC

# RSDKYF.KYFTP (domain RSKYFTP). Live: AMO 2118, QUA 1164, NUM 942, INT 467, DAT 108, TIM 15.
_KEY_FIGURE_TYPE: dict[str, str] = {
    "AMO": "Amount",
    "QUA": "Quantity",
    "NUM": "Number (without unit)",
    "INT": "Whole number",
    "FLO": "Floating point number (without unit)",
    "DAT": "Date",
    "TIM": "Time",
}

# RSDKYF.NCUMFL (domain RSNCUMFL). Live: blank 4752, '1' 35, '2' 27 - 62 non-cumulative figures.
_NON_CUMULATIVE: dict[str, tuple[NonCumulativeKind, str | None]] = {
    "": ("cumulative", "Cumulative value"),
    "1": ("stock_with_change", "Stock with change involving stocks"),
    "2": ("stock_with_movements", "Stock with inward and outward movement"),
}
# Fallback for an NCUMFL value the domain does not document. The kind reads "cumulative" because
# the model needs a value, but the caller is told the truth via a summability caveat rather than
# being allowed to assume the figure is safe to add up.
_UNKNOWN_NON_CUMULATIVE: tuple[NonCumulativeKind, str | None] = ("cumulative", None)

# RSDKYF.DATATP (domain DATATYPE) - only the values that occur for key figures are mapped; anything
# else is surfaced with advisory confidence rather than dropped.
# Live: CURR 2114, QUAN 1092, DEC 913, INT4 467, FLTP 176, DATS 41, TIMS 11.
_DATA_TYPE: dict[str, str] = {
    "CURR": "Currency field in BCD format",
    "QUAN": "Quantity field in BCD format",
    "DEC": "Packed number in BCD format",
    "INT4": "4-byte integer",
    "INT8": "8-byte integer",
    "INT2": "2-byte integer",
    "INT1": "1-byte integer",
    "FLTP": "Floating point number",
    "DATS": "Date (YYYYMMDD)",
    "TIMS": "Time (HHMMSS)",
    "NUMC": "Numerical text",
    "CHAR": "Character string",
}

# A reference value that is present but names nothing usable. Observed 109 times on the reference
# system; treating it as an InfoObject would invent an object that does not exist.
_NULL_REFERENCES = {"", "0"}

_MAX_REFERENCES = 5


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def classify_reference(value: Any) -> ReferenceShape:
    """Decide what an ``AGGRCHA*`` value actually names, without asserting more than is visible."""
    text = _clean(value)
    if text in _NULL_REFERENCES:
        return "unresolved"
    # Provider-qualified field references carry a separator; plain InfoObject names never do.
    if "-" in text or "." in text:
        return "provider_field"
    return "infoobject"


def decode_exception_aggregation(code: Any) -> AggregationRule | None:
    """Decode ``AGGREXC``. ``None`` when blank, which means no exception aggregation is set."""
    text = _clean(code).upper()
    if not text:
        return None
    label = _EXCEPTION_AGGREGATION.get(text)
    return AggregationRule(
        code=text,
        label=label,
        confidence="dictionary" if label else "advisory",
        is_summation=text == "SUM",
    )


def decode_keyfigure_aggregation(code: Any) -> AggregationRule | None:
    """Decode ``RSDKYF.AGGRGEN`` from its own (consistent) domain."""
    text = _clean(code).upper()
    if not text:
        return None
    label = _KEYFIGURE_GENERAL.get(text)
    return AggregationRule(
        code=text,
        label=label,
        confidence="dictionary" if label else "advisory",
        is_summation=text == "SUM",
    )


def decode_query_aggregation(code: Any) -> AggregationRule | None:
    """Decode ``RSZCALC.AGGRGEN`` avoiding its own domain's shifted texts."""
    text = _clean(code).upper()
    if not text:
        return None
    if text in _QUERY_GENERAL_DICTIONARY:
        return AggregationRule(
            code=text,
            label=_KEYFIGURE_GENERAL[text],
            confidence="dictionary",
            is_summation=text == "SUM",
        )
    if text in _QUERY_GENERAL_CROSS:
        return AggregationRule(
            code=text,
            label=_EXCEPTION_AGGREGATION[text],
            confidence="cross_domain",
            is_summation=False,
        )
    return AggregationRule(code=text, label=None, confidence="advisory")


def _simple_rule(code: Any, table: dict[str, str]) -> AggregationRule | None:
    text = _clean(code).upper()
    if not text:
        return None
    label = table.get(text)
    return AggregationRule(code=text, label=label, confidence="dictionary" if label else "advisory")


def build_exception_aggregation(
    *,
    code: Any,
    references: list[Any],
    exclude: Any = None,
) -> ExceptionAggregation | None:
    """Assemble exception aggregation from a code plus its ordered reference columns."""
    behaviour = decode_exception_aggregation(code)
    if behaviour is None:
        return None

    resolved: list[ReferenceCharacteristic] = []
    for index, raw in enumerate(references[:_MAX_REFERENCES], start=1):
        name = _clean(raw)
        if not name:
            continue
        resolved.append(
            ReferenceCharacteristic(name=name, shape=classify_reference(name), position=index)
        )

    excludes = _clean(exclude).upper() == "X"
    summable = behaviour.is_summation and not excludes

    note: str | None = None
    if not resolved and not behaviour.is_summation:
        note = (
            "an exception aggregation is set but no reference characteristic is recorded, so what "
            "it aggregates along cannot be stated from metadata"
        )
    elif any(r.shape == "unresolved" for r in resolved):
        note = (
            "at least one reference value does not name a usable object and is reported as "
            "unresolved rather than guessed"
        )
    elif excludes:
        note = (
            "the reference characteristic is excluded from aggregation rather than aggregated along"
        )

    return ExceptionAggregation(
        behaviour=behaviour,
        reference_characteristics=resolved,
        excludes_reference=excludes,
        reproducible_by_summation=summable,
        note=note,
    )


def build_key_figure_aggregation(
    *,
    key_figure: str,
    kyftp: Any = None,
    datatp: Any = None,
    aggrgen: Any = None,
    aggrexc: Any = None,
    aggrcha: Any = None,
    ncumfl: Any = None,
    fixcuky: Any = None,
    fixunit: Any = None,
    uninm: Any = None,
    semantic: Any = None,
    provenance: Provenance,
) -> KeyFigureAggregation:
    """Assemble the full key-figure aggregation record and decide whether it can be summed."""
    exception = build_exception_aggregation(code=aggrexc, references=[aggrcha])
    default = decode_keyfigure_aggregation(aggrgen)
    # An undocumented NCUMFL value must not be read as "cumulative"; it is unknown, and treating
    # unknown as safe-to-sum is exactly the wrong default.
    ncum_code = _clean(ncumfl)
    kind, ncum_label = _NON_CUMULATIVE.get(ncum_code, _UNKNOWN_NON_CUMULATIVE)

    caveats: list[str] = []
    summable = True

    if exception is not None and not exception.reproducible_by_summation:
        summable = False
        along = ", ".join(r.name for r in exception.reference_characteristics) or "an unrecorded"
        caveats.append(
            f"exception aggregation {exception.behaviour.code} "
            f"({exception.behaviour.label or 'meaning not documented'}) applies along {along} "
            "characteristic(s), so adding the figure up does not reproduce the reported number"
        )
    if kind != "cumulative":
        summable = False
        caveats.append(
            f"non-cumulative key figure ({ncum_label}): it is a stock, so it has no meaningful sum "
            "over time whatever its aggregation setting says"
        )
    elif ncum_code and ncum_code not in _NON_CUMULATIVE:
        summable = False
        caveats.append(
            f"NCUMFL carries the undocumented value {ncum_code!r}; whether this figure is "
            "cumulative is unknown, so it is not treated as safe to sum"
        )
    if default is not None and not default.is_summation and default.code in {"MIN", "MAX"}:
        summable = False
        caveats.append(f"standard aggregation is {default.code} rather than summation")
    unit_object = _clean(uninm) or None
    if unit_object and not _clean(fixcuky) and not _clean(fixunit):
        caveats.append(
            f"the unit/currency varies per record ({unit_object}), so a total across differing "
            "values is not meaningful without translating them first"
        )

    return KeyFigureAggregation(
        key_figure=key_figure,
        key_figure_type=_simple_rule(kyftp, _KEY_FIGURE_TYPE),
        data_type=_simple_rule(datatp, _DATA_TYPE),
        default_aggregation=default,
        exception_aggregation=exception,
        non_cumulative=kind,
        non_cumulative_label=ncum_label,
        fixed_currency=_clean(fixcuky) or None,
        fixed_unit=_clean(fixunit) or None,
        unit_infoobject=unit_object,
        stock_coverage=_clean(semantic).upper() == "S",
        summable=summable,
        summability_caveats=caveats,
        provenance=provenance,
    )
