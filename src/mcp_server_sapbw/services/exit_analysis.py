"""Static analysis of extractor-exit ABAP fetched from a source system.

Three things are derived from an exit include:

**Which DataSources it enhances.** A BW extractor exit is one procedure serving every enhanced
DataSource, dispatching on ``I_DATASOURCE`` in a ``CASE``. The ``WHEN`` literals therefore name the
enhanced DataSources - turning "an enhancement exists somewhere" into "this code runs for these
DataSources". It is advisory: a dispatch built dynamically, or delegated to a subroutine or class,
contributes nothing, and literals from an unrelated nested ``CASE`` can contribute false positives.
Callers holding a BW connection should intersect the result with the real DataSource catalogue,
which removes the false positives; that is what scenario 9.6 does.

**What each branch does.** Risk is measured per ``WHEN`` branch, not per include. One include serves
many DataSources, so attributing the whole include's table reads (or its per-record ``SELECT``s) to
any single DataSource would overstate - and a false "high severity" is worse than a documented gap.
Branch slicing stops at the next ``WHEN`` or ``ENDCASE``, which under-reports when a branch contains
a nested ``CASE``; erring toward under-attribution is deliberate. A branch that cannot be delimited
is marked ``resolved=False`` rather than approximated.

**What the code does overall.** Reused wholesale from :class:`~.routine_parser.RoutineParser`, which
already finds table reads, ``SELECT`` inside ``LOOP``, unguarded ``FOR ALL ENTRIES`` and
unfollowable calls. The mission's 9.6 questions - which tables the exit reads, and whether it does
per-record ``SELECT``s - are exactly that parser's output, so no second analyser exists.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..connectors.ecc import EXIT_SLOTS, AdtError, EccConnector
from ..models.ecc import (
    AdtProvenance,
    ExitBranch,
    ExitDataKind,
    ExitInventory,
    ExitSource,
    ExitUnavailableReason,
)
from ..models.provenance import Provenance
from .routine_parser import RoutineParser

# A CASE branch opener. `WHEN OTHERS` is excluded: it is the fallback, not a named DataSource.
_WHEN_RE = re.compile(r"^\s*when\b(?!\s+others\b)(.*)$", re.IGNORECASE)
_WHEN_ANY_RE = re.compile(r"^\s*when\b", re.IGNORECASE)
_ENDCASE_RE = re.compile(r"^\s*endcase\b", re.IGNORECASE)
# Direct comparison against the exit's importing parameter (dispatch by IF rather than CASE).
_COMPARE_RE = re.compile(r"\bi_datasource\s*=\s*'([^']+)'", re.IGNORECASE)
_LITERAL_RE = re.compile(r"'([^']*)'")

_MAX_HANDLED = 500
# Exit includes on a mature system run to thousands of lines; full text is opt-in and capped so a
# single call cannot flood the model context.
_MAX_SOURCE_LINES = 4000


def _code_of(raw: str) -> str | None:
    """Strip ABAP comments from one line; ``None`` for a full-line comment."""
    if raw.lstrip().startswith("*"):
        return None
    quote = raw.find('"')  # inline comment; ABAP string literals use single quotes
    return raw[:quote] if quote != -1 else raw


def parse_handled_datasources(lines: list[str]) -> list[str]:
    """DataSource names the exit dispatches on, in first-appearance order (advisory)."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        name = value.strip().upper()
        if name and name not in seen and len(found) < _MAX_HANDLED:
            seen.add(name)
            found.append(name)

    for raw in lines:
        code = _code_of(raw)
        if code is None:
            continue
        when = _WHEN_RE.match(code)
        if when:
            for literal in _LITERAL_RE.finditer(when.group(1)):
                add(literal.group(1))
        for compare in _COMPARE_RE.finditer(code):
            add(compare.group(1))
    return found


def slice_branches(lines: list[str]) -> dict[str, list[str]]:
    """Map each ``WHEN``-dispatched DataSource to the lines of its own branch.

    A branch runs from its ``WHEN`` line to the next ``WHEN`` or ``ENDCASE``. ``WHEN 'A' OR 'B'.``
    gives both names the same lines. Names dispatched by ``IF`` get no branch and are absent here.
    """
    branches: dict[str, list[str]] = {}
    current: list[str] = []
    open_names: list[str] = []

    def close() -> None:
        for name in open_names:
            branches.setdefault(name, []).extend(current)

    for raw in lines:
        code = _code_of(raw)
        if code is None:
            continue
        if _ENDCASE_RE.match(code):
            close()
            current, open_names = [], []
            continue
        if _WHEN_ANY_RE.match(code):
            close()
            current = []
            when = _WHEN_RE.match(code)
            open_names = (
                [m.group(1).strip().upper() for m in _LITERAL_RE.finditer(when.group(1))]
                if when
                else []
            )
            open_names = [name for name in open_names if name]
            continue
        if open_names:
            current.append(raw)
    close()
    return branches


