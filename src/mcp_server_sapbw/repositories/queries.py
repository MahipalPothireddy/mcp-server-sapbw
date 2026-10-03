"""BEx query repository (B7, mission Section 6).

Reads the query directory (RSZCOMPDIR), the query's own description via the RSZELTTXT/COMPUID join
(a query is itself an element), the element tree (RSZELTXREF parent->child), element definitions
(RSZELTDIR), restrictions (RSZRANGE), and variables (RSZGLOBV) with their processing type. Code
values (DEFTP / LAYTP / VPROCTP / VARTYP / RSZTYPEFLAG) were decoded live from DD07T in B7. All RSZ*
tables are OBJVERS-versioned (auto ``OBJVERS='A'`` via the dialect).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..core.dialect import quote_ident
from ..models.aggregation import AggregationRule, ExceptionAggregation
from ..models.completeness import COMPLETE, bounded
from ..models.provenance import UnsupportedResult
from ..models.queries import (
    AlertLevel,
    DecodedCode,
    ElementProperties,
    ElementRole,
    ElementType,
    FieldLineageHop,
    FieldLineagePath,
    Query,
    QueryAxis,
    QueryCondition,
    QueryElement,
    QueryElementEdge,
    QueryLineage,
    QueryOrigin,
    QueryOriginFilter,
    QuerySummary,
    QueryUsage,
    QueryVariable,
    Restriction,
    ValueHolds,
    ValueSource,
    VariableKind,
    VariableProcessingType,
)
from ..services.aggregation import build_exception_aggregation, decode_query_aggregation
from ..services.element_properties import build_element_properties
from ..services.field_lineage import FieldLineageService
from ..services.lineage import LineageService
from .base import Repository

#: One ``RSZELTDIR`` row as the element builder needs it: ``(DEFTP, MAPNAME, REUSABLE, SUBDEFTP)``.
#: Named because it grew a fourth member for D26 and three call sites had to agree about the arity.
_DirectoryRow = tuple[Any, Any, Any, Any]

_DEFTP_TO_TYPE: dict[str, ElementType] = {
    "REP": "query",
    # SEL is resolved by _selection_type(), not here: one code, two meanings (D28).
    "SEL": "selection",
    "CKF": "calculated_key_figure",
    "FML": "formula",
    "VAR": "variable",
    "STR": "structure",
    "SOB": "filter",
    "SHT": "query_sheet",  # the layout node owning the axes; was falling through to unknown (D29)
    "CEL": "cell",
    "ATR": "attribute",
    "NIL": "none",
}
#: The key-figure dimension. A selection restricting it is key-figure-side; one that does not is a
#: characteristic placement. This single name is the whole D28 discriminator.
_KEY_FIGURE_DIMENSION = "1KYFNM"

#: The pseudo-InfoObjects a condition's and an exception's rows are keyed under, in both
#: ``RSZSELECT`` and ``RSZRANGE``. Neither is a business characteristic, so neither is reported as
#: one.
#:
#: **These are two different names and assuming one was D41.** The first version of the condition
#: reader filtered on ``1CONDITION`` alone, which matches nothing for an exception - so all 77
#: exceptions came back with no operator, no threshold, no measure, and ``active=False`` by the
#: missing-row default, while **71 of 77 are actually switched on**. Measured, the partition is
#: clean with no crossover: ``SUBDEFTP='CON'`` only ever files under ``1CONDITION`` (134 select, 254
#: range rows) and ``SUBDEFTP='EXC'`` only ever under ``1EXCEPTION`` (77 and 267). Both names are
#: matched rather than dispatched on kind, so a release that swapped them is still read correctly.
_CONDITION_DIMENSION = "1CONDITION"
_EXCEPTION_DIMENSION = "1EXCEPTION"
_CONDITION_DIMENSIONS = (_CONDITION_DIMENSION, _EXCEPTION_DIMENSION)

#: ``RSZELTDIR.SUBDEFTP`` -> element type, for the ``SEL`` codes only (D26). Domain ``RSZSUBDEFTP``
#: declares 14 values; these are the four that a ``DEFTP='SEL'`` element carries on the reference
#: system, with the active-row counts that decide how much each branch matters:
#:
#:   CHA  Restricted Characteristic   50,956    RKF  Restricted Key Figure     509
#:   STM  Structure Element           12,357    CON  Condition                 127
#:                                              EXC  Exception                  77
#:
#: ``CHA`` and ``RKF`` are listed even though they change no output - measured across every active
#: ``SEL`` element, ``CHA`` never restricts ``1KYFNM`` and ``RKF`` always does, so the declared
#: column and the D28 heuristic agree perfectly on both. Mapping them here moves the basis from
#: inference to SAP's own statement without moving a single element.
#:
#: ``STM`` is deliberately **absent**. The heuristic calls 12,201 of them restricted key figures and
#: 156 characteristics; SAP calls all 12,357 structure elements. Which of those a reader should see
#: is a question about what Query Designer shows, and no ground truth has been collected for it, so
#: the fix leaves STM on the existing path rather than reclassifying 12,357 elements on my own
#: authority. Recorded as an open finding instead.
_SUBDEFTP_TO_TYPE: dict[str, ElementType] = {
    "CON": "condition",
    "EXC": "exception",
    "CHA": "characteristic",
    "RKF": "restricted_key_figure",
}

#: ``RSZSELECT.CONTYPE``, domain ``RRXCONTYPE`` as ``DD07T`` documents it on 7.50. A condition
#: element's row says ``1`` and an exception's says ``2``, which is a second declared column stating
#: what ``SUBDEFTP`` already said.
_CONTYPE_LABELS: dict[str, str] = {
    "1": "Condition",
    "2": "Exception",
    "3": "Selection",
    "4": "Local Invoice",
    "5": "Sort Information for FilterSpace",
}

#: ``RSZRANGE.OPT``, domain ``RSZ_OPERATOR_DOMAIN``. The ten declared values are threshold
#: comparisons. **The ranking operators are not declared** - this system uses ``TC`` 107 times and
#: ``BC`` 4 times on condition rows and the domain lists neither - the same shape of dictionary gap
#: as the undeclared ``OBJVERS='R'`` behind D33. So they are separated below by what is known:
#: ``TC`` is labelled from evidence (the customer's Query Designer export reads "Top N" on 4 of 4
#: conditions that carry it) and ``BC`` is left unlabelled, because inventing "Bottom N" from
#: symmetry would be a guess presented as a decode.
_OPT_DECLARED: dict[str, str] = {
    "EQ": "Equal: Single Value",
    "NE": "Not Equal: Everything Apart from the Specified Single Value",
    "BT": "Between: Range of Values",
    "NB": "Not Between: Everything Outside the Range",
    "LE": "Less or Equal: Everything <= Value in Field LOW",
    "GT": "Greater Than: Everything > Value in Field LOW",
    "GE": "Greater or Equal: Everything >= Value in Field LOW",
    "LT": "Less Than: Everything < Value in Field LOW",
    "CP": "Contains Pattern: Masked Input: Find Pattern",
    "NP": "Not Contains Pattern: Masked Input: Reject Pattern",
}
_OPT_OBSERVED: dict[str, str] = {"TC": "Top N (count)"}

#: ``RSZRANGE.LOWFLAG``, domain ``RSZTYPEFLAG`` - a declared value-source flag, which is why a
#: condition's threshold can be reported as "a variable" exactly rather than guessed at from the
#: shape of the string in ``LOW``.
_LOWFLAG_TO_SOURCE: dict[str, tuple[str, ValueHolds, bool]] = {
    "0": ("Blank", "unknown", False),
    "1": ("Value", "literal", False),
    "2": ("CIN link", "element_uid", False),
    "3": ("Variable CIN", "variable_name", True),
    "4": ("InfoObject", "infoobject", False),
    "5": ("Constant", "constant", False),
    "6": ("Exit variable name (screen filter)", "variable_name", True),
}

#: ``RSZRANGE.FACIOBJNM``: what each row is for. Used instead of ``ENUM`` ordering, because ordering
#: is how the rows happen to be stored and this is what they mean. Anything that is neither of these
#: is a **real characteristic**, and on an exception those rows are the drilldown levels it is
#: evaluated at - dropped entirely by the first version of this reader (D41).
_CONDITION_MEASURE_FACET = "1STRUC"  # LOW is the ranked key figure's element uid
_CONDITION_VALUE_FACET = "1VALUE"  # OPT is the operator, LOW/HIGH the threshold

#: ``RSZRANGE.ALERTLEVEL``, domain ``RSRA_ALERT_LEVEL`` as ``DD07T`` documents it on 7.50. Every one
#: of the nine occurs on the reference system's exception rows. ``00`` is not a level: it is what
#: the measure row and the drilldown rows carry, and what **all 254** condition range rows carry -
#: is the measured proof that alert levels are an exception-only concept.
_ALERT_LEVELS: dict[str, str] = {
    "01": "Good 1",
    "02": "Good 2",
    "03": "Good 3",
    "04": "Critical 1",
    "05": "Critical 2",
    "06": "Critical 3",
    "07": "Bad 1",
    "08": "Bad 2",
    "09": "Bad 3",
}
_NO_ALERT_LEVEL = "00"

#: ``RSZSELECT.EXCABSREL``, domain ``RSRA_ABS_REL``: whether an exception colours only the result
#: rows or every row. Blank/``0`` occurs too and is not declared, so it degrades to advisory rather
#: than being read as one of the two.
_EXCABSREL_LABELS: dict[str, str] = {"1": "Results Only", "2": "All"}


def _decode_contype(code: str | None) -> DecodedCode | None:
    """``RSZSELECT.CONTYPE`` against its declared domain ``RRXCONTYPE``."""
    text = (code or "").strip()
    if not text:
        return None
    label = _CONTYPE_LABELS.get(text)
    return DecodedCode(code=text, label=label, confidence="dictionary" if label else "advisory")


def _decode_operator(code: str | None) -> DecodedCode | None:
    """``RSZRANGE.OPT`` on a condition, honest about the two codes SAP does not declare.

    The declared operators are threshold comparisons and decode from the domain. The ranking
    operators do not appear in the domain at all: ``TC`` is used 107 times and ``BC`` 4 times on
    condition rows of the reference system, and ``RSZ_OPERATOR_DOMAIN`` lists neither.

    ``TC`` still gets a label, because there is evidence for it - the customer's Query Designer
    export reads "Top N" on 4 of 4 conditions carrying it - but the confidence stays ``advisory`` to
    say the label came from observation rather than from SAP. ``BC`` gets **no label**: symmetry
    makes "Bottom N" overwhelmingly likely, and that is precisely why it must not become a decode.
    The raw code is reported either way, so a reader can see the thing that was not resolved.
    """
    text = (code or "").strip().upper()
    if not text:
        return None
    declared = _OPT_DECLARED.get(text)
    if declared:
        return DecodedCode(code=text, label=declared, confidence="dictionary")
    return DecodedCode(code=text, label=_OPT_OBSERVED.get(text), confidence="advisory")


def _decode_alert_level(code: str | None) -> DecodedCode:
    """``RSZRANGE.ALERTLEVEL`` against its declared domain ``RSRA_ALERT_LEVEL``."""
    text = (code or "").strip()
    label = _ALERT_LEVELS.get(text)
    return DecodedCode(code=text, label=label, confidence="dictionary" if label else "advisory")


def _decode_evaluation_scope(code: str | None) -> DecodedCode | None:
    """``RSZSELECT.EXCABSREL``: whether an exception colours result rows only, or all of them."""
    text = (code or "").strip()
    if not text:
        return None
    label = _EXCABSREL_LABELS.get(text)
    return DecodedCode(code=text, label=label, confidence="dictionary" if label else "advisory")


def _decode_threshold_source(code: str | None) -> ValueSource | None:
    """``RSZRANGE.LOWFLAG`` against its declared domain ``RSZTYPEFLAG``."""
    text = (code or "").strip()
    if not text:
        return None
    entry = _LOWFLAG_TO_SOURCE.get(text)
    if entry is None:
        return ValueSource(code=text, confidence="advisory", value_holds="unknown")
    label, holds, runtime = entry
    return ValueSource(
        code=text,
        label=label,
        confidence="dictionary",
        value_holds=holds,
        runtime_resolved=runtime,
    )


def _selection_type(selected: list[str]) -> ElementType:
    """Classify a ``DEFTP='SEL'`` element from the InfoObjects it selects on (D28).

    BW gives restricted key figures and plain characteristic placements the same type code, so the
    element row alone cannot tell them apart. ``RSZSELECT`` can: a key-figure-side selection selects
    on ``1KYFNM``, the key-figure dimension, and a characteristic placement selects on the
    characteristic. Measured across 400 production queries this splits 6,480 key-figure-side from
    54,788 characteristic placements, with no overlap.

    It has to be ``RSZSELECT`` and not ``RSZRANGE``. A characteristic sitting on an axis with no
    value restriction - which is what a free characteristic *is* - has a ``RSZSELECT`` row naming it
    and no ``RSZRANGE`` row at all. Classifying from the ranges therefore left 32 of the subject
    query's placements unclassified while the answer sat in the other table.

    A selection with no ``RSZSELECT`` row either is left as ``selection`` rather than assumed.
    """
    if not selected:
        return "selection"
    if any(name.strip().upper() == _KEY_FIGURE_DIMENSION for name in selected):
        return "restricted_key_figure"
    return "characteristic"


#: (parent element DEFTP, edge LAYTP) -> where the child sits. Measured across 400 active production
#: queries; the counts are edges observed for that pair. Pairs absent here report ``unknown`` and
#: name themselves in ``axis_basis`` rather than being folded into a neighbouring meaning (D30).
_AXIS_BY_PARENT: dict[tuple[str, str], QueryAxis] = {
    ("SHT", "ROW"): "rows",  # 837
    ("SHT", "COL"): "columns",  # 425
    ("SHT", "AGG"): "free_characteristics",  # 26,748 - pure SEL/characteristic
    ("SHT", "FLT"): "structure_member",  # 8,057 - SEL/key-figure + FML, all key-figure-side
    ("SHT", "ATR"): "attribute_order",  # 18
    ("SOB", "AGG"): "filter",  # 28,040 - pure SEL/characteristic
    # A structure reached again from the filter node. Its real placement comes from the sheet, so
    # this is a reference; mapping it to ``columns`` would double-count the axis.
    ("SOB", "COL"): "reference",  # 372
    ("SOB", "ROW"): "reference",  # 3
    ("REP", "VAR"): "variable_sequence",
    ("REP", "SHT"): "structural",
    ("REP", "SOB"): "structural",
}
#: ``NIL`` means "referenced, not placed" under every parent, so it is settled before the pair map
#: is consulted. 44 of the measured edges reach it from a nested reusable definition.
_UNPLACED_LAYTP = "NIL"


def _derive_axis(parent_deftp: str | None, laytp: str | None) -> tuple[QueryAxis, str]:
    """Where a child sits, from its parent's type and the edge's layout code together (D30)."""
    parent = (parent_deftp or "").strip().upper() or "?"
    code = (laytp or "").strip().upper() or "?"
    basis = f"parent DEFTP={parent}, LAYTP={code}"
    if code == _UNPLACED_LAYTP:
        return "unplaced", basis
    axis = _AXIS_BY_PARENT.get((parent, code))
    if axis is None:
        return "unknown", f"{basis} (pair has no measured meaning)"
    return axis, basis


# Every value of the LAYTP domain RSZLAYTP as DD07T documents it on 7.50, read from the live
# dictionary rather than recalled. Nine of these were mapped and eight fell through to ``other``,
# which on the reference system silently bucketed 24.4% of all element-tree edges (D24). The
# right-hand comments are SAP's own texts.
_LAYTP_TO_ROLE: dict[str, ElementRole] = {
    "ROW": "rows",  # Row
    "COL": "columns",  # Column
    "FIX": "filter",  # Filter
    "FLT": "free",  # Formatted Reporting - Order
    "VAR": "variable",  # Variable Sequence
    "CEL": "cell",  # Cell
    "MBR": "structure_member",  # Structure element
    "NAV": "navigation",  # Navigation
    "AGG": "aggregated",  # Aggregated
    "NIL": "unplaced",  # No Layout - referenced, not placed on an axis
    "SHT": "query_sheet",  # Query Sheet
    "SOB": "selection_object",  # Selection Object
    "ATR": "attribute_order",  # Order of Attributes
    "QVR": "query_variable_sequence",  # Variable Squence of Query Variables [sic, SAP's spelling]
    "OPD": "operand",  # Operand
    "RNG": "area",  # Area
    "REP": "internal",  # Internal Use
}
# SAP's technical-name prefix for a query created ad hoc in the BEx Analyzer rather than in Query
# Designer. See models.queries.QueryOrigin for what this does and does not establish.
_AD_HOC_PREFIX = "!!"


def classify_origin(compid: str | None) -> QueryOrigin:
    """Classify a query by the shape of its technical name. Name-based, never a stored flag."""
    return "ad_hoc" if compid is not None and compid.startswith(_AD_HOC_PREFIX) else "designed"


_VPROCTP_TO_TYPE: dict[str, VariableProcessingType] = {
    "1": "replacement_path",
    "3": "customer_exit",
    "4": "sap_exit",
    "5": "user_entry",
    "6": "authorization",
    "7": "hana_exit",
}
_VARTYP_TO_KIND: dict[str, VariableKind] = {
    "1": "characteristic",
    "2": "hierarchy_node",
    "3": "text",
    "4": "formula",
    "5": "hierarchy",
}
_VARIABLE_FLAG = "3"  # RSZTYPEFLAG: LOW/HIGH holds a variable reference
_MAX_ELEMENTS = 500
_MAX_DEPTH = 10
#: Upstream depth for the provider-boundary fallback. A BEx query's provider normally sits on a
#: CompositeProvider over an ADSO over one or more DSO layers, so 6 stopped short of the DataSource
#: on the measured system (12 of 65 reached) while 8 matches ``bw_trace_to_source``'s own default.
_PROVIDER_TRACE_DEPTH = 8
#: DataSources named in the boundary hop. The set is a summary rather than a path, so this bounds
#: the reply without losing the count - which is reported in full alongside.
_MAX_BOUNDARY_DATASOURCES = 25
_TS_DIGITS = 8  # leading YYYYMMDD of an RSTIMESTMP decimal
_LANGUAGE = "E"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _restriction_key(restriction: Restriction) -> tuple[Any, ...]:
    """What makes two restrictions the same one, for deduplication during the filter rollup."""
    return (
        (restriction.iobjnm or "").strip().upper(),
        restriction.operator,
        restriction.sign,
        restriction.low,
        restriction.high,
        restriction.low_is_variable,
        restriction.high_is_variable,
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp_to_date(value: Any) -> date | None:
    """Parse the leading YYYYMMDD of an RSTIMESTMP decimal to a date."""
    if value is None:
        return None
    try:
        text = str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
    digits = text.split(".", maxsplit=1)[0]
    if len(digits) < _TS_DIGITS or not digits.isdigit() or digits[:_TS_DIGITS] == "00000000":
        return None
    try:
        return datetime.strptime(digits[:_TS_DIGITS], "%Y%m%d").date()
    except ValueError:
        return None


_MAX_LINEAGE_IOBJ = 100
# RSZCALC rows pulled per query. An element can hold many calculation steps, so this is generous
# relative to the 500-element tree cap while still bounding a pathological formula.
_CALC_ROW_CAP = 5000

# RSZELTPROP columns read, in SELECT order. RSZELTPROP has 100 columns; these are the ones that say
# something about what a value means rather than how it is formatted on screen. Named explicitly so
# the row -> dict zip stays aligned, and so adding one is a visible change.
_PROPERTY_COLUMNS: tuple[str, ...] = (
    "ELTUID",
    "TCUR",
    "TCURFLAG",
    "CTTNM",
    "TCURDATE",
    "TCURDATEFLAG",
    "UOMNM",
    "TUOM",
    "TUOMFLAG",
    "HIENM",
    "HIENMFLAG",
    "VERSION",
    "DATETO",
    "STRT_LVL",
    "HRY_ACTIVE",
    "STRMEM_LAGGR",
    "LAGGR_DIR",
    "NOSUMS",
    "HIDDEN",
    "SIGNINV",
    "CONSTSEL",
    "CUMUL",
    "KEYDATE",
    "KEYDATEFLAG",
)
# One RSZELTPROP row per element, so this only ever binds on a tree at the element cap.
_PROPERTY_ROW_CAP = 1000

# A caveat points at its evidence; it is not a second copy of it.
_CAVEAT_NAMES = 8
_CAVEAT_REASONS = 6


@dataclass
class _ConditionRanges:
    """One condition's or exception's ``RSZRANGE`` rows, sorted by what each row is for.

    Mutable and accumulated row by row, because an exception's alert levels and drilldown
    characteristics each arrive as several rows. The first version of this reader was a 4-tuple that
    kept one threshold, which is why a three-band exception reported one band (D41).
    """

    measure: str | None = None
    #: Condition shape: a single threshold with its operator and value-source flag.
    operator: str | None = None
    threshold: str | None = None
    lowflag: str | None = None
    #: Exception shape: ``(ALERTLEVEL, OPT, LOW, HIGH)`` per band.
    levels: list[tuple[str, str | None, str | None, str | None]] = field(default_factory=list)
    #: Exception shape: the characteristics it is evaluated at.
    drilldown: list[str] = field(default_factory=list)


@dataclass
class _CalcAggregation:
    """Aggregation accumulated across one element's RSZCALC calculation steps."""

    step_count: int = 0
    standard: AggregationRule | None = None
    exception: ExceptionAggregation | None = None
    # Steps of the same element declared different exception aggregations.
    mixed_steps: bool = False


