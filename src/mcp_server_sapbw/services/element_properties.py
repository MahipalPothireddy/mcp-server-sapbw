"""Decode ``RSZELTPROP``: the query-element settings that change what a number means.

A query element's restriction says which rows it covers. These properties say what happens to the
value afterwards - translated into another currency, aggregated locally as a last value rather
than a sum, displayed with its sign inverted, read against a different key date, or hidden
altogether. Two people comparing figures from one report can both be reading it correctly and
still disagree, and this table is where several of the reasons live.

Every code table was read from the ABAP dictionary on a live BW 7.50 system (``DD03L`` ->
``DOMNAME`` -> ``DD07L``/``DD07T``, joined on domain + ``AS4LOCAL`` + ``AS4VERS`` + ``VALPOS``),
not recalled. The value distributions in the comments come from the same system, over 73,854
active rows, and are there to show which branches real data exercises - including the ones it does
not, which still have to be read because another customer's system will use them.

Pure module: no connection, no I/O. The repository supplies raw column values.
"""

from __future__ import annotations

from typing import Any, cast

from ..models.aggregation import AggregationRule
from ..models.provenance import Provenance
from ..models.queries import (
    CurrencyTranslation,
    DisplayHierarchy,
    ElementProperties,
    UnitConversion,
    ValueSource,
)

# --- how a setting's value is specified (domain RSZTYPEFLAG) ----------------------------------
# Behind TCURFLAG, TCURDATEFLAG, TUOMFLAG, HIENMFLAG, VERSIONFLAG, DATETOFLAG, KEYDATEFLAG.
# The distinction that matters: a variable-driven value resolves per execution, so metadata can
# name the mechanism but not the effective value - the same dead end as a customer-exit variable.
# Live (TCURFLAG): '0' 73002, '1' 852.  (HIENMFLAG): '0' 72789, '2' 991, '1' 60, '3' 14.
_VALUE_SOURCE: dict[str, str] = {
    "0": "Not set",
    "1": "Fixed value",
    "2": "Reference to another element",
    "3": "Variable",
    "4": "InfoObject",
    "5": "Constant",
    "6": "Exit variable name (screen filter)",
}
# Sources whose effective value is only known at execution time.
_RUNTIME_SOURCES = frozenset({"3", "6"})
# Sources that mean "nothing configured here".
_UNSET_SOURCES = frozenset({"", "0"})
# What the paired value column actually holds, per source code. Learnt from live data: with
# HIENMFLAG = '2' the HIENM column holds a 25-character element UID rather than a hierarchy name,
# so presenting the stored value as the named object would show a UID as though it were a hierarchy.
_VALUE_HOLDS: dict[str, str] = {
    "1": "literal",
    "2": "element_uid",
    "3": "variable_name",
    "4": "infoobject",
    "5": "constant",
    "6": "variable_name",
}

# --- local aggregation for a structure member (domain RRLAGGR, behind STRMEM_LAGGR) -----------
# The same concepts as exception aggregation but with numeric codes, so it needs its own table.
# Live: '00' 73307, '01' 387, '11' 94, '07' 29, '12' 18, '06' 11 - 547 elements aggregate locally.
_LOCAL_AGGREGATION: dict[str, str] = {
    "00": "(Nothing defined)",
    "01": "Summation",
    "02": "Maximum",
    "03": "Minimum",
    "04": "Counting all values",
    "05": "Count all values <> 0",
    "06": "Average of all values",
    "07": "Average of all values <> 0",
    "08": "Standard deviation",
    "09": "Variance",
    "10": "Suppress result",
    "11": "First value",
    "12": "Last value",
    "13": "Summation of rounded values",
}
_LOCAL_AGGREGATION_UNSET = frozenset({"", "00"})

# --- direction of that local aggregation (domain RSZAXIS, behind LAGGR_DIR) -------------------
# Live: '0' 73850, '1' 2, '2' 2.
_AGGREGATION_DIRECTION: dict[str, str] = {
    "0": "Use default direction",
    "1": "Calculate along the rows",
    "2": "Calculate along the columns",
}