class ExitAnalysisService:
    """Fetches the four extractor-exit slots and analyses whatever the source system returns."""

    def __init__(self, connector: EccConnector, parser: RoutineParser | None = None) -> None:
        self._connector = connector
        self._parser = parser or RoutineParser()

    def inventory(self, *, include_source: bool = False) -> ExitInventory:
        """Read every exit slot. An absent slot is a finding, not a failure."""
        profile = self._connector.profile_name or ""
        client = self._connector.client or ""
        exits: list[ExitSource] = []
        caveats: list[str] = [
            "Handled-DataSource detection reads CASE literals from the exit source. A dispatch "
            "built dynamically, or delegated to a subroutine or class, is not detected, so the "
            "list is a lower bound; literals from an unrelated nested CASE can also appear.",
            "Per-DataSource risk is measured within that DataSource's own CASE branch, so it is "
            "not inflated by what other branches of the same include do. A branch containing a "
            "nested CASE is under-reported rather than over-reported.",
            "Table dependencies and anti-patterns come from the same heuristic parser used for BW "
            "transformation routines and carry the same lower-bound caveat.",
        ]
        fetch_failures = 0

        for slot in EXIT_SLOTS:
            record = self._read_slot(
                function_module=slot.function_module,
                include=slot.include,
                data_kind=slot.data_kind,
                profile=profile,
                client=client,
                include_source=include_source,
            )
            if record.unavailable_reason in ("fetch_failed", "unauthorized", "forbidden"):
                fetch_failures += 1
            exits.append(record)

        if fetch_failures:
            caveats.append(
                f"{fetch_failures} of {len(EXIT_SLOTS)} exit slot(s) could not be read, so the "
                "enhancement logic for those DataSource kinds is unknown rather than absent. "
                "Reading ABAP over ADT needs the ADT ICF node active and a user authorised for it"
            )
        union: list[str] = []
        for record in exits:
            union.extend(name for name in record.handled_datasources if name not in union)
        return ExitInventory(
            profile=profile,
            client=client,
            exits=exits,
            available_count=sum(1 for e in exits if e.available),
            handled_datasources=union,
            caveats=caveats,
        )

    def _read_slot(
        self,
        *,
        function_module: str,
        include: str,
        data_kind: ExitDataKind,
        profile: str,
        client: str,
        include_source: bool,
    ) -> ExitSource:
        try:
            response, path = self._connector.fetch_source(include)
        except AdtError as exc:
            return self._unavailable(function_module, include, data_kind, "fetch_failed", str(exc))

        reason = self._connector.classify_status(response.status)
        if reason is not None:
            return self._unavailable(
                function_module, include, data_kind, reason, _UNAVAILABLE_NOTES.get(reason)
            )

        lines = response.text.splitlines()
        analysis = self._parser.analyze(
            code_id=include,
            kind="exit",
            lines=lines,
            # An ADT read is not a table row; 'ADT' says so plainly and the key locates the object.
            provenance=Provenance(
                source_table="ADT",
                source_key={"INCLUDE": include, "CLIENT": client, "PROFILE": profile},
            ),
        )
        handled = parse_handled_datasources(lines)
        source, note = self._source_payload(response.text, lines, include_source)
        return ExitSource(
            exit_function_module=function_module,
            include_name=include,
            data_kind=data_kind,
            available=True,
            line_count=len(lines),
            source=source,
            handled_datasources=handled,
            branches=self._branches(include, lines, handled),
            analysis=analysis,
            provenance=AdtProvenance(
                profile=profile,
                client=client,
                adt_path=path,
                object_name=include,
                object_kind="include",
                fetched_at=datetime.now(UTC),
            ),
            note=note,
        )

    def _branches(self, include: str, lines: list[str], handled: list[str]) -> list[ExitBranch]:
        sliced = slice_branches(lines)
        branches: list[ExitBranch] = []
        for name in handled:
            branch_lines = sliced.get(name)
            if not branch_lines:
                # Dispatched by IF, or a branch this slicer could not delimit.
                branches.append(ExitBranch(datasource=name, resolved=False))
                continue
            analysis = self._parser.analyze(
                code_id=f"{include}:{name}",
                kind="exit",
                lines=branch_lines,
                provenance=Provenance(
                    source_table="ADT", source_key={"INCLUDE": include, "BRANCH": name}
                ),
            )
            branches.append(
                ExitBranch(
                    datasource=name,
                    resolved=True,
                    line_count=len(branch_lines),
                    table_reads=[dep.table for dep in analysis.table_dependencies],
                    per_record_selects=sum(
                        1 for ap in analysis.anti_patterns if ap.kind == "select_in_loop"
                    ),
                    anti_pattern_kinds=sorted({ap.kind for ap in analysis.anti_patterns}),
                    unresolved_call_count=len(analysis.unresolved_refs),
                )
            )
        return branches

    @staticmethod
    def _source_payload(
        text: str, lines: list[str], include_source: bool
    ) -> tuple[str | None, str | None]:
        if not include_source:
            return None, None
        if len(lines) > _MAX_SOURCE_LINES:
            return (
                "\n".join(lines[:_MAX_SOURCE_LINES]),
                f"source truncated to the first {_MAX_SOURCE_LINES} of {len(lines)} lines; "
                "the analysis below covers the whole include",
            )
        return text, None

    @staticmethod
    def _unavailable(
        function_module: str,
        include: str,
        data_kind: ExitDataKind,
        reason: ExitUnavailableReason,
        note: str | None,
    ) -> ExitSource:
        return ExitSource(
            exit_function_module=function_module,
            include_name=include,
            data_kind=data_kind,
            available=False,
            unavailable_reason=reason,
            note=note,
        )


_UNAVAILABLE_NOTES: dict[ExitUnavailableReason, str] = {
    "absent": (
        "the include does not exist in this system and client, which means no enhancement of this "
        "DataSource kind is implemented"
    ),
    "unauthorized": "ADT rejected the credentials for this profile",
    "forbidden": (
        "ADT refused the read; the user is authenticated but not authorised for source display "
        "(typically S_DEVELOP), or the ADT ICF node is inactive"
    ),
    "fetch_failed": "the ADT request did not complete",
}
