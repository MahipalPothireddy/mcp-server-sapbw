"""Heuristic ABAP routine parser (B5).

Static, regex-based analysis of transformation routine source. It extracts the tables a routine
reads (resolving ``/BIC/`` and ``/BI0/`` generated tables back to a BW object heuristically),
flags common anti-patterns, names calls it cannot follow, and reports coarse complexity signals.

It is deliberately a **lower bound** (mission Known Limitation 3): dynamic SQL, function-module and
class-method calls, and generated table names it cannot map are not resolved. Every result is
labelled ``completeness='lower_bound'`` and carries caveats. This service reads no database — it is
given the source lines and a provenance citation by the repository.
"""

from __future__ import annotations

import re

from ..models.provenance import Provenance
from ..models.transformations import (
    AntiPattern,
    ComplexitySignals,
    RoutineAnalysis,
    RoutineKind,
    TableDependency,
    UnresolvedRef,
)

# Table after FROM/JOIN (captures /BIC/..., /BI0/..., or a plain table name).
_FROM_RE = re.compile(r"\b(?:from|join)\s+([a-z0-9_/]+)", re.IGNORECASE)
_SELECT_RE = re.compile(r"\bselect\b", re.IGNORECASE)
_LOOP_OPEN_RE = re.compile(r"\b(loop\s+at|do|while)\b", re.IGNORECASE)
_LOOP_CLOSE_RE = re.compile(r"\b(endloop|enddo|endwhile)\b", re.IGNORECASE)
_FAE_RE = re.compile(r"\bfor\s+all\s+entries\s+in\s+@?\(?\s*([a-z0-9_<>]+)", re.IGNORECASE)
_GUARD_RE = re.compile(
    r"([a-z0-9_<>]+)\s+is\s+(not\s+)?initial|lines\(\s*([a-z0-9_<>]+)", re.IGNORECASE
)
_CALL_FUNC_RE = re.compile(r"\bcall\s+function\s+'([^']+)'", re.IGNORECASE)
_CALL_FUNC_DYN_RE = re.compile(r"\bcall\s+function\s+([a-z0-9_<>]+)", re.IGNORECASE)
_CALL_METHOD_RE = re.compile(
    r"\b(?:call\s+method\s+)?([a-z0-9_/=]+(?:=>|->)[a-z0-9_]+)\s*\(", re.IGNORECASE
)
_PERFORM_RE = re.compile(r"\bperform\s+([a-z0-9_]+)", re.IGNORECASE)
_WHERE_LITERAL_RE = re.compile(r"'[^']*'|=\s*\d", re.IGNORECASE)
_DB_WRITE_RE = re.compile(
    r"\b(insert|update|modify|delete)\s+(?:from\s+)?(/(?:bic|bi0)/[a-z0-9_/]+)", re.IGNORECASE
)
_DELETE_ITAB_RE = re.compile(
    r"\bdelete\s+(?:adjacent\s+duplicates|[a-z0-9_<>]+\s+(?:where|index|from))", re.IGNORECASE
)

_MAX_NESTING_FLAG = 2  # loop nesting at/above this is flagged as nested_loop


def _strip_comments(lines: list[str]) -> list[tuple[int, str]]:
    """Return (line_no, code) pairs with ABAP comments stripped (full-line ``*`` and inline)."""
    cleaned: list[tuple[int, str]] = []
    for line_no, raw in enumerate(lines, 1):
        if raw.lstrip().startswith("*"):
            continue  # full-line comment
        quote = raw.find('"')  # ABAP inline comment (strings use single quotes)
        code = raw[:quote] if quote != -1 else raw
        cleaned.append((line_no, code))
    return cleaned


def _resolve_bw_table(table: str) -> tuple[str | None, str | None]:
    """Best-effort map a /BIC/ or /BI0/ generated table to a BW object (advisory)."""
    upper = table.upper()
    for prefix in ("/BIC/", "/BI0/"):
        if upper.startswith(prefix):
            body = upper[len(prefix) :]
            if not body:
                return None, None
            table_class, rest = body[0], body[1:]
            name = rest.rstrip("0123456789") or rest
            if table_class == "A":  # active DSO table (/BIC/A<dso>00)
                return name, "dso"
            if table_class in ("P", "Q", "X", "Y", "S", "T", "M", "H", "K"):  # master-data/SID/text
                return name or body, "infoobject"
            return name or body, None
    return None, None