# --- total suppression (domain RRXNOSUMS, behind NOSUMS) --------------------------------------
# Live: 'U' 44060, blank 29000, 'C' 742, 'Z' 52. A suppressed total is a reporting decision, not a
# fault, but it explains a report that shows rows adding to a figure it never displays.
_TOTAL_SUPPRESSION: dict[str, str] = {
    "": "Default: do not suppress total",
    "U": "Suppress the total unconditionally",
    "C": "Suppress the total conditionally",
    "Z": "Do not suppress the total (set in Query Designer)",
}

# --- display state (domain RSZHIDDEN, behind HIDDEN) -----------------------------------------
# Live: '#' 46556, blank 22342, 'X' 3033, 'Y' 1923. '#' is BW's explicit "default", which is not the
# same statement as an unset blank and is reported as itself.
_DISPLAY_STATE: dict[str, str] = {
    "": "Display",
    "#": "Default",
    "X": "Hide",
    "Y": "Hide (can be shown)",
}
_HIDDEN_STATES = frozenset({"X", "Y"})

# --- three-valued booleans (domain RSZ_BOOL_DEFAULT, behind SIGNINV and EMPHASIS) -------------
# '' false, 'X' true, '#' default. Live SIGNINV: '#' 48322, blank 25298, 'X' 234.
_TRUE = "X"


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _value_source(code: Any) -> ValueSource | None:
    """Decode a ``*FLAG`` column. ``None`` when it says nothing is configured."""
    text = _clean(code)
    if text in _UNSET_SOURCES:
        return None
    label = _VALUE_SOURCE.get(text)
    return ValueSource(
        code=text,
        label=label,
        confidence="dictionary" if label else "advisory",
        runtime_resolved=text in _RUNTIME_SOURCES,
        value_holds=cast("Any", _VALUE_HOLDS.get(text, "unknown")),
    )


def _local_aggregation(code: Any) -> AggregationRule | None:
    text = _clean(code)
    if text in _LOCAL_AGGREGATION_UNSET:
        return None
    label = _LOCAL_AGGREGATION.get(text)
    return AggregationRule(
        code=text,
        label=label,
        confidence="dictionary" if label else "advisory",
        is_summation=text == "01",
    )


def _currency_translation(row: dict[str, Any]) -> CurrencyTranslation | None:
    """``None`` unless something about a currency translation is actually configured."""
    target_source = _value_source(row.get("TCURFLAG"))
    date_source = _value_source(row.get("TCURDATEFLAG"))
    target = _clean(row.get("TCUR")) or None
    translation_type = _clean(row.get("CTTNM")) or None
    key_date = _clean(row.get("TCURDATE")) or None
    if not any((target_source, date_source, target, translation_type, key_date)):
        return None
    return CurrencyTranslation(
        target_currency=target,
        target_source=target_source,
        translation_type=translation_type,
        key_date=key_date,
        key_date_source=date_source,
    )


def _unit_conversion(row: dict[str, Any]) -> UnitConversion | None:
    target_source = _value_source(row.get("TUOMFLAG"))
    # UOMNM is the unit InfoObject; TUOM the target-unit reference. Either alone is a configuration.
    target = _clean(row.get("TUOM")) or None
    unit_infoobject = _clean(row.get("UOMNM")) or None
    if not any((target_source, target, unit_infoobject)):
        return None
    return UnitConversion(
        target_unit=target, unit_infoobject=unit_infoobject, target_source=target_source
    )


def _display_hierarchy(row: dict[str, Any]) -> DisplayHierarchy | None:
    source = _value_source(row.get("HIENMFLAG"))
    hierarchy = _clean(row.get("HIENM")) or None
    active = _clean(row.get("HRY_ACTIVE")) == _TRUE
    if not any((source, hierarchy, active)):
        return None
    start_level = _clean(row.get("STRT_LVL"))
    return DisplayHierarchy(
        hierarchy=hierarchy,
        source=source,
        version=_clean(row.get("VERSION")) or None,
        valid_to=_clean(row.get("DATETO")) or None,
        start_level=int(start_level) if start_level.isdigit() and start_level != "00" else None,
        active=active,
    )


