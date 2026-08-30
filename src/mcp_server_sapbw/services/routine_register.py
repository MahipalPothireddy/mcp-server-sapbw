"""Portfolio-wide routine register: every transformation routine in the system, ranked.

Built from three bulk reads rather than a per-transformation loop, because a mature system holds
thousands of routines and a round trip each would be unusable:

1. **Ownership.** ``RSTRAN``'s five routine columns give the header routines (start/end/expert and
   two global slots); ``RSTRANSTEPROUT`` gives the field routines. Both are read whole, so every
   routine is attributed to its transformation and endpoints.
2. **Size.** One ``GROUP BY CODEID`` over the ABAP source table gives every routine's line count.
   This is what makes the portfolio totals complete without fetching a single line of source.
3. **Source, for the budget only.** The largest ``parse_budget`` routines are fetched in a single
   ``CODEID IN (...)`` read and parsed. Everything else is listed with ``analyzed=False``.

The distinction in step 3 matters: reporting an unparsed routine's pattern counts as zero would read
as "this routine is clean", which is a claim the register has not earned (mission Rule 2).

Orphaned code ids - a ``CODEID`` on a transformation with no rows in the source table - are counted
and reported rather than listed as empty routines.
"""

from __future__ import annotations

from typing import Any

from ..models.completeness import BoundHit, Completeness
from ..models.provenance import UnsupportedResult
from ..models.register import RoutineRegister, RoutineRegisterEntry
from ..models.transformations import RoutineKind
from ..repositories.base import Repository
from .routine_parser import RoutineParser

# RSTRAN header routine columns, in the order they are selected, mapped to their slot.
_HEADER_SLOTS: tuple[tuple[str, RoutineKind], ...] = (
    ("STARTROUTINE", "start"),
    ("ENDROUTINE", "end"),
    ("EXPERT", "expert"),
    ("GLBCODE", "global"),
    ("GLBCODE2", "global"),
)
# RSTRANSTEPROUT.KIND -> routine slot for field-level routines.
_ROUT_KIND: dict[str, RoutineKind] = {"NORMAL": "field", "FORMULA": "formula", "UNIT": "unit"}

# Bulk-scan caps. A transformation contributes at most 5 header routines, so the transformation cap
# bounds the header set; the field-routine and size scans are capped independently.
_TRANSFORM_SCAN_CAP = 20000
_FIELD_ROUTINE_SCAN_CAP = 60000
_SIZE_SCAN_CAP = 80000
# Source rows pulled for the parse budget in one read.
_SOURCE_ROW_CAP = 400000
_MAX_PARSE_BUDGET = 500


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _Owner:
    """Where a routine lives: its transformation, slot, and that transformation's endpoints."""

    __slots__ = ("kind", "source_name", "target_name", "tran_id")

    def __init__(
        self, tran_id: str, kind: RoutineKind, source_name: str | None, target_name: str | None
    ) -> None:
        self.tran_id = tran_id
        self.kind = kind
        self.source_name = source_name
        self.target_name = target_name


