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

**Where the logic is not in the include at all.** A widespread site pattern keeps ``ZXRSAU0n``
almost empty: it builds a program name from ``I_DATASOURCE`` and calls into it, typically
``CONCATENATE '<PREFIX>' i_datasource INTO prog`` followed by ``PERFORM ... IN PROGRAM (prog)``.
ABAP resolves that name at *runtime*, so reading the include statically finds no table, no
``FOR ALL ENTRIES`` and no per-record ``SELECT`` - and the honest-looking conclusion "this
enhancement does almost nothing" is precisely wrong. Measured on one production landscape, the
satellite layer held 84 ``SELECT``s and 74 ``FOR ALL ENTRIES``, 18 of them unguarded, none of it
reachable from the include.

This module therefore reads the dispatch rather than stopping at it: the ``CONCATENATE`` literals
next to a dynamic ``PERFORM`` give the naming rule, and each candidate ``<prefix><DATASOURCE>`` is
resolved as a program of its own.

**No naming convention is assumed.** The prefix is whatever the site's own ABAP concatenates, so it
is read out of the source rather than configured, and the rule in force is evidence instead of an
assumption. Any customer-namespace form is accepted - ``Z...``, ``Y...`` or ``/PARTNER/...`` - and
because each prefix is attributed to the exit slot whose dispatch produced it, a site that uses
different prefixes for transaction data and master data has that difference reported rather than
flattened. ``satellite_program_prefixes`` on the ECC profile exists only for sites whose
name-building this parser cannot read; it defaults to empty and supplements what was derived rather
than replacing it.

Because a satellite serves exactly one DataSource, its findings attribute cleanly, with none of the
over-attribution risk that makes branch slicing conservative.
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
    ExitSatellite,
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

# `PERFORM <form> IN PROGRAM (<var>)` - the program is named by a variable, so its value is only
# known at runtime. Note the parenthesised group: `IN PROGRAM zfoo` (a literal) is static and
# followable, and is deliberately not matched here.
_DYN_PERFORM_RE = re.compile(
    r"\bperform\b[^.]*?\bin\s+program\s*\(\s*([a-z0-9_<>-]+)\s*\)", re.IGNORECASE
)
# `CONCATENATE '<PREFIX>' i_datasource INTO prog` - the literals are the naming rule.
_CONCATENATE_RE = re.compile(r"^\s*concatenate\b(.*)$", re.IGNORECASE)
# String-template and inline forms: |<PREFIX>{ i_datasource }| or '<PREFIX>' && i_datasource.
_TEMPLATE_RE = re.compile(r"\|([A-Za-z0-9_/]+?)\{", re.IGNORECASE)

_MAX_HANDLED = 500
# Exit includes on a mature system run to thousands of lines; full text is opt-in and capped so a
# single call cannot flood the model context.
_MAX_SOURCE_LINES = 4000
# ABAP program names live in domain PROGRAMM (CHAR40). A candidate longer than this cannot exist,
# so probing it would spend a request to learn nothing.
_MAX_PROGRAM_NAME = 40
# A satellite prefix has to look like the start of a customer object name. This rejects the ordinary
# string literals that surround a CONCATENATE - separators, messages, spaces - without needing to
# understand the statement's structure.
_PREFIX_RE = re.compile(r"^(?:Z|Y|/[A-Z0-9_]+/)[A-Z0-9_]*$")
# A DataSource in a partner namespace, e.g. /PARTNER/SOME_DS -> SOME_DS.
_NAMESPACE_RE = re.compile(r"^/[A-Z0-9_]+/(.+)$")


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


