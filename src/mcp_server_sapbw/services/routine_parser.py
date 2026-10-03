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
from collections.abc import Iterable, Mapping

from ..models.provenance import Provenance
from ..models.transformations import (
    AntiPattern,
    ComplexitySignals,
    RoutineAnalysis,
    RoutineKind,
    TableDependency,
    UnresolvedRef,
)
from .table_resolver import resolve_table

# Table after FROM/JOIN (captures /BIC/..., /BI0/..., or a plain table name).
_FROM_RE = re.compile(r"\b(?:from|join)\s+([a-z0-9_/]+)", re.IGNORECASE)
_SELECT_RE = re.compile(r"\bselect\b", re.IGNORECASE)
_LOOP_OPEN_RE = re.compile(r"\b(loop\s+at|do|while)\b", re.IGNORECASE)
_LOOP_CLOSE_RE = re.compile(r"\b(endloop|enddo|endwhile)\b", re.IGNORECASE)
_FAE_RE = re.compile(r"\bfor\s+all\s+entries\s+in\s+@?\(?\s*([a-z0-9_/<>]+)", re.IGNORECASE)
_GUARD_RE = re.compile(
    # `(?:\[\])?` matters: `IF lt_keys[] IS NOT INITIAL` is the older and still common spelling, and
    # without it the guard is missed and a guarded read is reported as unguarded.
    r"([a-z0-9_<>]+)(?:\[\])?\s+is\s+(not\s+)?initial"
    r"|lines\(\s*([a-z0-9_<>]+)"
    r"|describe\s+table\s+([a-z0-9_<>]+)",
    re.IGNORECASE,
)
# Driver tables the framework itself fills before calling the routine, so a FOR ALL ENTRIES over one
# of them cannot degenerate into a full-table read and needs no guard.
#
# This is not a style allowance. Flagging them produced 20 of 74 findings on a real extractor-exit
# set - over a quarter of the report - each one describing a risk that cannot occur, which crowds
# out the ones that can. C_T_DATA and I_T_DATA are the RSAP0001 exit's own data parameters;
# SOURCE_PACKAGE and RESULT_PACKAGE are the transformation equivalents.
_FRAMEWORK_FILLED = frozenset(
    {
        "c_t_data",
        "i_t_data",
        "i_data",
        "source_package",
        "result_package",
        "<source_fields>",
        "<result_fields>",
    }
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


_FORM_START_RE = re.compile(r"^\s*form\s+([a-z0-9_]+)", re.IGNORECASE)
_ENDFORM_RE = re.compile(r"^\s*endform\b", re.IGNORECASE)
_IN_PROGRAM_RE = re.compile(r"\bin\s+program\b", re.IGNORECASE)


def form_bodies(lines: list[str]) -> dict[str, list[str]]:
    """``{form_name_lower: body_lines}`` for every ``FORM … ENDFORM`` defined in ``lines``.

    Needed because BW splits a *migrated update rule* in two: the field routine's own source block
    is a short wrapper that performs a subroutine, and the subroutine itself is a ``FORM`` in the
    transformation's global block. Analysing the wrapper alone describes none of the logic (D39).

    The body excludes the ``FORM``/``ENDFORM`` lines themselves, since those are the signature
    rather than the work. A ``FORM`` with no matching ``ENDFORM`` is skipped rather than run to the
    end of the block: guessing an end would attribute unrelated statements to it.
    """
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    collected: list[str] = []
    for raw in lines:
        if current is None:
            match = _FORM_START_RE.match(raw)
            if match:
                current = match.group(1).lower()
                collected = []
            continue
        if _ENDFORM_RE.match(raw):
            bodies.setdefault(current, collected)
            current = None
            continue
        collected.append(raw)
    return bodies


def performed_forms(lines: list[str]) -> list[str]:
    """Lower-cased names of local subroutines ``lines`` performs, in order of first appearance.

    ``PERFORM x IN PROGRAM (y)`` is deliberately excluded. That form resolves the program at runtime
    and is the pattern the mission's Known Limitation 3 is about; treating it as a local subroutine
    would claim a resolution that was never made. Statement-level rather than line-level, because
    the ``IN PROGRAM`` clause is routinely on a later line than the ``PERFORM``.
    """
    found: list[str] = []
    joined = " ".join(text for _, text in _strip_comments(lines))
    for statement in joined.split("."):
        if _IN_PROGRAM_RE.search(statement):
            continue
        for match in _PERFORM_RE.finditer(statement):
            name = match.group(1).lower()
            if name not in found:
                found.append(name)
    return found


def _resolve_bw_table(
    table: str, catalog: Mapping[str, Iterable[str]] | None = None
) -> tuple[str | None, str | None, str | None]:
    """Map a generated table to its BW object via the shared resolver.

    Returns ``(object_name, kind, confidence)``. ``confidence`` is ``'confirmed'`` only when the
    reading was checked against a catalogue of real object names; otherwise ``'advisory'``.

    This used to be a second, cruder implementation living here, and it was wrong in two ways that
    a real system exposed. It stripped *every* trailing digit, so a family of ADSO active tables
    named ``<ns>A<NAME>08`` + role suffix ``2``, ``<NAME>07`` + ``2`` and so on all collapsed onto
    the same truncated stem - a name that exists in no catalogue - and it typed every ``A`` table as
    a classic DSO, so an ADSO was never identified as one. Both are fixed by delegating to the
    single resolver that knows the ADSO ``1``/``2``/``3`` and DSO ``00``/``40`` suffixes apart,
    handles customer namespaces, and reports whether it confirmed the reading.
    """
    resolved = resolve_table(table, catalog)
    if resolved.object_name is None:
        return None, None, None
    kind = None if resolved.kind == "unknown" else resolved.kind
    return resolved.object_name, kind, resolved.confidence


class RoutineParser:
    """Static heuristic analysis of a single routine's ABAP source."""

    def analyze(
        self,
        *,
        code_id: str,
        kind: RoutineKind,
        lines: list[str],
        provenance: Provenance,
        catalog: Mapping[str, Iterable[str]] | None = None,
        extra_caveats: list[str] | None = None,
        followed_forms: Iterable[str] | None = None,
    ) -> RoutineAnalysis:
        """Analyse one routine.

        ``catalog`` maps an object kind to the known object names of that kind. Supplying it lets a
        table reading be *confirmed* against real objects instead of resting on the naming
        convention; without it every resolution is reported as ``advisory``.

        ``extra_caveats`` lets the caller record something about the *source it supplied* that the
        parser cannot know - specifically that a performed subroutine's body was appended, and from
        where, so a line number in the result can still be located (D39).

        ``followed_forms`` names subroutines whose bodies the caller appended. Those stop counting
        as unresolved calls, because they *were* followed: leaving them in would make the evidence
        line say three calls went unfollowed when only two did, which overstates the shortfall as
        surely as omitting a caveat would understate it.
        """
        cleaned = _strip_comments(lines)
        code_lines = [text for _, text in cleaned]
        joined = " ".join(code_lines)

        table_deps = self._table_dependencies(joined, catalog)
        anti, complexity = self._scan_lines(cleaned)
        unresolved = self._unresolved_calls(cleaned)
        if followed_forms:
            followed = {name.strip().lower() for name in followed_forms}
            unresolved = [
                ref
                for ref in unresolved
                if not (ref.call_kind == "form" and ref.object_name.strip().lower() in followed)
            ]
        complexity.line_count = len(lines)
        complexity.call_count = len(unresolved)

        caveats = [
            "static heuristic parse; dynamic SQL, function-module and class-method calls are not "
            "followed, so table dependencies are a lower bound",
            "/BIC/ and /BI0/ table-to-object resolution is advisory (naming-convention based)",
            *(extra_caveats or []),
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

    def _table_dependencies(
        self, joined: str, catalog: Mapping[str, Iterable[str]] | None = None
    ) -> list[TableDependency]:
        seen: dict[str, TableDependency] = {}
        for statement in joined.split("."):
            if not _SELECT_RE.search(statement):
                continue
            for match in _FROM_RE.finditer(statement):
                table = match.group(1).strip()
                if not table or table in seen or table.lower() in ("table",):
                    continue
                # Any namespaced table is offered to the resolver, not just /BIC/ and /BI0/: a
                # provider in its own namespace generates tables there too, and only asking about
                # the two SAP namespaces left those reads permanently unresolved.
                resolved, kind, confidence = (
                    _resolve_bw_table(table, catalog)
                    if table.startswith("/")
                    else (None, None, None)
                )
                in_bw_namespace = table.upper().startswith(("/BIC/", "/BI0/"))
                seen[table] = TableDependency(
                    table=table,
                    access="read",
                    # A /BIC/ or /BI0/ table is BW-generated whether or not its name resolved;
                    # a customer-namespace table only counts as generated once it did.
                    is_bw_generated=in_bw_namespace or resolved is not None,
                    resolved_object=resolved,
                    resolved_kind=kind,
                    resolution_confidence=confidence,  # type: ignore[arg-type]
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
        """Flag ``FOR ALL ENTRIES`` whose driver table has no emptiness guard.

        An **upper bound**, and the opposite direction of travel from the table dependencies. The
        guard forms recognised are ``IS [NOT] INITIAL``, ``lines( )`` and ``DESCRIBE TABLE``; a
        routine that instead tests ``sy-subrc`` after filling the driver table is guarded in fact
        but counted here, so the number is a ceiling on the risk rather than a measurement of it.
        The driver table is named in the detail so a reviewer can settle each case without
        re-reading the whole routine.
        """
        guarded: set[str] = set()
        for _, code in cleaned:
            for match in _GUARD_RE.finditer(code):
                name = match.group(1) or match.group(3) or match.group(4)
                if name:
                    guarded.add(name.lower())
        flagged: list[AntiPattern] = []
        for line_no, code in cleaned:
            fae = _FAE_RE.search(code)
            if fae is None:
                continue
            driver = fae.group(1).lower().lstrip("@").rstrip("[]")
            if driver in guarded or driver in _FRAMEWORK_FILLED:
                continue
            flagged.append(
                AntiPattern(
                    kind="missing_for_all_entries",
                    line_no=line_no,
                    detail=(
                        f"FOR ALL ENTRIES over {driver} with no is-not-initial guard; if it is "
                        "empty the read returns the whole table. Upper bound: an sy-subrc check "
                        "after filling it is a guard this parser does not follow."
                    ),
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