class RoutineRegisterService(Repository):
    """Ranks every transformation routine in the system by pattern count, then size."""

    def __init__(self, connection: Any, capability: Any, cache: Any = None) -> None:
        super().__init__(connection, capability, cache)
        self._parser = RoutineParser()

    def build(
        self, *, limit: int = 50, offset: int = 0, parse_budget: int = 100
    ) -> RoutineRegister | UnsupportedResult:
        unsupported = self.require("transformation", "routine_source")
        if unsupported is not None:
            return unsupported

        budget = max(1, min(parse_budget, _MAX_PARSE_BUDGET))
        owners = self._owners()
        sizes = self._sizes()

        # A routine exists for the register when a transformation declares it AND source rows exist.
        known = [code for code in owners if code in sizes]
        orphaned = len(owners) - len(known)
        # Largest first: the parse budget should be spent where there is most logic to find.
        known.sort(key=lambda code: (-sizes[code], code))

        to_parse = known[:budget]
        analyses = self._analyse(to_parse)

        entries = [
            self._entry(code, owners[code], sizes[code], analyses.get(code)) for code in known
        ]
        # Ranking basis: measured pattern count first, then measured size. No invented weighting.
        entries.sort(key=lambda e: (-e.anti_pattern_total, -e.line_count, e.code_id))

        totals: dict[str, int] = {}
        for entry in entries:
            for kind, count in entry.anti_pattern_counts.items():
                totals[kind] = totals.get(kind, 0) + count

        window = entries[offset : offset + limit]
        return RoutineRegister(
            entries=window,
            total_routines=len(known),
            total_lines=sum(sizes[code] for code in known),
            analyzed_count=len(analyses),
            parse_budget=budget,
            anti_pattern_totals=dict(sorted(totals.items())),
            limit=limit,
            offset=offset,
            # Two bounds, and the second one the old bool could not express at all (D6). Paging is
            # benign - the totals stay exact. The parse budget is not: entries beyond it carry
            # `analyzed=false`, so their pattern counts are *unknown* rather than zero, and a caller
            # ranking by anti-patterns is ranking a partially analysed portfolio.
            completeness=Completeness(
                bounds=[
                    *(
                        [BoundHit(bound="page_limit", scope="entries", limit=limit)]
                        if offset + limit < len(entries)
                        else []
                    ),
                    *(
                        [BoundHit(bound="parse_budget", scope="anti_patterns", limit=budget)]
                        if len(analyses) < len(known)
                        else []
                    ),
                ]
            ),
            caveats=self._caveats(len(known), len(analyses), orphaned),
        )

    # --- ownership -------------------------------------------------------------------------

    def _owners(self) -> dict[str, _Owner]:
        """``{code_id: owner}`` for every routine any transformation declares."""
        owners: dict[str, _Owner] = {}
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[
                        "TRANID",
                        "SOURCENAME",
                        "TARGETNAME",
                        *[column for column, _ in _HEADER_SLOTS],
                    ],
                    from_logical="transformation",
                    order_by=["TRANID"],
                ),
                limit=_TRANSFORM_SCAN_CAP,
            )
        )
        endpoints: dict[str, tuple[str | None, str | None]] = {}
        for row in rows:
            tran_id = _clean(row[0])
            if tran_id is None:
                continue
            source, target = _clean(row[1]), _clean(row[2])
            endpoints[tran_id] = (source, target)
            for index, (_column, kind) in enumerate(_HEADER_SLOTS, start=3):
                code = _clean(row[index])
                if code and code not in owners:
                    owners[code] = _Owner(tran_id, kind, source, target)
        owners.update(self._field_routine_owners(endpoints, owners))
        return owners

    def _field_routine_owners(
        self,
        endpoints: dict[str, tuple[str | None, str | None]],
        seen: dict[str, _Owner],
    ) -> dict[str, _Owner]:
        if not self.capability.is_available("transformation_step_rout"):
            return {}
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["TRANID", "CODEID", "KIND"],
                    from_logical="transformation_step_rout",
                    order_by=["TRANID", "CODEID"],
                ),
                limit=_FIELD_ROUTINE_SCAN_CAP,
            )
        )
        found: dict[str, _Owner] = {}
        for tran_raw, code_raw, kind_raw in rows:
            tran_id, code = _clean(tran_raw), _clean(code_raw)
            if tran_id is None or code is None or code in seen or code in found:
                continue
            source, target = endpoints.get(tran_id, (None, None))
            found[code] = _Owner(
                tran_id, _ROUT_KIND.get((_clean(kind_raw) or "").upper(), "field"), source, target
            )
        return found

    # --- size ------------------------------------------------------------------------------

    def _sizes(self) -> dict[str, int]:
        """``{code_id: line_count}`` for every routine that has source. One aggregate read."""
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["CODEID", "COUNT(*)"],
                    from_logical="routine_source",
                    # RSAABAP is not OBJVERS-auto in the dialect; state it explicitly.
                    where=["OBJVERS = 'A'"],
                    group_by=["CODEID"],
                    # Capped scan over the whole ABAP source table. The register is ranked and then
                    # truncated, so an arbitrary slice would rank a different set of routines each
                    # time - and the register exists precisely to be worked down in order (D8).
                    order_by=["CODEID"],
                ),
                limit=_SIZE_SCAN_CAP,
            )
        )
        sizes: dict[str, int] = {}
        for code_raw, count in rows:
            code = _clean(code_raw)
            if code:
                sizes[code] = _as_int(count)
        return sizes

    # --- analysis --------------------------------------------------------------------------

    def _analyse(self, code_ids: list[str]) -> dict[str, Any]:
        """Parse the budgeted routines, fetching all their source in a single read."""
        if not code_ids:
            return {}
        placeholders = ", ".join("?" for _ in code_ids)
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["CODEID", "LINE"],
                    from_logical="routine_source",
                    where=[f"CODEID IN ({placeholders})", "OBJVERS = 'A'"],
                    params=list(code_ids),
                    order_by=["CODEID", "LINE_NO"],
                ),
                limit=_SOURCE_ROW_CAP,
            )
        )
        lines_by_code: dict[str, list[str]] = {}
        for code_raw, line in rows:
            code = _clean(code_raw)
            if code:
                lines_by_code.setdefault(code, []).append("" if line is None else str(line))
        return {
            code: self._parser.analyze(
                code_id=code,
                kind="unknown",
                lines=lines,
                provenance=self.provenance("routine_source", {"CODEID": code, "OBJVERS": "A"}),
            )
            for code, lines in lines_by_code.items()
        }

    def _entry(
        self, code: str, owner: _Owner, line_count: int, analysis: Any
    ) -> RoutineRegisterEntry:
        entry = RoutineRegisterEntry(
            code_id=code,
            kind=owner.kind,
            transformation_id=owner.tran_id,
            source_name=owner.source_name,
            target_name=owner.target_name,
            line_count=line_count,
            analyzed=analysis is not None,
            provenance=self.provenance("routine_source", {"CODEID": code, "OBJVERS": "A"}),
        )
        if analysis is None:
            return entry
        counts: dict[str, int] = {}
        for pattern in analysis.anti_patterns:
            counts[pattern.kind] = counts.get(pattern.kind, 0) + 1
        entry.select_count = analysis.complexity.select_count
        entry.loop_count = analysis.complexity.loop_count
        entry.max_loop_nesting = analysis.complexity.max_loop_nesting
        entry.unresolved_call_count = len(analysis.unresolved_refs)
        entry.anti_pattern_counts = dict(sorted(counts.items()))
        entry.anti_pattern_total = sum(counts.values())
        entry.table_reads = [dep.table for dep in analysis.table_dependencies]
        return entry

    @staticmethod
    def _caveats(total: int, analyzed: int, orphaned: int) -> list[str]:
        caveats = [
            "Line counts and portfolio totals cover every routine. Pattern counts cover only the "
            f"{analyzed} largest routine(s) that were parsed: an entry with analyzed=false has "
            "unknown patterns, not none.",
            "Entries are ranked by measured anti-pattern count then measured line count. A small "
            "routine with a bad pattern outside the parse budget is therefore not ranked by it.",
            "Pattern detection is the same heuristic used per transformation and is a lower bound: "
            "dynamic SQL, function-module and class-method calls are not followed.",
        ]
        if analyzed < total:
            caveats.append(
                f"{total - analyzed} routine(s) were listed but not parsed. Raise parse_budget to "
                "analyse more, at the cost of reading more source"
            )
        if orphaned:
            caveats.append(
                f"{orphaned} routine reference(s) on transformations have no source rows and are "
                "excluded; these are usually leftovers from deleted or never-activated logic"
            )
        return caveats
