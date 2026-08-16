"""Reads the BW 3.x dataflow layer: transfer rules, update rules, InfoSource routing.

Backs Scenario 5. The 3.x path is ``DataSource -> InfoSource -> transfer structure -> communication
structure -> update rules -> target``; ``RSISOSMAP`` is the bridge that ties a DataSource to its
transfer structure, and ``RSTSRULES`` holds the field-level rules.

Two things about these tables are easy to get wrong and are handled explicitly here:

* **``OBJVERS = 'A'`` is not auto-injected.** The dialect only injects the active-version filter for
  the ``RSD``/``RSO``/``RSZ``/``RSTRAN`` families. ``RSTS``, ``RSTSRULES``, ``RSISOSMAP``,
  ``RSUPDINFO`` and friends match none of those, so every query here states the filter itself.
  Omitting it silently returns modified and delivered versions alongside the active one.
* **``RSTS`` keys on ``TRANSTRU``, and has no ``OLTPSOURCE``.** ``TSTPNM`` is the transport package,
  not a join key. The DataSource association only exists via ``RSISOSMAP``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models.provenance import UnsupportedResult
from ..models.threex import RoutineRef, ThreeXFlow, ThreeXFlowReport, TransferRule, UpdateRule
from .base import Repository

# A transfer structure can carry thousands of field rules; this bounds one structure's rule read.
_MAX_RULES = 2000
# Bound on the update-rule scan. Only 21 are active on the reference system, so this is generous.
_MAX_UPDATE_RULES = 1000

# RSAROUT.CODETP (domain RSCODETP, read from the dictionary). The registry spans both the 7.x and
# 3.x worlds, which is what lets a 3.x rule's routine be attributed at all.
# Live: TF 9,971 (transformation), TR 378 (transfer rules), blank 82, IC 15, UR 3.
_ROUTINE_KIND: dict[str, str] = {
    "UR": "update_rule",
    "TC": "time_conversion",
    "TR": "transfer_rule",
    "IC": "infoobject_conversion",
    "GR": "global_routine",
    "S1": "deletion_routine_sdl",
    "TF": "transformation",
}

# RSAROUT.DEPENDENCY (domain RSROUTDEP). A routine taking the whole source structure has a wider
# change-impact surface than one naming its fields, and BW records which it is.
# Live: blank 10,119 (indeterminate), '1' 243, '0' 52, '2' 35.
_ROUTINE_DEPENDENCY: dict[str, str] = {
    "": "indeterminate",
    "0": "uses no source-structure field",
    "1": "uses selected source-structure fields",
    "2": "uses the whole source structure",
}

# RSAROUTT carries one row per language; English is the fallback the rest of the server uses.
_TEXT_LANGUAGE = "E"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ThreeXRepository(Repository):
    """The 3.x dataflow layer, alongside the 7.x transformations the rest of the server reads."""

    # --- flows ---------------------------------------------------------------------------

    def list_flows(
        self,
        *,
        datasource: str | None = None,
        only_without_transformation: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> ThreeXFlowReport | UnsupportedResult:
        """DataSources routed through a 3.x transfer structure, with their rule profile."""
        unsupported = self.require("infosource_map", "transfer_rule")
        if unsupported is not None:
            return unsupported

        rules_by_ts = self._rule_profile()
        start_routines = self._transfer_structures_with_start_routine()
        seven_x = self._datasources_with_transformation()
        update_targets = self._update_targets_by_infosource()

        where = ["OBJVERS = 'A'", "OLTPSOURCE <> ''"]
        params: list[Any] = []
        if datasource:
            where.append("UPPER(OLTPSOURCE) LIKE ?")
            params.append(f"%{datasource.upper()}%")

        rows = self.select(
            self.dialect.build_select(
                columns=["OLTPSOURCE", "LOGSYS", "ISOURCE", "TRANSTRU", "ISTYPE"],
                from_logical="infosource_map",
                where=where,
                params=params,
                order_by=["OLTPSOURCE", "TRANSTRU"],
            )
        )

        flows: list[ThreeXFlow] = []
        for oltp, logsys, isource, transtru, istype in rows:
            name = _clean(oltp)
            ts = _clean(transtru)
            if name is None:
                continue
            profile = rules_by_ts.get(ts or "", (0, 0, 0, 0))
            if only_without_transformation and name in seven_x:
                continue
            # A transfer structure with no rules at all is a PSA-only shell rather than a 3.x flow.
            if profile[0] == 0:
                continue
            source_name = _clean(isource)
            flows.append(
                ThreeXFlow(
                    datasource=name,
                    logical_system=_clean(logsys),
                    infosource=source_name,
                    transfer_structure=ts,
                    infosource_type=_clean(istype),
                    has_start_routine=ts in start_routines,
                    rule_count=profile[0],
                    rules_with_routine=profile[1],
                    rules_with_formula=profile[2],
                    rules_with_constant=profile[3],
                    has_seven_x_transformation=name in seven_x,
                    update_rule_targets=sorted(update_targets.get(source_name or "", set())),
                    provenance=[
                        self.provenance(
                            "infosource_map",
                            {"OLTPSOURCE": name, "TRANSTRU": ts or "", "OBJVERS": "A"},
                        )
                    ],
                )
            )

        total = len(flows)
        page = flows[offset : offset + limit]
        caveats: list[str] = []
        if not self.capability.is_available("update_rule"):
            caveats.append(
                "RSUPDINFO is unavailable, so update-rule targets are not resolved; a flow's "
                "target is unknown rather than absent"
            )
        three_x_only = sum(1 for f in flows if not f.has_seven_x_transformation)
        caveats.append(
            "a DataSource with both a 3.x route and a 7.x transformation is normally "
            "mid-migration; which one actually runs is decided by the InfoPackage or DTP in the "
            "chain, not here"
        )
        return ThreeXFlowReport(
            flows=page,
            total_count=total,
            limit=limit,
            offset=offset,
            active_transfer_structures=len(rules_by_ts),
            datasources_with_3x_route=len({f.datasource for f in flows}),
            datasources_with_7x_transformation=len(
                {f.datasource for f in flows if f.has_seven_x_transformation}
            ),
            datasources_3x_only=three_x_only,
            active_update_rules=self._active_update_rule_count(),
            caveats=caveats,
        )

    # --- transfer rules ------------------------------------------------------------------

    def get_transfer_rules(self, transfer_structure: str) -> list[TransferRule] | UnsupportedResult:
        """Field-level transfer rules for one transfer structure."""
        unsupported = self.require("transfer_rule")
        if unsupported is not None:
            return unsupported
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[
                        "TRANSTRU",
                        "COMSTRU",
                        "IOBJNM",
                        "IOBJNM_TS",
                        "FIXED_VALUE",
                        "CONVROUT_G",
                        "CONVROUT_L",
                        "FORMULA_ID",
                        "CONVERSION",
                    ],
                    from_logical="transfer_rule",
                    where=["OBJVERS = 'A'", "TRANSTRU = ?"],
                    params=[transfer_structure],
                    order_by=["IOBJNM"],
                ),
                limit=_MAX_RULES,
            )
        )
        registry = self.routine_registry([_clean(row[5]) or _clean(row[6]) or "" for row in rows])
        return [
            TransferRule(
                transfer_structure=str(row[0]).strip(),
                comm_structure=_clean(row[1]),
                infoobject=_clean(row[2]),
                infoobject_ts=_clean(row[3]),
                fixed_value=_clean(row[4]),
                conversion_routine_global=_clean(row[5]),
                conversion_routine_local=_clean(row[6]),
                formula_id=_clean(row[7]),
                conversion=_clean(row[8]),
                routine=registry.get(_clean(row[5]) or _clean(row[6]) or ""),
                provenance=self.provenance(
                    "transfer_rule",
                    {
                        "TRANSTRU": str(row[0]).strip(),
                        "IOBJNM": _clean(row[2]) or "",
                        "OBJVERS": "A",
                    },
                ),
            )
            for row in rows
        ]

    # --- update rules --------------------------------------------------------------------

    def list_update_rules(self, *, limit: int = 100) -> list[UpdateRule] | UnsupportedResult:
        """Active 3.x update rules: InfoSource -> target."""
        unsupported = self.require("update_rule")
        if unsupported is not None:
            return unsupported
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=[
                        "UPDID",
                        "ISOURCE",
                        "INFOCUBE",
                        "STARTROUTINE",
                        "EXPERTMODE",
                        "OBJSTAT",
                    ],
                    from_logical="update_rule",
                    where=["OBJVERS = 'A'"],
                    order_by=["ISOURCE", "INFOCUBE"],
                ),
                limit=min(limit, _MAX_UPDATE_RULES),
            )
        )
        routine_counts = self._update_routine_counts()
        update_ids = [str(row[0]).strip() for row in rows]
        code_ids_by_update = self._update_routine_code_ids(update_ids)
        registry = self.routine_registry(
            [code for codes in code_ids_by_update.values() for code in codes]
        )
        return [
            UpdateRule(
                update_id=str(row[0]).strip(),
                infosource=_clean(row[1]),
                target=_clean(row[2]),
                has_start_routine=bool(_clean(row[3])),
                expert_mode=bool(_clean(row[4])),
                object_status=_clean(row[5]),
                routine_count=routine_counts.get(str(row[0]).strip(), 0),
                routines=[
                    registry[code]
                    for code in code_ids_by_update.get(str(row[0]).strip(), [])
                    if code in registry
                ],
                provenance=self.provenance(
                    "update_rule", {"UPDID": str(row[0]).strip(), "OBJVERS": "A"}
                ),
            )
            for row in rows
        ]

    # --- routine registry (RSAROUT / RSAROUTT) -------------------------------------------

    def routine_registry(self, code_ids: Sequence[str]) -> dict[str, RoutineRef]:
        """Registry entries for the given routine code ids, in as few queries as possible.

        Three bulk reads, never one per routine: the header (``RSAROUT``), the description
        (``RSAROUTT``) and the line count from ``RSAABAP``. A code id absent from the header is
        absent from the result rather than invented.

        ``RSAROUT`` holds no ABAP. That was the standing assumption behind treating 3.x routine
        logic as unreachable, and it is wrong: the source is in ``RSAABAP`` under the same code id,
        which every one of this system's 3.x routines resolves in. The registry supplies the type
        and the input dependency, and ``line_count`` says whether there is code to read.
        """
        wanted = [c for c in dict.fromkeys(str(c).strip() for c in code_ids) if c]
        if not wanted or not self.capability.is_available("routine_source_3x"):
            return {}
        placeholders = ", ".join("?" for _ in wanted)
        rows = self.select(
            self.dialect.build_select(
                columns=["CODEID", "CODETP", "OWNER", "OBJSTAT", "ACTIVFL", "DEPENDENCY"],
                from_logical="routine_source_3x",
                where=[f"CODEID IN ({placeholders})"],
                params=list(wanted),
            )
        )
        if not rows:
            return {}
        found = [str(row[0]).strip() for row in rows]
        descriptions = self._routine_descriptions(found)
        line_counts = self._routine_line_counts(found)

        registry: dict[str, RoutineRef] = {}
        for code_id, codetp, owner, objstat, activfl, dependency in rows:
            key = str(code_id).strip()
            if not key:
                continue
            kind_code = _clean(codetp)
            provenance = [self.provenance("routine_source_3x", {"CODEID": key, "OBJVERS": "A"})]
            if key in descriptions:
                provenance.append(
                    self.provenance("routine_text_3x", {"CODEID": key, "OBJVERS": "A"})
                )
            lines = line_counts.get(key, 0)
            if lines:
                provenance.append(self.provenance("routine_source", {"CODEID": key}))
            registry[key] = RoutineRef(
                code_id=key,
                kind=_ROUTINE_KIND.get(kind_code or "", "unknown"),
                kind_code=kind_code,
                description=descriptions.get(key),
                owner=_clean(owner),
                # Both columns say the same thing on this system; requiring both is the safer
                # reading, since an active-version row alone does not mean the routine is active.
                active=_clean(objstat) == "ACT" and _clean(activfl) == "X",
                source_dependency=_ROUTINE_DEPENDENCY.get(_clean(dependency) or ""),
                line_count=lines,
                source_available=lines > 0,
                provenance=provenance,
            )
        return registry

    def _routine_descriptions(self, code_ids: list[str]) -> dict[str, str]:
        """``RSAROUTT`` short texts, preferred language then English."""
        if not code_ids or not self.capability.is_available("routine_text_3x"):
            return {}
        placeholders = ", ".join("?" for _ in code_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=["CODEID", "LANGU", "TXTLG"],
                from_logical="routine_text_3x",
                where=[f"CODEID IN ({placeholders})"],
                params=list(code_ids),
            )
        )
        best: dict[str, tuple[int, str]] = {}
        for code_id, langu, text in rows:
            key = str(code_id).strip()
            value = _clean(text)
            if not key or value is None:
                continue
            rank = 0 if str(langu).strip().upper() == _TEXT_LANGUAGE else 1
            current = best.get(key)
            if current is None or rank < current[0]:
                best[key] = (rank, value)
        return {key: value[1] for key, value in best.items()}

    def _routine_line_counts(self, code_ids: list[str]) -> dict[str, int]:
        """How many ABAP lines each routine has. Zero means the logic cannot be read at all."""
        if not code_ids or not self.capability.is_available("routine_source"):
            return {}
        placeholders = ", ".join("?" for _ in code_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=["CODEID", "COUNT(*)"],
                from_logical="routine_source",
                where=[f"CODEID IN ({placeholders})"],
                params=list(code_ids),
                group_by=["CODEID"],
            )
        )
        return {str(code_id).strip(): int(count or 0) for code_id, count in rows}

    def _update_routine_code_ids(self, update_ids: list[str]) -> dict[str, list[str]]:
        """``{UPDID: [CODEID]}`` from ``RSUPDROUT``, ordered by the routine number."""
        if not update_ids or not self.capability.is_available("update_rule_routine"):
            return {}
        placeholders = ", ".join("?" for _ in update_ids)
        rows = self.select(
            self.dialect.build_select(
                columns=["UPDID", "CODEID"],
                from_logical="update_rule_routine",
                where=["OBJVERS = 'A'", f"UPDID IN ({placeholders})"],
                params=list(update_ids),
                order_by=["UPDID", "ROUTINE"],
            )
        )
        found: dict[str, list[str]] = {}
        for update_id, code_id in rows:
            key, code = str(update_id).strip(), _clean(code_id)
            if key and code:
                found.setdefault(key, []).append(code)
        return found

    # --- helpers -------------------------------------------------------------------------

    def _rule_profile(self) -> dict[str, tuple[int, int, int, int]]:
        """Per transfer structure: (rules, with routine, with formula, with constant)."""
        rows = self.select(
            self.dialect.build_select(
                columns=[
                    "TRANSTRU",
                    "COUNT(*)",
                    "SUM(CASE WHEN CONVROUT_G <> '' OR CONVROUT_L <> '' THEN 1 ELSE 0 END)",
                    "SUM(CASE WHEN FORMULA_ID <> '' THEN 1 ELSE 0 END)",
                    "SUM(CASE WHEN FIXED_VALUE <> '' THEN 1 ELSE 0 END)",
                ],
                from_logical="transfer_rule",
                where=["OBJVERS = 'A'"],
                group_by=["TRANSTRU"],
            )
        )
        out: dict[str, tuple[int, int, int, int]] = {}
        for name, total, routine, formula, constant in rows:
            key = _clean(name)
            if key:
                out[key] = (
                    int(total or 0),
                    int(routine or 0),
                    int(formula or 0),
                    int(constant or 0),
                )
        return out

    def _transfer_structures_with_start_routine(self) -> set[str]:
        if not self.capability.is_available("transfer_structure"):
            return set()
        rows = self.select(
            self.dialect.build_select(
                columns=["TRANSTRU"],
                from_logical="transfer_structure",
                where=["OBJVERS = 'A'", "STARTROUTINE <> ''"],
            )
        )
        return {c for c in (_clean(r[0]) for r in rows) if c}

    def _datasources_with_transformation(self) -> set[str]:
        """DataSources that also feed a 7.x transformation, so the two eras can be told apart."""
        if not self.capability.is_available("transformation"):
            return set()
        rows = self.select(
            self.dialect.build_select(
                columns=["SOURCENAME"],
                from_logical="transformation",
                where=["OBJVERS = 'A'", "SOURCETYPE = 'RSDS'"],
                group_by=["SOURCENAME"],
            )
        )
        return {c for c in (_clean(r[0]) for r in rows) if c}

    def _update_targets_by_infosource(self) -> dict[str, set[str]]:
        if not self.capability.is_available("update_rule"):
            return {}
        rows = self.select(
            self.dialect.build_select(
                columns=["ISOURCE", "INFOCUBE"],
                from_logical="update_rule",
                where=["OBJVERS = 'A'"],
            )
        )
        out: dict[str, set[str]] = {}
        for isource, target in rows:
            key, value = _clean(isource), _clean(target)
            if key and value:
                out.setdefault(key, set()).add(value)
        return out

    def _active_update_rule_count(self) -> int:
        if not self.capability.is_available("update_rule"):
            return 0
        base = self.dialect.build_select(
            columns=["UPDID"], from_logical="update_rule", where=["OBJVERS = 'A'"]
        )
        rows = self.select(self.dialect.count_query(base))
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _update_routine_counts(self) -> dict[str, int]:
        if not self.capability.is_available("update_rule_routine"):
            return {}
        rows = self.select(
            self.dialect.build_select(
                columns=["UPDID", "COUNT(*)"],
                from_logical="update_rule_routine",
                where=["OBJVERS = 'A'"],
                group_by=["UPDID"],
            )
        )
        out: dict[str, int] = {}
        for updid, count in rows:
            key = _clean(updid)
            if key:
                out[key] = int(count or 0)
        return out
