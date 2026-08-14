"""Transformations repository (B5).

Reads transformation headers (RSTRAN), field-level rule mappings (RSTRANRULE + RSTRANFIELD), routine
references (RSTRAN header code-ids + RSTRANSTEPROUT), and full ABAP source (RSAABAP). Column usage
and joins were validated live in B5:

- Routine code-ids on RSTRAN (STARTROUTINE/ENDROUTINE/EXPERT/GLBCODE/GLBCODE2) and
  RSTRANSTEPROUT.CODEID all join to RSAABAP.CODEID (OBJVERS='A', ordered by LINE_NO). RSAABAP's
  prefix is not OBJVERS-auto (see dialect), so its queries add OBJVERS='A' explicitly.
- RSTRANFIELD.PARAMTYPE: '1' = target field, '0' = source field (verified against CONSTANT rules).
- RULETYPE / RSTLOGO endpoint codes are decoded below.

``list_transformations`` filters endpoints by pattern (see :func:`..core.dialect.like_term`): a
DataSource endpoint is stored space-padded as ``<DATASOURCE><pad><LOGSYS>``, so an equality filter
on the DataSource name alone can never match.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..core.dialect import like_term
from ..models.provenance import UnsupportedResult
from ..models.transformations import (
    AggregationBehaviour,
    ConstantValue,
    DeclaredLookup,
    EndpointKind,
    FieldMapping,
    LookupKeyDate,
    LookupKind,
    LookupMissBehaviour,
    RoutineAnalysis,
    RoutineCode,
    RoutineKind,
    RoutineRef,
    RuleGroupType,
    RuleType,
    Transformation,
    TransformationEndpoint,
    TransformationSummary,
)
from ..services.routine_parser import RoutineParser
from .base import Repository
from .texts import StoredText, TextsRepository, TextTableSpec

_TEXT_SPEC = TextTableSpec("transformation_text", "TRANID", "classic")

# RSTLOGO endpoint type code -> readable kind.
_RSTLOGO_TO_KIND: dict[str, EndpointKind] = {
    "RSDS": "datasource",
    "TRCS": "infosource",
    "ODSO": "dso",
    "ADSO": "adso",
    "CUBE": "infocube",
    "MPRO": "multiprovider",
    "HCPR": "compositeprovider",
    "IOBJ": "infoobject",
    "ELEM": "query_element",
}

# Rule-depth decodes. Every mapping below was read from the ABAP dictionary (DD03L -> DD07T fixed
# domain values) on the live system rather than assumed, so the labels are SAP's own:
#   RSTRAN_AGGREGATION, RSTRAN_GROUPTYPE, RSTRAN_ODSO, RSMPER.
_AGGR_TO_BEHAVIOUR: dict[str, AggregationBehaviour] = {
    "MOV": "direct_assignment",  # overwrite the target value
    "SUM": "summation",  # add to the target value
    "MIN": "minimum",
    "MAX": "maximum",
    "NOP": "none",
}
_GROUPTYPE_TO_KIND: dict[str, RuleGroupType] = {
    "S": "standard",
    "N": "normal",
    "R": "return_table",
    "T": "technical_fields",
}
# RSTRANSTEPODSO/ADSO.BEHAVIOR: what BW does when the lookup finds no record.
_BEHAVIOR_TO_MISS: dict[str, LookupMissBehaviour] = {"": "error", "C": "constant"}
# RSTRANSTEPMASTER.MPER: which key date a time-dependent master-data read uses.
_MPER_TO_KEY_DATE: dict[str, LookupKeyDate] = {
    "1": "period_start",
    "2": "period_end",
    "3": "current_date",
    "4": "constant_date",
}
# RSTRANSTEPCNST.INTTYPE: ABAP internal type of a constant value.
_INTTYPE_LABEL: dict[str, str] = {
    "C": "character",
    "N": "numeric text",
    "D": "date",
    "T": "time",
    "P": "packed decimal",
    "I": "integer",
    "F": "floating point",
    "X": "byte",
}

# RSTRANRULE.RULETYPE code -> RuleType (codes are upper-case words on the wire).
_RULETYPE_TO_RULE: dict[str, RuleType] = {
    "DIRECT": "direct",
    "CONSTANT": "constant",
    "ROUTINE": "routine",
    "FORMULA": "formula",
    "MASTER": "master",
    "TIME": "time",
    "UNIT": "unit",
    "START": "start",
    "END": "end",
    "EXPERT": "expert",
    "ADSO": "adso",
    "ODSO": "odso",
    "HIER_SPLIT": "hier_split",
}

# RSTRANSTEPROUT.KIND -> RoutineKind (field-level routines).
_ROUT_KIND_TO_KIND: dict[str, RoutineKind] = {
    "NORMAL": "field",
    "FORMULA": "formula",
    "UNIT": "unit",
}

# RSTRAN header routine columns -> RoutineKind.
_HEADER_ROUTINES: tuple[tuple[str, RoutineKind], ...] = (
    ("STARTROUTINE", "start"),
    ("ENDROUTINE", "end"),
    ("EXPERT", "expert"),
    ("GLBCODE", "global"),
    ("GLBCODE2", "global"),
)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _endpoint(type_code: Any, subtype: Any, name: Any) -> TransformationEndpoint | None:
    code = _clean(type_code)
    obj_name = _clean(name)
    if code is None or obj_name is None:
        return None
    return TransformationEndpoint(
        name=obj_name,
        kind=_RSTLOGO_TO_KIND.get(code, "other"),
        type_code=code,
        subtype=_clean(subtype),
    )


class TransformationsRepository(Repository):
    """Transformation structure, field mappings, routine source, and routine analysis."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._texts = TextsRepository(connection, capability, cache)
        self._parser = RoutineParser()
        # Per-instance memo for analyze_routines. Analysing a routine means fetching its ABAP
        # source from RSAABAP (2.3M rows on the reference system) and parsing every line, and the
        # lineage walk asks for the same transformation repeatedly: once per node it appears
        # against, and again on every page whose graph overlaps. Measured at ~12s per node without
        # this. Safe because the repository reads metadata only, so a transformation's analysis
        # cannot change under a live instance; the memo dies with the instance, so a later tool
        # call re-reads.
        self._analysis_memo: dict[str, list[RoutineAnalysis] | UnsupportedResult] = {}

    # --- listing -------------------------------------------------------------------------

    def list_transformations(
        self,
        *,
        source_name: str | None = None,
        target_name: str | None = None,
        with_routines_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TransformationSummary], int] | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported

        where: list[str] = []
        params: list[Any] = []
        # Endpoint names are pattern-matched, not compared: DataSource endpoints are stored
        # space-padded ('<DATASOURCE><pad><LOGSYS>'), so equality can never match one.
        for column, value in (("SOURCENAME", source_name), ("TARGETNAME", target_name)):
            if not value:
                continue
            like = like_term(value)
            where.append(like.clause(column))
            params.append(like.value)
        if with_routines_only:
            where.append("(STARTROUTINE <> '' OR ENDROUTINE <> '' OR EXPERT <> '')")

        base = self.dialect.build_select(
            columns=[
                "TRANID",
                "SOURCETYPE",
                "SOURCENAME",
                "TARGETTYPE",
                "TARGETNAME",
                "STARTROUTINE",
                "ENDROUTINE",
                "EXPERT",
            ],
            from_logical="transformation",
            where=where,
            params=params,
            order_by=["TRANID"],
        )
        total = self._count(base)
        rows = self.select(self.dialect.paginate(base, limit=limit, offset=offset))
        tran_ids = [str(r[0]) for r in rows]
        texts = self._texts_for(tran_ids)

        summaries: list[TransformationSummary] = []
        for row in rows:
            tran_id = str(row[0])
            src = _endpoint(row[1], None, row[2])
            tgt = _endpoint(row[3], None, row[4])
            has_routines = any(_clean(row[i]) for i in (5, 6, 7))
            summaries.append(
                TransformationSummary(
                    tran_id=tran_id,
                    description=texts.get(tran_id),
                    source_name=src.name if src else None,
                    source_kind=src.kind if src else None,
                    target_name=tgt.name if tgt else None,
                    target_kind=tgt.kind if tgt else None,
                    has_routines=has_routines,
                    provenance=self.provenance(
                        "transformation", {"TRANID": tran_id, "OBJVERS": "A"}
                    ),
                )
            )
        return summaries, total

    # --- structure -----------------------------------------------------------------------

    def get_transformation(self, tran_id: str) -> Transformation | UnsupportedResult:
        """Header, field mappings and routine references. Cached (scope ``transformation``)."""
        return self.cached_model(
            "transformation",
            tran_id,
            model=Transformation,
            build=lambda: self._get_transformation_uncached(tran_id),
            # No header row yields an empty shell; it may be transported later, so never cache it.
            cache_when=lambda tran: tran.source is not None or tran.target is not None,
        )

    def _get_transformation_uncached(self, tran_id: str) -> Transformation | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        header = self.select(
            self.dialect.build_select(
                columns=[
                    "OBJSTAT",
                    "SOURCETYPE",
                    "SOURCESUBTYPE",
                    "SOURCENAME",
                    "TARGETTYPE",
                    "TARGETSUBTYPE",
                    "TARGETNAME",
                    "STARTROUTINE",
                    "ENDROUTINE",
                    "EXPERT",
                    "GLBCODE",
                    "GLBCODE2",
                ],
                from_logical="transformation",
                where=["TRANID = ?"],
                params=[tran_id],
            )
        )
        if not header:
            return Transformation(
                tran_id=tran_id,
                active=False,
                provenance=self.provenance("transformation", {"TRANID": tran_id, "OBJVERS": "A"}),
            )
        row = header[0]
        refs = self._routine_refs(tran_id, row)
        return Transformation(
            tran_id=tran_id,
            description=self._texts_for([tran_id]).get(tran_id),
            active=str(row[0]).strip() == "ACT",
            source=_endpoint(row[1], row[2], row[3]),
            target=_endpoint(row[4], row[5], row[6]),
            field_mappings=self._field_mappings(tran_id),
            routines=refs,
            declared_lookups=self._declared_lookups_or_note(tran_id),
            has_start_routine=bool(_clean(row[7])),
            has_end_routine=bool(_clean(row[8])),
            has_expert_routine=bool(_clean(row[9])),
            caveats=self._structure_caveats(),
            provenance=self.provenance("transformation", {"TRANID": tran_id, "OBJVERS": "A"}),
        )

    def _declared_lookups_or_note(self, tran_id: str) -> list[DeclaredLookup]:
        return self.declared_lookups(tran_id)

    def _structure_caveats(self) -> list[str]:
        """Say which rule-depth sources were unavailable, so an empty list is not read as 'none'."""
        missing = [
            physical
            for logical, physical in (
                ("transformation_step_const", "RSTRANSTEPCNST"),
                ("transformation_step_master", "RSTRANSTEPMASTER"),
                ("transformation_step_dso", "RSTRANSTEPODSO"),
                ("transformation_step_adso", "RSTRANSTEPADSO"),
            )
            if not self.capability.is_available(logical)
        ]
        if not missing:
            return []
        return [
            f"not available on this release: {', '.join(missing)}; declared lookups and/or "
            "constant values may be incomplete"
        ]

    def _field_mappings(self, tran_id: str) -> list[FieldMapping]:
        if not self.capability.is_available("transformation_rule"):
            return []
        rules = self.select(
            self.dialect.build_select(
                columns=["RULEID", "RULETYPE", "AGGR", "GROUPTYPE", "NO_CONV"],
                from_logical="transformation_rule",
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID"],
            )
        )
        fields_by_rule = self._fields_by_rule(tran_id)
        routine_by_rule = self._routine_code_by_rule(tran_id)
        constants = self._constants_by_rule(tran_id)
        mappings: list[FieldMapping] = []
        for rule_id_raw, ruletype, aggr, grouptype, no_conv in rules:
            rule_id = _as_int(rule_id_raw)
            if rule_id is None:
                continue
            targets, sources, keys = fields_by_rule.get(rule_id, ([], [], []))
            aggr_code = _clean(aggr)
            mappings.append(
                FieldMapping(
                    rule_id=rule_id,
                    rule_type=_RULETYPE_TO_RULE.get(str(ruletype).strip(), "unknown"),
                    target_fields=targets,
                    source_fields=sources,
                    key_fields=keys,
                    routine_code_id=routine_by_rule.get(rule_id),
                    aggregation=_AGGR_TO_BEHAVIOUR.get(aggr_code or ""),
                    aggregation_code=aggr_code,
                    group_type=_GROUPTYPE_TO_KIND.get(_clean(grouptype) or ""),
                    no_conversion=str(no_conv).strip().upper() == "X",
                    constant=constants.get(rule_id),
                    provenance=self.provenance(
                        "transformation_rule", {"TRANID": tran_id, "RULEID": str(rule_id)}
                    ),
                )
            )
        return mappings

    def _constants_by_rule(self, tran_id: str) -> dict[int, ConstantValue]:
        """The literal each CONSTANT rule writes (RSTRANSTEPCNST)."""
        if not self.capability.is_available("transformation_step_const"):
            return {}
        rows = self.select(
            self.dialect.build_select(
                columns=["RULEID", "VALUE", "INTTYPE", "LENGTH", "DECIMALS"],
                from_logical="transformation_step_const",
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID", "STEPID"],
            )
        )
        result: dict[int, ConstantValue] = {}
        for rule_id_raw, value, inttype, length, decimals in rows:
            rule_id = _as_int(rule_id_raw)
            if rule_id is None or rule_id in result:
                continue
            code = _clean(inttype)
            result[rule_id] = ConstantValue(
                value="" if value is None else str(value).strip(),
                internal_type=code,
                internal_type_label=_INTTYPE_LABEL.get(code or ""),
                length=_as_int(length),
                decimals=_as_int(decimals),
            )
        return result

    def declared_lookups(self, tran_id: str) -> list[DeclaredLookup]:
        """Lookups the transformation declares, from the typed rule-step tables.

        Exact by construction — BW records these itself — unlike the routine parser's inferred
        reads. Master-data lookups additionally carry the key-date rule they read at.
        """
        lookups: list[DeclaredLookup] = []
        lookups.extend(self._master_lookups(tran_id))
        lookups.extend(self._store_lookups(tran_id, "transformation_step_dso", "ODSOBJECT", "dso"))
        lookups.extend(self._store_lookups(tran_id, "transformation_step_adso", "ADSONM", "adso"))
        lookups.sort(key=lambda item: (item.kind, item.object_name))
        return lookups

    def _master_lookups(self, tran_id: str) -> list[DeclaredLookup]:
        if not self.capability.is_available("transformation_step_master"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["RULEID", "STEPID", "IOBJNM", "MPER", "DATEIOBJNM", "CONSTANT"],
                from_logical="transformation_step_master",
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID", "STEPID"],
            )
        )
        out: list[DeclaredLookup] = []
        for rule_id, step_id, iobjnm, mper, date_iobj, constant in rows:
            name = _clean(iobjnm)
            if name is None:
                continue
            out.append(
                DeclaredLookup(
                    kind="master_data",
                    object_name=name,
                    rule_id=_as_int(rule_id),
                    step_id=_as_int(step_id),
                    key_date=_MPER_TO_KEY_DATE.get(_clean(mper) or ""),
                    key_date_field=_clean(date_iobj),
                    miss_constant=_clean(constant),
                    provenance=self.provenance(
                        "transformation_step_master", {"TRANID": tran_id, "IOBJNM": name}
                    ),
                )
            )
        return out

    def _store_lookups(
        self, tran_id: str, logical: str, name_column: str, kind: LookupKind
    ) -> list[DeclaredLookup]:
        if not self.capability.is_available(logical):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["RULEID", "STEPID", name_column, "BEHAVIOR", "CONSTANT"],
                from_logical=logical,
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID", "STEPID"],
            )
        )
        out: list[DeclaredLookup] = []
        for rule_id, step_id, obj, behavior, constant in rows:
            name = _clean(obj)
            if name is None:
                continue
            out.append(
                DeclaredLookup(
                    kind=kind,
                    object_name=name,
                    rule_id=_as_int(rule_id),
                    step_id=_as_int(step_id),
                    miss_behaviour=_BEHAVIOR_TO_MISS.get((_clean(behavior) or "").upper(), "error"),
                    miss_constant=_clean(constant),
                    provenance=self.provenance(logical, {"TRANID": tran_id, name_column: name}),
                )
            )
        return out

    def _fields_by_rule(self, tran_id: str) -> dict[int, tuple[list[str], list[str], list[str]]]:
        """Per rule: ``(target_fields, source_fields, key_fields)``.

        ``KEYFLAG`` marks a target field as part of the target's semantic key, which is what decides
        whether a second record overwrites the first or lands beside it.
        """
        if not self.capability.is_available("transformation_field"):
            return {}
        rows = self.select(
            self.dialect.build_select(
                columns=["RULEID", "PARAMTYPE", "FIELDNM", "KEYFLAG"],
                from_logical="transformation_field",
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID", "RULEPOSIT"],
            )
        )
        result: dict[int, tuple[list[str], list[str], list[str]]] = defaultdict(
            lambda: ([], [], [])
        )
        for rule_id_raw, paramtype, fieldnm, keyflag in rows:
            rule_id = _as_int(rule_id_raw)
            field = _clean(fieldnm)
            if rule_id is None or field is None:
                continue
            targets, sources, keys = result[rule_id]
            is_target = _as_int(paramtype) == 1
            bucket = targets if is_target else sources
            if field not in bucket:
                bucket.append(field)
            if is_target and str(keyflag).strip().upper() == "X" and field not in keys:
                keys.append(field)
        return result

    def _routine_code_by_rule(self, tran_id: str) -> dict[int, str]:
        if not self.capability.is_available("transformation_step_rout"):
            return {}
        rows = self.select(
            self.dialect.build_select(
                columns=["RULEID", "CODEID"],
                from_logical="transformation_step_rout",
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID", "STEPID"],
            )
        )
        result: dict[int, str] = {}
        for rule_id_raw, codeid in rows:
            rule_id = _as_int(rule_id_raw)
            code = _clean(codeid)
            if rule_id is not None and code is not None:
                result.setdefault(rule_id, code)
        return result

    # --- routines ------------------------------------------------------------------------

    def _routine_refs(self, tran_id: str, header_row: tuple[Any, ...]) -> list[RoutineRef]:
        """Header routines (start/end/expert/global) + field routines (RSTRANSTEPROUT)."""
        refs: list[RoutineRef] = []
        # header slots start at index 7 (STARTROUTINE); map by column name -> value order.
        header_values = {
            "STARTROUTINE": header_row[7],
            "ENDROUTINE": header_row[8],
            "EXPERT": header_row[9],
            "GLBCODE": header_row[10],
            "GLBCODE2": header_row[11],
        }
        seen_codes: set[str] = set()
        for column, kind in _HEADER_ROUTINES:
            code = _clean(header_values[column])
            if code and code not in seen_codes:
                seen_codes.add(code)
                refs.append(
                    RoutineRef(
                        kind=kind,
                        code_id=code,
                        provenance=self.provenance(
                            "transformation", {"TRANID": tran_id, "OBJVERS": "A"}
                        ),
                    )
                )
        refs.extend(self._field_routine_refs(tran_id, seen_codes))
        return refs

    def _field_routine_refs(self, tran_id: str, seen_codes: set[str]) -> list[RoutineRef]:
        if not self.capability.is_available("transformation_step_rout"):
            return []
        rows = self.select(
            self.dialect.build_select(
                columns=["RULEID", "CODEID", "KIND"],
                from_logical="transformation_step_rout",
                where=["TRANID = ?"],
                params=[tran_id],
                order_by=["RULEID", "STEPID"],
            )
        )
        refs: list[RoutineRef] = []
        for rule_id_raw, codeid, kind_raw in rows:
            code = _clean(codeid)
            if code is None or code in seen_codes:
                continue
            seen_codes.add(code)
            refs.append(
                RoutineRef(
                    kind=_ROUT_KIND_TO_KIND.get(str(kind_raw).strip(), "field"),
                    code_id=code,
                    rule_id=_as_int(rule_id_raw),
                    provenance=self.provenance(
                        "transformation_step_rout", {"TRANID": tran_id, "CODEID": code}
                    ),
                )
            )
        return refs

    def get_routine_code(self, tran_id: str) -> list[RoutineCode] | UnsupportedResult:
        """Full ABAP for every routine of a transformation. Cached (scope ``routine_code``).

        RSAABAP is the largest table the server reads (millions of source lines on a real system)
        and routine source only changes when a transformation is re-activated, so this is the
        highest-value entry in the cache.
        """
        return self.cached_model_list(
            "routine_code",
            tran_id,
            model=RoutineCode,
            build=lambda: self._get_routine_code_uncached(tran_id),
        )

    def _get_routine_code_uncached(self, tran_id: str) -> list[RoutineCode] | UnsupportedResult:
        unsupported = self.require("transformation", "routine_source")
        if unsupported is not None:
            return unsupported
        header = self.select(
            self.dialect.build_select(
                columns=[
                    "OBJSTAT",
                    "SOURCETYPE",
                    "SOURCESUBTYPE",
                    "SOURCENAME",
                    "TARGETTYPE",
                    "TARGETSUBTYPE",
                    "TARGETNAME",
                    "STARTROUTINE",
                    "ENDROUTINE",
                    "EXPERT",
                    "GLBCODE",
                    "GLBCODE2",
                ],
                from_logical="transformation",
                where=["TRANID = ?"],
                params=[tran_id],
            )
        )
        if not header:
            return []
        refs = self._routine_refs(tran_id, header[0])
        return [self._routine_code(ref) for ref in refs]

    def _routine_code(self, ref: RoutineRef) -> RoutineCode:
        lines = self._source_lines(ref.code_id)
        return RoutineCode(
            kind=ref.kind,
            code_id=ref.code_id,
            rule_id=ref.rule_id,
            line_count=len(lines),
            lines=lines,
            provenance=self.provenance("routine_source", {"CODEID": ref.code_id, "OBJVERS": "A"}),
        )

    def _source_lines(self, code_id: str) -> list[str]:
        # RSAABAP prefix is not OBJVERS-auto (see dialect); add OBJVERS='A' explicitly.
        rows = self.select(
            self.dialect.build_select(
                columns=["LINE"],
                from_logical="routine_source",
                where=["CODEID = ?", "OBJVERS = 'A'"],
                params=[code_id],
                order_by=["LINE_NO"],
            )
        )
        return [("" if r[0] is None else str(r[0])) for r in rows]

    def analyze_routines(self, tran_id: str) -> list[RoutineAnalysis] | UnsupportedResult:
        """Parsed dependencies / anti-patterns per routine. Cached (scope ``routine_analysis``).

        Two levels: an in-process memo for repeat hits inside one call (the analyzers and the
        routine register re-ask for the same transformation), and the SQLite cache so a later call
        does not re-read RSAABAP and re-parse the source.
        """
        memoised = self._analysis_memo.get(tran_id)
        if memoised is not None:
            return memoised
        analyses = self.cached_model_list(
            "routine_analysis",
            tran_id,
            model=RoutineAnalysis,
            build=lambda: self._analyze_routines_uncached(tran_id),
        )
        self._analysis_memo[tran_id] = analyses
        return analyses

    def _analyze_routines_uncached(self, tran_id: str) -> list[RoutineAnalysis] | UnsupportedResult:
        codes = self.get_routine_code(tran_id)
        if isinstance(codes, UnsupportedResult):
            return codes
        return [
            self._parser.analyze(
                code_id=code.code_id,
                kind=code.kind,
                lines=code.lines,
                provenance=self.provenance(
                    "routine_source", {"CODEID": code.code_id, "OBJVERS": "A"}
                ),
            )
            for code in codes
        ]

    # --- helpers -------------------------------------------------------------------------

    def _count(self, base: Any) -> int:
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _texts_for(self, tran_ids: list[str], *, langu: str = "E") -> dict[str, str]:
        result: dict[str, str] = {}
        for tran_id in tran_ids:
            stored: StoredText | None = self._texts.object_text(
                _TEXT_SPEC, tran_id, preferred_language=langu
            )
            if stored and stored.short:
                result[tran_id] = stored.short
            elif stored and stored.long:
                result[tran_id] = stored.long
        return result