class QueriesRepository(Repository):
    """BEx query header, element tree, restrictions, variables, usage, and field lineage."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._lineage = LineageService(connection, capability, cache)

    # --- listing -------------------------------------------------------------------------

    def list_queries(
        self,
        *,
        provider: str | None = None,
        owner: str | None = None,
        origin: QueryOriginFilter = "all",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[QuerySummary], int] | UnsupportedResult:
        unsupported = self.require("query_dir")
        if unsupported is not None:
            return unsupported

        # OBJVERS first, and not optional. RSZCOMPDIR holds a row per *version* of a component, so
        # without it one query is counted once per version it exists in - and OBJSTAT = 'ACT' does
        # not substitute, because it is the activation state of the row, not the version of it.
        # Measured on the reference system: 2,373 rows for 1,069 active queries (A 1,069, M 890,
        # D 279, B 135), so the catalogue over-reported by 2.2x, the index listed every query up to
        # four times with a different "last used" against each, and a documentation run rebuilt the
        # same page for each duplicate. This is the exact failure mission rule 6 describes.
        where = ["OBJVERS = 'A'", "OBJSTAT = 'ACT'"]
        params: list[Any] = []
        # RSZCOMPDIR lists all reusable components; restrict to actual queries (root DEFTP='REP').
        rep_filter = self._query_only_filter()
        if rep_filter:
            where.append(rep_filter)
        if owner:
            where.append("OWNER = ?")
            params.append(owner)
        # Parameterised rather than an inline literal. "!" is not a LIKE metacharacter and is not
        # the dialect's escape character, so the pattern needs no escaping.
        if origin == "designed":
            where.append("COMPID NOT LIKE ?")
            params.append(f"{_AD_HOC_PREFIX}%")
        elif origin == "ad_hoc":
            where.append("COMPID LIKE ?")
            params.append(f"{_AD_HOC_PREFIX}%")
        if provider:
            compuids = self._compuids_for_provider(provider)
            if not compuids:
                return [], 0
            placeholders = ", ".join("?" for _ in compuids)
            where.append(f"COMPUID IN ({placeholders})")
            params.extend(compuids)

        base = self.dialect.build_select(
            columns=["COMPUID", "COMPID", "OWNER", "LASTUSED"],
            from_logical="query_dir",
            where=where,
            params=params,
            order_by=["COMPID"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        compuids = [str(r[0]) for r in rows]
        texts = self._element_texts(compuids)
        providers = self._providers_for(compuids)

        summaries: list[QuerySummary] = []
        for compuid, compid, owner_val, lastused in rows:
            cu = str(compuid)
            name = _clean(compid)
            summaries.append(
                QuerySummary(
                    compuid=cu,
                    compid=name,
                    description=texts.get(cu, (None, None))[1] or texts.get(cu, (None, None))[0],
                    provider=providers.get(cu),
                    owner=_clean(owner_val),
                    last_used=_timestamp_to_date(lastused),
                    origin=classify_origin(name),
                    provenance=self.provenance("query_dir", {"COMPUID": cu, "OBJVERS": "A"}),
                )
            )
        return summaries, total

    # --- full definition -----------------------------------------------------------------

    def get_query(self, identifier: str) -> Query | UnsupportedResult:
        """Full query definition. Cached (scope ``query``).

        The recursive RSZELTXREF walk plus the RSZELTTXT/RSZSELECT/RSZRANGE/RSZCALC joins are the
        most query-heavy read in the server, and a query definition only changes on re-activation.
        """
        return self.cached_model(
            "query",
            identifier,
            model=Query,
            build=lambda: self._get_query_uncached(identifier),
            # A not-found shell carries no COMPID; never cache "does not exist".
            cache_when=lambda query: query.compid is not None,
        )

    def _get_query_uncached(self, identifier: str) -> Query | UnsupportedResult:
        unsupported = self.require("query_dir", "element_dir", "element_xref")
        if unsupported is not None:
            return unsupported
        header = self._header(identifier)
        if header is None:
            return Query(
                compuid=identifier,
                active=False,
                caveats=["query not found"],
                provenance=self.provenance("query_dir", {"COMPID": identifier}),
            )
        compuid, compid, owner, changed_by, lastused, objstat = header

        eltuids, edge_rows, truncated = self._element_tree(compuid)
        directory = self._element_directory(eltuids)
        texts = self._element_texts(list(eltuids))
        restrictions = self._restrictions(list(eltuids))
        selected = self._selected_infoobjects(list(eltuids))
        calc = self._calc_aggregation(list(eltuids))
        properties = self._element_properties(list(eltuids))

        # The filter node stores nothing itself; its children carry the restrictions (D31). Rolled
        # up before elements are built so the filter element answers "what does this query filter
        # on" instead of returning an empty list while the answer sits one hop away.
        restrictions = self._rollup_filter_restrictions(directory, edge_rows, restrictions)

        elements = [
            self._build_element(
                uid,
                directory.get(uid),
                texts.get(uid),
                restrictions.get(uid, []),
                selected=selected.get(uid, []),
                calc=calc.get(uid),
                properties=properties.get(uid),
            )
            for uid in sorted(eltuids)
        ]
        parent_deftp = {uid: (_clean(row[0]) if row else None) for uid, row in directory.items()}
        edges = []
        for p, c, laytp, posn in edge_rows:
            axis, axis_basis = _derive_axis(parent_deftp.get(p), laytp)
            edges.append(
                QueryElementEdge(
                    parent_uid=p,
                    child_uid=c,
                    role=_LAYTP_TO_ROLE.get(str(laytp).strip().upper(), "other"),
                    role_code=_clean(laytp),
                    axis=axis,
                    axis_basis=axis_basis,
                    position=_as_int(posn),
                    provenance=self.provenance("element_xref", {"SELTUID": p, "TELTUID": c}),
                )
            )
        providers = self._providers_list(compuid)
        variables = self._variables(elements, restrictions)
        conditions = self._conditions(elements, texts)

        return Query(
            compuid=compuid,
            compid=_clean(compid),
            description=(texts.get(compuid) or (None, None))[1]
            or (texts.get(compuid) or (None, None))[0],
            active=str(objstat).strip() == "ACT",
            provider=providers[0] if providers else None,
            providers=providers,
            owner=_clean(owner),
            last_changed_by=_clean(changed_by),
            last_used=_timestamp_to_date(lastused),
            elements=elements,
            edges=edges,
            variables=variables,
            conditions=conditions,
            # Names the bound rather than only flagging it (D6). A capped element tree matters
            # beyond this response: field-level lineage walks these elements, so an incomplete tree
            # produces incomplete lineage that would otherwise look whole.
            completeness=(
                bounded("row_cap", scope="element_tree", limit=_MAX_ELEMENTS)
                if truncated
                else COMPLETE
            ),
            caveats=[
                *(["element tree capped"] if truncated else []),
                *self._aggregation_caveats(elements),
                *self._property_caveats(elements),
            ],
            provenance=self.provenance("query_dir", {"COMPUID": compuid, "OBJVERS": "A"}),
        )

    @staticmethod
    def _rollup_filter_restrictions(
        directory: dict[str, _DirectoryRow],
        edge_rows: list[tuple[Any, Any, Any, Any]],
        restrictions: dict[str, list[Restriction]],
    ) -> dict[str, list[Restriction]]:
        """Give the filter node the restrictions its children hold (D31).

        ``DEFTP='SOB'`` is the query's global filter, and on the reference system it has no
        ``RSZSELECT`` row of its own - every restriction hangs off a child selection. So the filter
        element reported ``restrictions: []`` while the query plainly filtered on nine
        characteristics, six of them by authorisation variable. That is the same exposure the
        unsupported ``RSECVAL`` path already fails to report, so the two gaps compounded.

        The children's restrictions are copied onto the parent rather than moved: the child is
        still where the restriction is stored, and its provenance still says so. Deduplicated on
        the fields that identify a restriction, because a filter reached from more than one edge
        would otherwise list the same one twice.
        """
        filter_uids = {
            uid for uid, row in directory.items() if row and str(row[0]).strip().upper() == "SOB"
        }
        if not filter_uids:
            return restrictions
        children: dict[str, list[str]] = {uid: [] for uid in filter_uids}
        for parent, child, _laytp, _posn in edge_rows:
            if parent in children:
                children[parent].append(str(child))
        rolled = dict(restrictions)
        for uid, kids in children.items():
            seen: set[tuple[Any, ...]] = set()
            collected: list[Restriction] = list(rolled.get(uid, []))
            for existing in collected:
                seen.add(_restriction_key(existing))
            for kid in kids:
                for restriction in restrictions.get(kid, []):
                    key = _restriction_key(restriction)
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(restriction)
            if collected:
                rolled[uid] = collected
        return rolled

    def _build_element(
        self,
        uid: str,
        directory: _DirectoryRow | None,
        text: tuple[str | None, str | None] | None,
        restrictions: list[Restriction],
        *,
        selected: list[str] | None = None,
        calc: _CalcAggregation | None = None,
        properties: ElementProperties | None = None,
    ) -> QueryElement:
        deftp, mapname, reusable, subdeftp = directory if directory else (None, None, None, None)
        code = str(deftp).strip()
        element_type = _DEFTP_TO_TYPE.get(code, "unknown")
        if element_type == "selection":
            # SUBDEFTP first: it is SAP's own statement of the type, and it is the only route to a
            # condition or an exception, neither of which restricts 1KYFNM. Falling back to the D28
            # heuristic keeps the 1,581 blank-SUBDEFTP elements and the 12,357 structure elements on
            # exactly the behaviour they already had (D26).
            element_type = _SUBDEFTP_TO_TYPE.get(str(subdeftp).strip().upper()) or _selection_type(
                selected or []
            )
        # The InfoObject a characteristic placement places. It has no MAPNAME of its own, so without
        # this it is an anonymous row - which is why only 31 of the subject query's 107 elements
        # carried any identifier. 1KYFNM is the key-figure dimension, not a business characteristic,
        # so it is not reported as one.
        iobjnm = next(
            (name for name in (selected or []) if name.strip().upper() != _KEY_FIGURE_DIMENSION),
            None,
        )
        return QueryElement(
            eltuid=uid,
            element_type=element_type,
            iobjnm=iobjnm if element_type == "characteristic" else None,
            name=_clean(mapname),
            description=(text or (None, None))[1] or (text or (None, None))[0],
            reusable=str(reusable).strip() == "X",
            restrictions=restrictions,
            calc_step_count=calc.step_count if calc else 0,
            standard_aggregation=calc.standard if calc else None,
            exception_aggregation=calc.exception if calc else None,
            properties=properties,
            provenance=self.provenance("element_dir", {"ELTUID": uid, "OBJVERS": "A"}),
        )

    # --- element properties (RSZELTPROP) --------------------------------------------------

    def _element_properties(self, eltuids: list[str]) -> dict[str, ElementProperties]:
        """Per-element settings that change what a value means.

        One row per element, so one query for the whole tree. An element with no row simply has no
        properties configured; an unavailable table is reported by :meth:`_property_caveats` rather
        than left to look like "nothing is configured anywhere".
        """
        if not eltuids or not self.capability.is_available("element_prop"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=list(_PROPERTY_COLUMNS),
                    from_logical="element_prop",
                    where=[f"ELTUID IN ({placeholders})"],
                    params=list(eltuids),
                    order_by=["ELTUID"],
                ),
                limit=_PROPERTY_ROW_CAP,
            )
        )
        found: dict[str, ElementProperties] = {}
        for row in rows:
            record = dict(zip(_PROPERTY_COLUMNS, row, strict=False))
            uid = _clean(record.get("ELTUID"))
            if uid is None:
                continue
            found[uid] = build_element_properties(
                record,
                eltuid=uid,
                provenance=self.provenance("element_prop", {"ELTUID": uid, "OBJVERS": "A"}),
            )
        return found

    def _property_caveats(self, elements: list[QueryElement]) -> list[str]:
        """Say which elements do something to their value, and when the table could not be read."""
        if not self.capability.is_available("element_prop"):
            return [
                f"element display and calculation settings were not read: "
                f"{self.physical('element_prop')} is absent on this release, so currency "
                "translation, local aggregation, sign inversion and key-date overrides on this "
                "query are unknown rather than absent"
            ]
        altered = [e for e in elements if e.properties and e.properties.changes_the_number]
        if not altered:
            return []
        reasons = sorted(
            {r for e in altered for r in (e.properties.changes_the_number if e.properties else [])}
        )
        named = [e.name or e.eltuid for e in altered[:_CAVEAT_NAMES]]
        return [
            f"{len(altered)} element(s) alter their own value before it is displayed, so the "
            "figure shown is not the plain sum of the records behind it: "
            + "; ".join(reasons[:_CAVEAT_REASONS])
            + f". Affected: {', '.join(named)}"
        ]

    # --- aggregation (RSZCALC) -----------------------------------------------------------

    def _calc_aggregation(self, eltuids: list[str]) -> dict[str, _CalcAggregation]:
        """Aggregation per element from ``RSZCALC``.

        An element can hold several calculation steps, and exception aggregation sits on a step
        rather than on the element. The first step that declares one is taken as the element's
        aggregation, and a disagreement between steps is reported rather than silently resolved -
        a formula whose steps aggregate differently cannot be summarised by one of them.
        """
        if not eltuids or not self.capability.is_available("element_calc"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[
                        "ELTUID",
                        "AGGRGEN",
                        "AGGREXC",
                        "AGGRCHA",
                        "AGGRCHA2",
                        "AGGRCHA3",
                        "AGGRCHA4",
                        "AGGRCHA5",
                        "AGGREXCLUDE",
                    ],
                    from_logical="element_calc",
                    where=[f"ELTUID IN ({placeholders})"],
                    params=list(eltuids),
                    order_by=["ELTUID", "STEPNR"],
                ),
                limit=_CALC_ROW_CAP,
            )
        )
        found: dict[str, _CalcAggregation] = {}
        for row in rows:
            uid = _clean(row[0])
            if uid is None:
                continue
            record = found.setdefault(uid, _CalcAggregation())
            record.step_count += 1
            if record.standard is None:
                record.standard = decode_query_aggregation(row[1])
            exception = build_exception_aggregation(
                code=row[2], references=list(row[3:8]), exclude=row[8]
            )
            if exception is None:
                continue
            if record.exception is None:
                record.exception = exception
            elif record.exception.behaviour.code != exception.behaviour.code:
                record.mixed_steps = True
        for record in found.values():
            if record.mixed_steps and record.exception is not None:
                record.exception.note = (
                    "calculation steps of this element declare different exception aggregations; "
                    "the first is reported and the element cannot be characterised by it alone"
                )
        return found

    @staticmethod
    def _aggregation_caveats(elements: list[QueryElement]) -> list[str]:
        """State plainly when the query holds figures that summation does not reproduce."""
        non_summable = [
            e
            for e in elements
            if e.exception_aggregation is not None
            and not e.exception_aggregation.reproducible_by_summation
        ]
        if not non_summable:
            return []
        named = [e.name or e.eltuid for e in non_summable[:8]]
        behaviours = sorted({e.exception_aggregation.behaviour.code for e in non_summable})  # type: ignore[union-attr]
        return [
            f"{len(non_summable)} element(s) carry exception aggregation "
            f"({', '.join(behaviours)}), so their values are not reproduced by adding the "
            "underlying rows up. Comparing such a figure against a summed total will differ "
            f"legitimately: {', '.join(named)}"
        ]

    # --- usage ---------------------------------------------------------------------------

    def get_query_usage(
        self, identifier: str, *, stale_days: int = 365
    ) -> QueryUsage | UnsupportedResult:
        unsupported = self.require("query_dir")
        if unsupported is not None:
            return unsupported
        header = self._header(identifier)
        if header is None:
            return QueryUsage(
                compuid=identifier,
                decommission_candidate=False,
                reason="query not found",
                provenance=self.provenance("query_dir", {"COMPID": identifier}),
            )
        compuid, compid, _owner, _changed, lastused, _objstat = header
        last_used = _timestamp_to_date(lastused)
        if last_used is None:
            candidate, reason = True, "never used (no LASTUSED recorded)"
        else:
            age = (datetime.now().date() - last_used).days
            candidate = age > stale_days
            reason = f"last used {age} days ago" + (" (stale)" if candidate else "")
        return QueryUsage(
            compuid=compuid,
            compid=_clean(compid),
            last_used=last_used,
            decommission_candidate=candidate,
            reason=reason,
            provenance=self.provenance("query_dir", {"COMPUID": compuid, "OBJVERS": "A"}),
        )

    # --- field-level lineage -------------------------------------------------------------

    def get_query_lineage(self, identifier: str) -> QueryLineage | UnsupportedResult:
        """Per-InfoObject lineage toward the DataSource. Cached (scope ``query_lineage``)."""
        return self.cached_model(
            "query_lineage",
            identifier,
            model=QueryLineage,
            build=lambda: self._get_query_lineage_uncached(identifier),
            cache_when=lambda lineage: lineage.compid is not None,
        )

    def _get_query_lineage_uncached(self, identifier: str) -> QueryLineage | UnsupportedResult:
        unsupported = self.require("query_dir", "element_xref", "transformation")
        if unsupported is not None:
            return unsupported
        header = self._header(identifier)
        if header is None:
            return QueryLineage(
                compuid=identifier,
                caveats=["query not found"],
                provenance=self.provenance("query_dir", {"COMPID": identifier}),
            )
        compuid, compid = str(header[0]), header[1]
        providers = self._providers_list(compuid)
        eltuids, _edges, truncated = self._element_tree(compuid)
        restrictions = self._restrictions(list(eltuids))
        directory = self._element_directory(eltuids)
        infoobjects = self._referenced_infoobjects(list(eltuids), restrictions)

        var_names: set[str] = {
            _clean(mapname)  # type: ignore[misc]
            for deftp, mapname, _reuse, _sub in directory.values()
            if str(deftp).strip() == "VAR" and _clean(mapname)
        }
        self._add_restriction_variables(restrictions, var_names)
        customer_exit = sorted(
            {v.name for v in self._fetch_variables(var_names) if v.is_customer_exit}
        )

        master = providers[0] if providers else None
        paths, boundary = self._field_paths(master, sorted(infoobjects)[:_MAX_LINEAGE_IOBJ])
        field_level = sum(1 for path in paths if path.resolution == "field")
        own_reach = sum(
            1 for path in paths if path.resolution == "field" and path.reaches_datasource
        )
        caveats = [
            f"{field_level} of {len(paths)} InfoObjects resolved to field level (followed rule by "
            "rule through RSTRANFIELD/RSTRANRULE, and through the CompositeProvider's declared "
            "model where the provider is one). The remainder show the provider's DataSource "
            "boundary with resolution='provider' and a reason - that boundary is a set of "
            "alternatives, NOT that field's own derivation.",
            f"{own_reach} of {len(paths)} InfoObjects were traced to a DataSource as their own "
            "derivation. Count only these as field lineage: a provider-level path also reports "
            "reaches_datasource=true, but that is the provider's boundary. A field-level path that "
            "stops short says where and why in unresolved_reason - the honest stops are a "
            "constant, a start/end routine rather than a field rule, and a calculation-view part "
            "whose lineage continues outside BW (bw_get_calc_view_lineage).",
            "a hop whose rule is a routine is marked advisory: BW records the rule, but what the "
            "ABAP reads is a heuristic lower bound",
            "customer-exit variable values resolve in ABAP at runtime and are not derivable",
        ]
        fanned = [
            p for p in paths if any(h.source_objects for h in p.hops if h.via != "datasource")
        ]
        if fanned:
            caveats.append(
                f"{len(fanned)} field(s) are supplied by more than one object at some hop - a "
                "CompositeProvider union feeds the same element from several parts, and each is "
                "equally the source of some of its rows. The chain follows one part, chosen by "
                "sorted name so it is reproducible, and that hop's source_objects names them all."
            )
        if truncated:
            caveats.append("element tree capped")
        return QueryLineage(
            compuid=compuid,
            compid=_clean(compid),
            providers=providers,
            paths=paths,
            provider_datasources=boundary,
            customer_exit_variables=customer_exit,
            caveats=caveats,
            provenance=self.provenance("query_dir", {"COMPUID": compuid, "OBJVERS": "A"}),
        )

    def _field_paths(
        self, master: str | None, infoobjects: list[str]
    ) -> tuple[list[FieldLineagePath], list[str]]:
        """One path per InfoObject, plus the provider's DataSource boundary set.

        Field-level where a rule or a CompositeProvider mapping was found, provider-level otherwise.
        The two are labelled differently on purpose. Returning the provider's upstream objects for
        every InfoObject makes distinct fields look identically traced, which is how a reader ends
        up believing a specific source field was identified when it was not.
        """
        if master is None:
            return [
                FieldLineagePath(
                    iobjnm=iobj,
                    provider=None,
                    resolution="none",
                    unresolved_reason="the query resolves to no InfoProvider",
                    provenance=self.provenance("query_provider", {"IOBJNM": iobj}),
                )
                for iobj in infoobjects
            ], []

        service = FieldLineageService(self._connection, self.capability, self._cache)
        fallback: tuple[list[FieldLineageHop], list[str], bool] | None = None
        paths: list[FieldLineagePath] = []
        boundary: list[str] = []
        for iobj in infoobjects:
            traced = service.trace_field(master, iobj)
            if traced.resolution == "field":
                paths.append(traced)
                continue
            # No rule populates this field: fall back to the provider's upstream, but label it.
            if fallback is None:
                fallback = self._provider_hops(master)
                boundary = fallback[1]
            hops, reached, advisory = fallback
            paths.append(
                FieldLineagePath(
                    iobjnm=iobj,
                    provider=master,
                    hops=list(hops),
                    reaches_datasource=bool(reached),
                    has_routine_hop=advisory,
                    resolution="provider",
                    unresolved_reason=traced.unresolved_reason,
                    provenance=self.provenance("element_range", {"IOBJNM": iobj}),
                )
            )
        return paths, boundary

    def _referenced_infoobjects(
        self, eltuids: list[str], restrictions: dict[str, list[Restriction]]
    ) -> set[str]:
        objs: set[str] = {r.iobjnm for rs in restrictions.values() for r in rs}
        if eltuids and self.capability.is_available("element_select"):
            placeholders = ", ".join("?" for _ in eltuids)
            rows = self.select(
                self.dialect.build_select(
                    columns=["IOBJNM"],
                    from_logical="element_select",
                    where=[f"ELTUID IN ({placeholders})"],
                    params=list(eltuids),
                )
            )
            objs.update(str(r[0]).strip() for r in rows if _clean(r[0]))
        return {o for o in objs if o}

    def _provider_hops(self, provider: str | None) -> tuple[list[FieldLineageHop], list[str], bool]:
        """The provider's DataSource boundary, as a boundary rather than as a path.

        This is the fallback for a field whose own derivation could not be found, and its shape was
        a defect in its own right. It used to append every DataSource the provider reaches as a
        *separate sequential hop*, so a reader saw ``provider -> DS1 -> DS2 -> ... -> DS65`` and had
        every reason to read it as a chain the field flows along. It is not a chain: those 65
        DataSources are alternatives, none of them established as this field's source. Measured on a
        production query the same 65 were repeated for each of 100 unresolved fields - 6,500 hops
        asserting a shape that does not exist, and 217 seconds to say nothing per field.

        Now one hop that says so, and the set itself is carried once on
        ``QueryLineage.provider_datasources`` rather than restated per field - it is a property of
        the provider, identical for every field that falls back to it.

        Returns ``(hops, datasources reached, advisory)``.
        """
        if provider is None:
            return [], [], False
        hops: list[FieldLineageHop] = [
            FieldLineageHop(object_name=provider, object_type="provider", via="provider")
        ]
        trace = self._lineage.trace_to_source(provider, depth=_PROVIDER_TRACE_DEPTH)
        if isinstance(trace, UnsupportedResult):
            return hops, [], False
        advisory = any(e.kind == "routine_lookup" for e in trace.graph.edges)
        reached = sorted(trace.datasources_reached)
        if not reached:
            return hops, [], advisory
        hops.append(
            FieldLineageHop(
                object_name=f"{len(reached)} DataSource(s) upstream of {provider}",
                object_type="datasource",
                via="datasource",
                # Always advisory: this is the provider's boundary, not a derivation of this field,
                # whatever the confidence of the edges that reached it.
                advisory=True,
                note=(
                    f"the provider reaches {len(reached)} DataSource(s), named once in "
                    "provider_datasources. They are alternatives rather than a chain, and none is "
                    "established as this field's source - this hop is the provider's boundary "
                    "because no rule for the field was found (resolution='provider'). "
                    "bw_trace_to_source on the provider gives the graph."
                ),
            )
        )
        return hops, reached, advisory

    # --- shared helpers ------------------------------------------------------------------

    def _header(self, identifier: str) -> tuple[str, Any, Any, Any, Any, Any] | None:
        """Resolve a COMPID (technical name) or COMPUID to the RSZCOMPDIR header row."""
        for column in ("COMPID", "COMPUID"):
            rows = self.select(
                self.dialect.build_select(
                    columns=["COMPUID", "COMPID", "OWNER", "TSTPNM", "LASTUSED", "OBJSTAT"],
                    from_logical="query_dir",
                    where=[f"{column} = ?"],
                    params=[identifier],
                )
            )
            if rows:
                r = rows[0]
                return str(r[0]), r[1], r[2], r[3], r[4], r[5]
        return None

    def _element_tree(self, compuid: str) -> tuple[set[str], list[tuple[str, str, Any, Any]], bool]:
        eltuids: set[str] = {compuid}
        edges: list[tuple[str, str, Any, Any]] = []
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(compuid, 0)])
        truncated = False
        while queue:
            parent, level = queue.popleft()
            if parent in visited or level >= _MAX_DEPTH:
                continue
            visited.add(parent)
            rows = self.select(
                self.dialect.build_select(
                    columns=["TELTUID", "LAYTP", "POSN"],
                    from_logical="element_xref",
                    where=["SELTUID = ?"],
                    params=[parent],
                    order_by=["POSN"],
                )
            )
            for child_raw, laytp, posn in rows:
                child = str(child_raw).strip()
                if not child:
                    continue
                edges.append((parent, child, laytp, posn))
                eltuids.add(child)
                if len(eltuids) >= _MAX_ELEMENTS:
                    truncated = True
                    break
                if child not in visited:
                    queue.append((child, level + 1))
            if truncated:
                break
        return eltuids, edges, truncated

    def _element_directory(self, eltuids: set[str]) -> dict[str, _DirectoryRow]:
        """``ELTUID -> (DEFTP, MAPNAME, REUSABLE, SUBDEFTP)``.

        ``SUBDEFTP`` was added for D26. It is the column that distinguishes a condition from a
        characteristic placement, and it had simply never been read.
        """
        if not eltuids or not self.capability.is_available("element_dir"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "DEFTP", "MAPNAME", "REUSABLE", "SUBDEFTP"],
                from_logical="element_dir",
                where=[f"ELTUID IN ({placeholders})"],
                params=list(eltuids),
            )
        )
        return {str(r[0]).strip(): (r[1], r[2], r[3], r[4]) for r in rows}

    def _element_texts(self, eltuids: list[str]) -> dict[str, tuple[str | None, str | None]]:
        if not eltuids or not self.capability.is_available("element_text"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "TXTSH", "TXTLG"],
                from_logical="element_text",
                where=["LANGU = ?", f"ELTUID IN ({placeholders})"],
                params=[_LANGUAGE, *eltuids],
            )
        )
        return {str(r[0]).strip(): (_clean(r[1]), _clean(r[2])) for r in rows}

    def _selected_infoobjects(self, eltuids: list[str]) -> dict[str, list[str]]:
        """``ELTUID -> the InfoObjects it selects on`` from ``RSZSELECT``.

        Separate from ``_restrictions`` because the two tables answer different questions.
        ``RSZRANGE`` says *what values* a selection is restricted to and is absent when there is no
        restriction; ``RSZSELECT`` says *which InfoObject* the selection is about and is present
        either way. Classifying a selection (D28) and naming the characteristic it places both need
        the second, which was never read.
        """
        if not eltuids or not self.capability.is_available("element_select"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "IOBJNM"],
                from_logical="element_select",
                where=[f"ELTUID IN ({placeholders})"],
                params=list(eltuids),
            )
        )
        result: dict[str, list[str]] = defaultdict(list)
        for eltuid, iobjnm in rows:
            name = _clean(iobjnm)
            if name is None:
                continue
            key = str(eltuid).strip()
            if name not in result[key]:
                result[key].append(name)
        return result

    def _restrictions(self, eltuids: list[str]) -> dict[str, list[Restriction]]:
        if not eltuids or not self.capability.is_available("element_range"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "IOBJNM", "SIGN", "OPT", "LOW", "HIGH", "LOWFLAG", "HIGHFLAG"],
                from_logical="element_range",
                where=[f"ELTUID IN ({placeholders})"],
                params=list(eltuids),
            )
        )
        result: dict[str, list[Restriction]] = defaultdict(list)
        for eltuid, iobjnm, sign, opt, low, high, lowflag, highflag in rows:
            iobj = _clean(iobjnm)
            if iobj is None:
                continue
            result[str(eltuid).strip()].append(
                Restriction(
                    iobjnm=iobj,
                    sign=_clean(sign),
                    operator=_clean(opt),
                    low=_clean(low),
                    high=_clean(high),
                    low_is_variable=str(lowflag).strip() == _VARIABLE_FLAG,
                    high_is_variable=str(highflag).strip() == _VARIABLE_FLAG,
                    provenance=self.provenance(
                        "element_range", {"ELTUID": str(eltuid).strip(), "IOBJNM": iobj}
                    ),
                )
            )
        return result

    # --- conditions and exceptions (D26) --------------------------------------------------

    def _conditions(
        self,
        elements: list[QueryElement],
        texts: dict[str, tuple[str | None, str | None]],
    ) -> list[QueryCondition]:
        """Conditions and exceptions, with their operator, threshold and on/off state (D26).

        There is no condition table. All 73 ``RSZ*`` transparent tables were listed on the reference
        system and not one holds conditions; a condition is an element, and the definition is two
        ``RSZRANGE`` rows keyed under the pseudo-InfoObject ``1CONDITION``:

          * ``FACIOBJNM='1STRUC'`` - ``LOW`` is the uid of the key figure being ranked or tested,
            flagged ``LOWFLAG='2'`` (a reference to another element, not a value).
          * ``FACIOBJNM='1VALUE'`` - ``OPT`` is the operator and ``LOW`` the threshold, with
            ``LOWFLAG`` saying whether that threshold is a number or a variable.

        ``RSZSELECT`` supplies the two things the range rows do not: ``ACTIVE``, and ``CONTYPE`` as
        a second declared confirmation of the element's kind.

        The elements are already in the walked tree, so this reads no new tree and adds one query
        per table over a handful of uids. Returning an empty list therefore means "this query has
        none", not "conditions were not looked for".
        """
        wanted = {
            e.eltuid: e.element_type
            for e in elements
            if e.element_type in ("condition", "exception")
        }
        if not wanted:
            return []
        names = {e.eltuid: e.name for e in elements}
        descriptions = {
            uid: (text or (None, None))[1] or (text or (None, None))[0]
            for uid, text in texts.items()
        }
        state = self._condition_state(list(wanted))
        ranges = self._condition_ranges(list(wanted))

        conditions: list[QueryCondition] = []
        for uid in sorted(wanted):
            active, contype, excabsrel = state.get(uid, (False, None, None))
            rows = ranges.get(uid) or _ConditionRanges()
            is_exception = wanted[uid] == "exception"
            provenance = [
                self.provenance("element_dir", {"ELTUID": uid, "OBJVERS": "A"}),
                self.provenance("element_select", {"ELTUID": uid, "OBJVERS": "A"}),
                self.provenance("element_range", {"ELTUID": uid, "OBJVERS": "A"}),
            ]
            # A condition has exactly one threshold and no alert levels; an exception has levels and
            # no single threshold. Measured on every active row of both kinds, so this is the shape
            # of the data rather than a convenience (D41).
            conditions.append(
                QueryCondition(
                    eltuid=uid,
                    kind="exception" if is_exception else "condition",
                    name=names.get(uid),
                    description=descriptions.get(uid),
                    active=active,
                    condition_type=_decode_contype(contype),
                    operator=None if is_exception else _decode_operator(rows.operator),
                    threshold=None if is_exception else rows.threshold,
                    threshold_source=(
                        None if is_exception else _decode_threshold_source(rows.lowflag)
                    ),
                    measure_eltuid=rows.measure,
                    measure_description=descriptions.get(rows.measure or ""),
                    alert_levels=[
                        AlertLevel(
                            level=_decode_alert_level(level),
                            operator=_decode_operator(opt),
                            low=low,
                            high=high,
                            provenance=self.provenance(
                                "element_range", {"ELTUID": uid, "ALERTLEVEL": level}
                            ),
                        )
                        for level, opt, low, high in rows.levels
                    ],
                    drilldown_characteristics=rows.drilldown,
                    evaluation_scope=(
                        _decode_evaluation_scope(excabsrel) if is_exception else None
                    ),
                    provenance=provenance,
                )
            )
        return conditions

    def _condition_state(
        self, eltuids: list[str]
    ) -> dict[str, tuple[bool, str | None, str | None]]:
        """``ELTUID -> (active, CONTYPE, EXCABSREL)`` from ``RSZSELECT``.

        ``ACTIVE`` blank means switched off. It carries real information for both kinds - 89 active
        against 45 inactive across the reference system's condition rows, and 71 against 6 for
        exceptions - and an element with no row at all is reported inactive rather than assumed on.
        That default is the safer one when the row is genuinely absent, and it is exactly what made
        D41 damaging rather than merely incomplete: filtering on the wrong pseudo-InfoObject made
        every exception's row look absent, so all 71 live ones were reported switched off.
        """
        if not eltuids or not self.capability.is_available("element_select"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        dimensions = ", ".join("?" for _ in _CONDITION_DIMENSIONS)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "ACTIVE", "CONTYPE", "EXCABSREL"],
                from_logical="element_select",
                where=[f"ELTUID IN ({placeholders})", f"IOBJNM IN ({dimensions})"],
                params=[*eltuids, *_CONDITION_DIMENSIONS],
            )
        )
        return {
            str(r[0]).strip(): (
                str(r[1]).strip().upper() == "X",
                _clean(r[2]),
                _clean(r[3]),
            )
            for r in rows
        }

    def _condition_ranges(self, eltuids: list[str]) -> dict[str, _ConditionRanges]:
        """Every ``RSZRANGE`` row of a condition or exception, sorted into what each row is for.

        Three row shapes, told apart by ``FACIOBJNM`` rather than by ``ENUM`` ordering - ordering is
        how the rows happen to be stored, ``FACIOBJNM`` is a statement of purpose:

          * ``1STRUC`` - the key figure being ranked or tested. Exactly one per element, on all 204
            conditions and exceptions of the reference system.
          * ``1VALUE`` - a threshold. **One for a condition, one per alert level for an exception**;
            the observed row-count histogram per exception is ``{2: 18, 3: 19, 4: 32, 5: 2, 6: 6}``.
          * anything else - a real characteristic, which on an exception is a drilldown level it is
            evaluated at. These were dropped entirely before D41.
        """
        if not eltuids or not self.capability.is_available("element_range"):
            return {}
        placeholders = ", ".join("?" for _ in eltuids)
        dimensions = ", ".join("?" for _ in _CONDITION_DIMENSIONS)
        rows = self.select(
            self.dialect.build_select(
                columns=["ELTUID", "FACIOBJNM", "OPT", "LOW", "HIGH", "LOWFLAG", "ALERTLEVEL"],
                from_logical="element_range",
                where=[f"ELTUID IN ({placeholders})", f"IOBJNM IN ({dimensions})"],
                params=[*eltuids, *_CONDITION_DIMENSIONS],
                order_by=["ELTUID", "ENUM"],
            )
        )
        out: dict[str, _ConditionRanges] = {}
        for eltuid, faciobjnm, opt, low, high, lowflag, alertlevel in rows:
            uid = str(eltuid).strip()
            entry = out.setdefault(uid, _ConditionRanges())
            facet = str(faciobjnm).strip().upper()
            if facet == _CONDITION_MEASURE_FACET:
                entry.measure = _clean(low)
            elif facet == _CONDITION_VALUE_FACET:
                level = str(alertlevel or "").strip()
                if level and level != _NO_ALERT_LEVEL:
                    entry.levels.append((level, _clean(opt), _clean(low), _clean(high)))
                else:
                    # A condition: one threshold, no level. Proven by all 254 of the reference
                    # system's condition range rows carrying ALERTLEVEL '00'.
                    entry.operator = _clean(opt)
                    entry.threshold = _clean(low)
                    entry.lowflag = _clean(lowflag)
            else:
                name = _clean(faciobjnm)
                if name and name not in entry.drilldown:
                    entry.drilldown.append(name)
        return out

    def _variables(
        self, elements: list[QueryElement], restrictions: dict[str, list[Restriction]]
    ) -> list[QueryVariable]:
        """Every variable the query uses, found by name *and* by element uid (D25).

        Keying only on the element's ``MAPNAME`` silently loses a large share of them. Measured on
        the reference system, **801 of 2,188** active variable elements (36.6%) carry a blank
        ``MAPNAME`` - a variable reached through a condition is one such case, and the subject query
        lost its Top N parameter that way while still counting the element. Every one of those 801
        is nameable through ``RSZGLOBV.VARUNIID``, which holds the element uid and is populated on
        all 2,188 rows, so the join loses nothing and recovers all of them.

        Both routes are used rather than replacing one with the other: a variable referenced from a
        restriction is known by name only, and one referenced from a condition by uid only.
        """
        names: set[str] = {e.name for e in elements if e.element_type == "variable" and e.name}
        self._add_restriction_variables(restrictions, names)
        uids: set[str] = {e.eltuid for e in elements if e.element_type == "variable" and not e.name}
        return self._fetch_variables(names, uids)

    @staticmethod
    def _add_restriction_variables(
        restrictions: dict[str, list[Restriction]], names: set[str]
    ) -> None:
        for restr_list in restrictions.values():
            for r in restr_list:
                if r.low_is_variable and r.low:
                    names.add(r.low)
                if r.high_is_variable and r.high:
                    names.add(r.high)

    def _fetch_variables(
        self, names: set[str], uids: set[str] | None = None
    ) -> list[QueryVariable]:
        uids = uids or set()
        if (not names and not uids) or not self.capability.is_available("global_variable"):
            return []
        ordered = sorted(names)
        ordered_uids = sorted(uids)
        clauses: list[str] = []
        params: list[Any] = []
        if ordered:
            clauses.append(f"VNAM IN ({', '.join('?' for _ in ordered)})")
            params.extend(ordered)
        if ordered_uids:
            clauses.append(f"VARUNIID IN ({', '.join('?' for _ in ordered_uids)})")
            params.extend(ordered_uids)
        rows = self.select(
            self.dialect.build_select(
                columns=["VNAM", "VARTYP", "VPROCTP", "IOBJNM", "VARINPUT"],
                from_logical="global_variable",
                where=[f"({' OR '.join(clauses)})"],
                params=params,
            )
        )
        variables: list[QueryVariable] = []
        seen: set[str] = set()
        for vnam, vartyp, vproctp, iobjnm, varinput in rows:
            name = _clean(vnam)
            if name is None or name in seen:
                continue  # a variable found by both routes must be listed once
            seen.add(name)
            processing = _VPROCTP_TO_TYPE.get(str(vproctp).strip(), "unknown")
            variables.append(
                QueryVariable(
                    name=name,
                    iobjnm=_clean(iobjnm),
                    kind=_VARTYP_TO_KIND.get(str(vartyp).strip(), "unknown"),
                    processing_type=processing,
                    is_customer_exit=processing == "customer_exit",
                    input_ready=str(varinput).strip() == "X",
                    provenance=self.provenance("global_variable", {"VNAM": name, "OBJVERS": "A"}),
                )
            )
        return variables

    def _query_only_filter(self) -> str | None:
        """WHERE fragment restricting RSZCOMPDIR to queries (root element DEFTP='REP')."""
        if not self.capability.is_available("element_dir"):
            return None
        status = self.capability.table("element_dir")
        physical = status.resolved_name if status and status.resolved_name else "RSZELTDIR"
        schema = self.capability.abap_schema
        ref = f"{quote_ident(schema)}.{quote_ident(physical)}" if schema else quote_ident(physical)
        return f"COMPUID IN (SELECT ELTUID FROM {ref} WHERE DEFTP = 'REP' AND OBJVERS = 'A')"

    def _compuids_for_provider(self, provider: str) -> list[str]:
        if not self.capability.is_available("query_provider"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["COMPUID"],
                from_logical="query_provider",
                where=["INFOCUBE = ?"],
                params=[provider],
            )
        )
        return sorted({str(r[0]).strip() for r in rows if str(r[0]).strip()})

    def _providers_for(self, compuids: list[str]) -> dict[str, str]:
        """Master provider per query (first/IS_MASTER)."""
        if not compuids or not self.capability.is_available("query_provider"):
            return {}
        placeholders = ", ".join("?" for _ in compuids)
        rows = self.select(
            self.dialect.build_select(
                columns=["COMPUID", "INFOCUBE", "IS_MASTER"],
                from_logical="query_provider",
                where=[f"COMPUID IN ({placeholders})"],
                params=list(compuids),
            )
        )
        result: dict[str, str] = {}
        for compuid, infocube, is_master in rows:
            cu, provider = str(compuid).strip(), _clean(infocube)
            if provider is None:
                continue
            if cu not in result or str(is_master).strip() == "X":
                result[cu] = provider
        return result

    def _providers_list(self, compuid: str) -> list[str]:
        if not self.capability.is_available("query_provider"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["INFOCUBE", "IS_MASTER"],
                from_logical="query_provider",
                where=["COMPUID = ?"],
                params=[compuid],
            )
        )
        masters = [_clean(r[0]) for r in rows if str(r[1]).strip() == "X" and _clean(r[0])]
        others = [_clean(r[0]) for r in rows if str(r[1]).strip() != "X" and _clean(r[0])]
        ordered: list[str] = []
        for name in [*masters, *others]:
            if name is not None and name not in ordered:
                ordered.append(name)
        return ordered

    def _count(self, base: Any) -> int:
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0