class RoutineParser:
    """Static heuristic analysis of a single routine's ABAP source."""

    def analyze(
        self,
        *,
        code_id: str,
        kind: RoutineKind,
        lines: list[str],
        provenance: Provenance,
    ) -> RoutineAnalysis:
        cleaned = _strip_comments(lines)
        code_lines = [text for _, text in cleaned]
        joined = " ".join(code_lines)

        table_deps = self._table_dependencies(joined)
        anti, complexity = self._scan_lines(cleaned)
        unresolved = self._unresolved_calls(cleaned)
        complexity.line_count = len(lines)
        complexity.call_count = len(unresolved)

        caveats = [
            "static heuristic parse; dynamic SQL, function-module and class-method calls are not "
            "followed, so table dependencies are a lower bound",
            "/BIC/ and /BI0/ table-to-object resolution is advisory (naming-convention based)",
        ]
        return RoutineAnalysis(
            code_id=code_id,
            kind=kind,
            table_dependencies=table_deps,
            anti_patterns=anti,
            unresolved_refs=unresolved,
            complexity=complexity,
            caveats=caveats,
            provenance=provenance,
        )

    def _table_dependencies(self, joined: str) -> list[TableDependency]:
        seen: dict[str, TableDependency] = {}
        for statement in joined.split("."):
            if not _SELECT_RE.search(statement):
                continue
            for match in _FROM_RE.finditer(statement):
                table = match.group(1).strip()
                if not table or table in seen or table.lower() in ("table",):
                    continue
                is_bw = table.startswith(("/BIC/", "/BI0/", "/bic/", "/bi0/"))
                resolved, kind = _resolve_bw_table(table) if is_bw else (None, None)
                seen[table] = TableDependency(
                    table=table,
                    access="read",
                    is_bw_generated=is_bw,
                    resolved_object=resolved,
                    resolved_kind=kind,
                )
        return list(seen.values())

    def _scan_lines(
        self, cleaned: list[tuple[int, str]]
    ) -> tuple[list[AntiPattern], ComplexitySignals]:
        anti: list[AntiPattern] = []
        complexity = ComplexitySignals()
        loop_depth = 0
        for line_no, code in cleaned:
            lower = code.lower()
            if _LOOP_OPEN_RE.search(lower):
                loop_depth += 1
                complexity.loop_count += 1
                complexity.max_loop_nesting = max(complexity.max_loop_nesting, loop_depth)
                if loop_depth >= _MAX_NESTING_FLAG:
                    anti.append(AntiPattern(kind="nested_loop", line_no=line_no))
            if _LOOP_CLOSE_RE.search(lower):
                loop_depth = max(0, loop_depth - 1)
            if _SELECT_RE.search(lower):
                complexity.select_count += 1
                if loop_depth > 0:
                    anti.append(AntiPattern(kind="select_in_loop", line_no=line_no))
            self._flag_writes_and_deletes(lower, line_no, anti)
            if "where" in lower and _WHERE_LITERAL_RE.search(lower):
                anti.append(AntiPattern(kind="hardcoded_value", line_no=line_no))
        anti.extend(self._missing_for_all_entries(cleaned))
        return anti, complexity

    @staticmethod
    def _flag_writes_and_deletes(lower: str, line_no: int, anti: list[AntiPattern]) -> None:
        if _DB_WRITE_RE.search(lower):
            anti.append(AntiPattern(kind="database_modification", line_no=line_no))
        elif _DELETE_ITAB_RE.search(lower):
            anti.append(AntiPattern(kind="recordset_delete", line_no=line_no))

    @staticmethod
    def _missing_for_all_entries(cleaned: list[tuple[int, str]]) -> list[AntiPattern]:
        guarded: set[str] = set()
        for _, code in cleaned:
            for match in _GUARD_RE.finditer(code):
                name = match.group(1) or match.group(3)
                if name:
                    guarded.add(name.lower())
        flagged: list[AntiPattern] = []
        for line_no, code in cleaned:
            fae = _FAE_RE.search(code)
            if fae and fae.group(1).lower() not in guarded:
                flagged.append(
                    AntiPattern(
                        kind="missing_for_all_entries",
                        line_no=line_no,
                        detail="FOR ALL ENTRIES without an is-not-initial guard (empty reads all)",
                    )
                )
        return flagged

    @staticmethod
    def _unresolved_calls(cleaned: list[tuple[int, str]]) -> list[UnresolvedRef]:
        refs: list[UnresolvedRef] = []
        for line_no, code in cleaned:
            func = _CALL_FUNC_RE.search(code)
            if func:
                refs.append(
                    UnresolvedRef(
                        call_kind="function_module", object_name=func.group(1), line_no=line_no
                    )
                )
            elif _CALL_FUNC_DYN_RE.search(code):
                refs.append(
                    UnresolvedRef(call_kind="dynamic", object_name="(dynamic)", line_no=line_no)
                )
            method = _CALL_METHOD_RE.search(code)
            if method:
                refs.append(
                    UnresolvedRef(
                        call_kind="class_method", object_name=method.group(1), line_no=line_no
                    )
                )
            perform = _PERFORM_RE.search(code)
            if perform:
                refs.append(
                    UnresolvedRef(call_kind="form", object_name=perform.group(1), line_no=line_no)
                )
        return refs