def parse_dynamic_dispatch(lines: list[str]) -> tuple[bool, list[str]]:
    """Detect a runtime-named ``PERFORM`` and recover the prefixes used to build the name.

    Returns ``(dispatches_dynamically, prefixes)``. The flag matters on its own: it is the
    difference between "this include implements nothing" and "this include's logic is somewhere a
    static read cannot reach", and only the second warrants going looking.

    Prefix recovery is deliberately narrow. Only literals on a line that also mentions
    ``I_DATASOURCE`` are considered, and each must look like the start of a customer object name, so
    a message text or a separator sitting in an unrelated ``CONCATENATE`` is not mistaken for a
    naming rule. The cost of a wrong prefix is a wasted 404, not a wrong answer, but a plausible
    wrong prefix would still put noise in the report.
    """
    dynamic = False
    prefixes: list[str] = []

    def add(value: str) -> None:
        candidate = value.strip().upper()
        if candidate and _PREFIX_RE.match(candidate) and candidate not in prefixes:
            prefixes.append(candidate)

    for raw in lines:
        code = _code_of(raw)
        if code is None:
            continue
        if _DYN_PERFORM_RE.search(code):
            dynamic = True
        lower = code.lower()
        if "i_datasource" not in lower:
            continue
        # A CONCATENATE, or any assignment that pastes a literal onto the DataSource name.
        if _CONCATENATE_RE.match(code) or "&&" in code or "|" in code:
            for literal in _LITERAL_RE.finditer(code):
                add(literal.group(1))
            for template in _TEMPLATE_RE.finditer(code):
                add(template.group(1))
    return dynamic, prefixes


