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

from typing import Any

from ..models.provenance import UnsupportedResult
from ..models.threex import ThreeXFlow, ThreeXFlowReport, TransferRule, UpdateRule
from .base import Repository

# A transfer structure can carry thousands of field rules; this bounds one structure's rule read.
_MAX_RULES = 2000
# Bound on the update-rule scan. Only 21 are active on the reference system, so this is generous.
_MAX_UPDATE_RULES = 1000


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
        return [
            UpdateRule(
                update_id=str(row[0]).strip(),
                infosource=_clean(row[1]),
                target=_clean(row[2]),
                has_start_routine=bool(_clean(row[3])),
                expert_mode=bool(_clean(row[4])),
                object_status=_clean(row[5]),
                routine_count=routine_counts.get(str(row[0]).strip(), 0),
                provenance=self.provenance(
                    "update_rule", {"UPDID": str(row[0]).strip(), "OBJVERS": "A"}
                ),
            )
            for row in rows
        ]

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