def _describe_target(value: str | None, source: ValueSource | None, noun: str) -> str:
    """Describe a translation/conversion target without overstating what was read.

    A flag can be set with its value column empty - 852 elements declare a currency-translation
    target on the reference system but only 817 store one. Saying "translated to a target read at
    runtime" there would be doubly wrong: nothing is read at runtime, and nothing was found. The
    honest statement is that the target is configured but not recorded on the element.
    """
    if source is None:
        return value or f"another {noun}"
    if source.runtime_resolved:
        return f"a target {noun} chosen at runtime ({source.label or source.code})"
    if source.value_holds == "element_uid":
        return f"a target {noun} defined by another element" + (f" ({value})" if value else "")
    if source.value_holds == "infoobject" and value:
        return f"a target {noun} taken from InfoObject {value}"
    if value:
        return value
    return (
        f"a target {noun} that the element declares ({source.label or source.code}) "
        "but does not record"
    )


def build_element_properties(
    row: dict[str, Any], *, eltuid: str, provenance: Provenance
) -> ElementProperties:
    """Assemble one element's properties, and say which of them change the number.

    ``changes_the_number`` is the synthesis worth having: a caller asking "why does this figure not
    match the rows behind it" gets the applicable reasons named, instead of a flat property dump it
    has to interpret. A setting that is merely cosmetic stays out of that list.
    """
    currency = _currency_translation(row)
    unit = _unit_conversion(row)
    hierarchy = _display_hierarchy(row)
    aggregation = _local_aggregation(row.get("STRMEM_LAGGR"))
    direction_code = _clean(row.get("LAGGR_DIR"))
    display_code = _clean(row.get("HIDDEN"))
    suppression_code = _clean(row.get("NOSUMS"))
    key_date_source = _value_source(row.get("KEYDATEFLAG"))

    reasons: list[str] = []
    if currency is not None:
        reasons.append(
            "values are translated to "
            + _describe_target(currency.target_currency, currency.target_source, "currency")
            + (
                f" using translation type {currency.translation_type}"
                if currency.translation_type
                else ""
            )
        )
    if unit is not None:
        reasons.append(
            "values are converted to "
            + _describe_target(unit.target_unit or unit.unit_infoobject, unit.target_source, "unit")
        )
    if aggregation is not None and not aggregation.is_summation:
        reasons.append(
            f"aggregates locally as {aggregation.label or aggregation.code}, so the figure "
            "shown is not the sum of the rows beneath it"
        )
    if _clean(row.get("SIGNINV")) == _TRUE:
        reasons.append("the sign is inverted on display, so the figure carries the opposite sign")
    if _clean(row.get("CUMUL")) == _TRUE:
        reasons.append("values are displayed cumulatively, so each figure includes the ones before")
    if _clean(row.get("CONSTSEL")) == _TRUE:
        reasons.append(
            "constant selection is set, so this element ignores the drilldown and the navigation "
            "state the rest of the report responds to"
        )
    if key_date_source is not None:
        reasons.append(
            "a key date of its own governs which time-dependent master data is read"
            + (" and it resolves per execution" if key_date_source.runtime_resolved else "")
        )
    if hierarchy is not None and hierarchy.source is not None and hierarchy.source.runtime_resolved:
        reasons.append(
            "the display hierarchy is chosen by a variable, so the grouping differs per execution"
        )

    return ElementProperties(
        eltuid=eltuid,
        currency_translation=currency,
        unit_conversion=unit,
        display_hierarchy=hierarchy,
        local_aggregation=aggregation,
        local_aggregation_direction=_AGGREGATION_DIRECTION.get(direction_code)
        if direction_code not in {"", "0"}
        else None,
        total_suppression=_TOTAL_SUPPRESSION.get(suppression_code, suppression_code) or None,
        total_suppressed=suppression_code in {"U", "C"},
        display=_DISPLAY_STATE.get(display_code, display_code) or None,
        hidden=display_code in _HIDDEN_STATES,
        sign_inverted=_clean(row.get("SIGNINV")) == _TRUE,
        constant_selection=_clean(row.get("CONSTSEL")) == _TRUE,
        cumulative=_clean(row.get("CUMUL")) == _TRUE,
        key_date=_clean(row.get("KEYDATE")) or None,
        key_date_source=key_date_source,
        changes_the_number=reasons,
        provenance=provenance,
    )