def satellite_program_name(prefix: str, datasource: str) -> str | None:
    """``<prefix><datasource>`` as a legal ABAP program name, or ``None`` if it cannot be one.

    **A namespaced DataSource loses its namespace.** ``/PARTNER/SOME_DS`` cannot simply be pasted
    onto a prefix - a slash is a namespace delimiter and is legal only at the front of a name - so
    the namespace is dropped and the satellite resolves to ``<PREFIX>SOME_DS``. Rejecting these
    outright, which an earlier version did, silently lost every namespaced DataSource: on the
    landscape where this was measured that was four of the satellites actually present, each
    reported as "no satellite" while the program sat there to be read.

    A name over the 40-character length of domain ``PROGRAMM`` cannot exist, so it is skipped rather
    than probed. Skipped is reported separately from absent, because "could not be a program name"
    and "is not a program" are different facts.
    """
    head = prefix.strip().upper()
    body = datasource.strip().upper()
    # Drop a leading /NAMESPACE/; anything else containing a slash is not a name this rule can form.
    namespaced = _NAMESPACE_RE.match(body)
    if namespaced:
        body = namespaced.group(1)
    name = f"{head}{body}"
    if "/" in body or " " in name or not body:
        return None
    if len(name) > _MAX_PROGRAM_NAME:
        return None
    return name


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
        # One probe per program name per service instance. Satellite resolution is the only part of
        # this service that scales with the DataSource catalogue, and a caller that asks twice about
        # overlapping DataSource sets must not pay twice for the same program.
        self._satellite_memo: dict[str, ExitSatellite] = {}

    def inventory(
        self, *, include_source: bool = False, datasources: list[str] | None = None
    ) -> ExitInventory:
        """Read every exit slot. An absent slot is a finding, not a failure.

        ``datasources`` opts into satellite resolution. It has to be supplied by the caller because
        the authoritative list of enhanced DataSources lives in BW, not in the source system - and
        under dynamic dispatch a satellite can exist for a DataSource the include never names, so
        deriving candidates from the include alone would miss exactly the cases worth finding.
        """
        profile = self._connector.profile_name or ""
        client = self._connector.client or ""
        exits: list[ExitSource] = []
        caveats: list[str] = [
            "Handled-DataSource detection reads CASE literals from the exit source. A dispatch "
            "delegated to a class, or built in a way this parser cannot read, is not detected, so "
            "the list is a lower bound; literals from an unrelated nested CASE can also appear. A "
            "program name built at runtime is detected and followed separately (see satellites).",
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

        satellites, prefixes, considered, sat_caveats = self._resolve_satellites(
            exits, datasources, profile=profile, client=client, include_source=include_source
        )
        caveats.extend(sat_caveats)
        return ExitInventory(
            profile=profile,
            client=client,
            exits=exits,
            available_count=sum(1 for e in exits if e.available),
            handled_datasources=union,
            satellites=satellites,
            satellite_prefixes=prefixes,
            satellites_found_count=sum(1 for s in satellites if s.available),
            satellite_candidates_considered=considered,
            caveats=caveats,
        )

    # --- satellite exit programs -----------------------------------------------------------

    def _resolve_satellites(
        self,
        exits: list[ExitSource],
        datasources: list[str] | None,
        *,
        profile: str,
        client: str,
        include_source: bool,
    ) -> tuple[list[ExitSatellite], list[str], int, list[str]]:
        """Probe ``<prefix><DATASOURCE>`` for each candidate and analyse whatever exists."""
        prefix_origin = self._prefix_origins(exits)
        prefixes = list(prefix_origin)
        dynamic_slots = [slot.include_name for slot in exits if slot.dynamic_dispatch]

        if not prefixes:
            if dynamic_slots:
                # The strongest statement available: the logic is provably elsewhere and this parser
                # could not learn where. Silence here would read as "there is nothing else".
                return (
                    [],
                    [],
                    0,
                    [
                        f"{', '.join(dynamic_slots)} dispatch(es) to a program named at runtime, "
                        "so the enhancement logic is not in the include and this parser could not "
                        "recover the naming rule. Set 'satellite_program_prefixes' on the ECC "
                        "profile to read that layer; until then the per-DataSource table reads and "
                        "per-record SELECTs below are a floor, not a measurement."
                    ],
                )
            return [], [], 0, []
        if datasources is None:
            naming = ", ".join(f"{p}<DATASOURCE>" for p in prefixes)
            return (
                [],
                prefixes,
                0,
                [
                    f"Satellite exit programs are named {naming} on this system, but no DataSource "
                    "list was supplied, so none were read. The per-DataSource risk below covers "
                    "only logic held in the include itself."
                ],
            )

        budget = self._connector.max_satellite_fetches
        satellites: list[ExitSatellite] = []
        skipped_names: list[str] = []
        considered = 0
        spent = 0
        truncated_at: str | None = None

        for datasource in datasources:
            name_upper = datasource.strip().upper()
            if not name_upper:
                continue
            for prefix in prefixes:
                program = satellite_program_name(prefix, name_upper)
                if program is None:
                    skipped_names.append(f"{prefix}{name_upper}")
                    continue
                considered += 1
                cached = self._satellite_memo.get(program)
                if cached is not None:
                    satellites.append(cached)
                    continue
                if spent >= budget:
                    truncated_at = truncated_at or name_upper
                    continue
                spent += 1
                record = self._probe_satellite(
                    program=program,
                    prefix=prefix,
                    datasource=name_upper,
                    dispatched_from=prefix_origin[prefix],
                    profile=profile,
                    client=client,
                    include_source=include_source,
                )
                self._satellite_memo[program] = record
                satellites.append(record)

        return (
            satellites,
            prefixes,
            considered,
            self._satellite_caveats(
                satellites=satellites,
                prefixes=prefixes,
                dynamic_slots=dynamic_slots,
                skipped_names=skipped_names,
                budget=budget,
                spent=spent,
                truncated_at=truncated_at,
            ),
        )

    def _prefix_origins(self, exits: list[ExitSource]) -> dict[str, ExitDataKind | None]:
        """Prefix to the exit slot that produced it; ``None`` for a configured prefix.

        Derived prefixes come first and win the attribution: a prefix read out of the transaction
        exit is known to serve transaction data, whereas a configured one carries no such evidence
        and is recorded as unattributed rather than assigned a kind it may not have.
        """
        origins: dict[str, ExitDataKind | None] = {}
        for slot in exits:
            for prefix in slot.satellite_prefixes:
                origins.setdefault(prefix, slot.data_kind)
        for prefix in self._connector.satellite_program_prefixes:
            origins.setdefault(prefix, None)
        return origins

    def _probe_satellite(
        self,
        *,
        program: str,
        prefix: str,
        datasource: str,
        dispatched_from: ExitDataKind | None,
        profile: str,
        client: str,
        include_source: bool,
    ) -> ExitSatellite:
        def missing(reason: ExitUnavailableReason, note: str | None) -> ExitSatellite:
            return ExitSatellite(
                program_name=program,
                datasource=datasource,
                prefix=prefix,
                dispatched_from=dispatched_from,
                available=False,
                unavailable_reason=reason,
                note=note,
            )

        try:
            # Programs first: a satellite is a standalone program, so the include path would 404 and
            # cost an extra round trip on every one of hundreds of probes.
            response, path = self._connector.fetch_source(program, order=("programs", "includes"))
        except AdtError as exc:
            return missing("fetch_failed", str(exc))

        reason = self._connector.classify_status(response.status)
        if reason is not None:
            return missing(
                reason,
                f"no program {program} exists in this system and client, so this DataSource has no "
                "satellite exit"
                if reason == "absent"
                else _UNAVAILABLE_NOTES.get(reason),
            )

        lines = response.text.splitlines()
        analysis = self._parser.analyze(
            code_id=program,
            kind="exit",
            lines=lines,
            provenance=Provenance(
                source_table="ADT",
                source_key={"PROGRAM": program, "CLIENT": client, "PROFILE": profile},
            ),
        )
        source, note = self._source_payload(response.text, lines, include_source)
        return ExitSatellite(
            program_name=program,
            datasource=datasource,
            prefix=prefix,
            dispatched_from=dispatched_from,
            available=True,
            line_count=len(lines),
            source=source,
            table_reads=[dep.table for dep in analysis.table_dependencies],
            per_record_selects=sum(
                1 for ap in analysis.anti_patterns if ap.kind == "select_in_loop"
            ),
            unguarded_for_all_entries=sum(
                1 for ap in analysis.anti_patterns if ap.kind == "missing_for_all_entries"
            ),
            anti_pattern_kinds=sorted({ap.kind for ap in analysis.anti_patterns}),
            unresolved_call_count=len(analysis.unresolved_refs),
            analysis=analysis,
            provenance=AdtProvenance(
                profile=profile,
                client=client,
                adt_path=path,
                object_name=program,
                object_kind="program",
                fetched_at=datetime.now(UTC),
            ),
            note=note,
        )

    @staticmethod
    def _satellite_caveats(
        *,
        satellites: list[ExitSatellite],
        prefixes: list[str],
        dynamic_slots: list[str],
        skipped_names: list[str],
        budget: int,
        spent: int,
        truncated_at: str | None,
    ) -> list[str]:
        found = [s for s in satellites if s.available]
        naming = ", ".join(f"{p}<DATASOURCE>" for p in prefixes)
        caveats = [
            f"Satellite exit programs ({naming}) were read as separate programs because "
            f"{', '.join(dynamic_slots) or 'the exit'} dispatches on a program name built at "
            f"runtime, which no static read of the include can follow: {len(found)} of "
            f"{spent} probed program(s) exist. Their table reads and per-record SELECTs are "
            "attributed to a single DataSource each, so unlike a CASE branch there is no "
            "over-attribution risk."
        ]
        unreadable = [s for s in satellites if not s.available and s.unavailable_reason != "absent"]
        if unreadable:
            caveats.append(
                f"{len(unreadable)} satellite program(s) could not be read (authorisation or "
                "transport), so their logic is unknown rather than absent."
            )
        if skipped_names:
            caveats.append(
                f"{len(skipped_names)} candidate(s) cannot be an ABAP program name - the length "
                f"limit is {_MAX_PROGRAM_NAME} characters, and a slash is legal only as a leading "
                "namespace - so they were not probed and their satellite status is unknown, not "
                f"absent (e.g. {skipped_names[0]}). A leading /NAMESPACE/ is stripped rather than "
                "skipped, which is the convention observed on real systems."
            )
        if truncated_at is not None:
            caveats.append(
                f"The satellite probe budget of {budget} request(s) was exhausted at "
                f"{truncated_at}; the remaining DataSources were not probed. Raise "
                "'max_satellite_fetches' on the ECC profile for full coverage."
            )
        return caveats

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
        dynamic, prefixes = parse_dynamic_dispatch(lines)
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
            dynamic_dispatch=dynamic,
            satellite_prefixes=prefixes,
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
