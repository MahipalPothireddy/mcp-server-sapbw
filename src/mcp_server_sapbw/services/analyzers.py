"""Risk-scenario analyzers (B9, mission Section 9).

One analyzer per scenario (9.1-9.8) plus the layer-violation finder, each returning a
:class:`ScenarioReport` of :class:`Finding` objects (severity, affected objects, evidence,
recommendation). The analyzers *compose* the existing repositories/services — transformations and
routine parsing (B5), lineage (B6), queries (B7), the HANA layer (B8), and chains/runtimes (B3) —
and add the shared latency math (``services.latency``).

Design constraints honoured here:

* **Structural detection, not naming.** The mission's "ADM"/"EDW" layer names are the customer's
  labels for structurally detectable roles (full-update loads, CompositeProvider->DSO edges, merged
  multi-stream DSOs, deep DSO stacks). Nothing is inferred from a technical name (Rule 2).
* **Bounded.** Every analyzer caps how many candidate objects it examines and reports ``truncated``
  so an on-demand call never scans an entire large system.
* **Honest gaps.** Where BW alone cannot answer (object -> loading-chain frequency mapping is not
  cleanly derivable on this landscape; ECC/Tableau/BOBJ metadata lives outside BW), the analyzer
  records a caveat or an ``unpopulated_reason`` rather than guessing (Rules 2/3).
"""

from __future__ import annotations

from typing import Any

from ..connectors.base import ConnectorRegistry
from ..models.findings import Finding, ScenarioReport, Severity
from ..models.provenance import Provenance, UnsupportedResult
from ..repositories.base import Repository
from ..repositories.chains import ChainsRepository
from ..repositories.hana import HanaRepository
from ..repositories.transformations import TransformationsRepository
from . import latency

# RSTLOGO endpoint type codes (verified live, B5).
_CP = "HCPR"  # CompositeProvider
_IOBJ = "IOBJ"  # InfoObject
_DS = "RSDS"  # DataSource
_DSO_TYPES = ("ODSO", "ADSO")
_DSO_IN = "TARGETTYPE IN ('ODSO', 'ADSO')"
_DSO_SRC_IN = "SOURCETYPE IN ('ODSO', 'ADSO')"

SCENARIO_TITLES: dict[str, str] = {
    "9.1": "Full-update loads with routine lookups on less-frequently-refreshed objects",
    "9.2": "Deep DSO -> calc view -> CompositeProvider -> query layer stacks",
    "9.3": "CompositeProviders feeding DSOs (silent activation-order dependency)",
    "9.4": "InfoObjects (master data) loaded from CompositeProviders",
    "9.5": "Merged multi-stream DSOs (key-collision and semantic risk)",
    "9.6": "ECC extractor enhancements",
    "9.7": "Downstream report schedules vs. feeding-chain completion",
    "9.8": "Dashboards reading calc views directly (bypassing BW)",
    "layer_violations": "Structural layer violations (CP->DSO, CP->InfoObject, deep DSO stacks)",
}

_SCAN_CAP = 5000  # hard cap on rows pulled for bulk edge scans
_MERGED_INBOUND_CAP = 20  # inbound transformations examined per merged DSO for the field matrix
_DEEP_STACK_MIN = 2  # >= this many DSO->DSO hops (3+ layers) is the 9.2 "deep stack" shape
_HIGH_STACK_DEPTH = 3  # >= this many hops escalates a deep-stack finding to high severity
# 9.1 parses routine source per candidate (RSAABAP), so scanning is budgeted rather than unbounded:
# candidates without a resolvable lookup are skipped, and the scan stops at this many parses.
_LATENCY_PARSE_BUDGET = 250
_MANY_LOOKUPS = 3  # >= this many looked-up objects escalates a 9.1 finding to high severity


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class Analyzers(Repository):
    """The eight risk-scenario analyzers plus the layer-violation finder."""

    def __init__(
        self, connection: Any, capability: Any, cache: Any = None, registry: Any = None
    ) -> None:
        super().__init__(connection, capability, cache)
        self._transformations = TransformationsRepository(connection, capability, cache)
        self._chains = ChainsRepository(connection, capability, cache)
        self._hana = HanaRepository(connection, capability, cache)
        self._registry: ConnectorRegistry = registry or ConnectorRegistry()

    # --- dispatch ------------------------------------------------------------------------

    def run_scenario(self, scenario: str, *, limit: int = 50) -> ScenarioReport | UnsupportedResult:
        """Run one scenario by id ("9.1".."9.8" or "layer_violations")."""
        dispatch = {
            "9.1": self.check_load_latency,
            "9.2": self.deep_layer_stacks,
            "9.3": self.composite_provider_to_dso,
            "9.4": self.infoobject_from_composite_provider,
            "9.5": self.merged_stream_dso,
            "9.6": self.extractor_enhancements,
            "9.7": self.schedule_risk,
            "9.8": self.dashboards_on_calc_views,
            "layer_violations": self.find_layer_violations,
        }
        func = dispatch.get(scenario)
        if func is None:
            return UnsupportedResult(
                missing=[scenario],
                release=self.capability.bw_release,
                detail=f"unknown scenario '{scenario}'; valid: {', '.join(sorted(dispatch))}",
            )
        return func(limit=limit)

    # --- shared query helpers ------------------------------------------------------------

    def _fetch_transform(
        self,
        columns: list[str],
        where: list[str],
        params: list[Any],
        *,
        limit: int,
        order_by: list[str] | None = None,
        group_by: list[str] | None = None,
    ) -> list[tuple[Any, ...]]:
        base = self.dialect.build_select(
            columns=columns,
            from_logical="transformation",
            where=where,
            params=params,
            group_by=group_by,
            order_by=order_by,
        )
        return self.select(self.dialect.paginate(base, limit=limit, offset=0))

    def _edge_report(
        self,
        scenario: str,
        source_type: str,
        target_clause: str,
        *,
        severity: Severity,
        title: str,
        recommendation: str,
        detail: str,
        target_kind: str,
        limit: int,
        caveats: list[str],
        extra_params: list[str] | None = None,
    ) -> ScenarioReport:
        """Shared builder for the simple 'source_type -> target' edge scenarios (9.3, 9.4)."""
        rows = self._fetch_transform(
            ["TRANID", "SOURCENAME", "TARGETNAME"],
            ["SOURCETYPE = ?", target_clause],
            [source_type, *(extra_params or [])],
            limit=limit + 1,
            order_by=["TARGETNAME", "TRANID"],
        )
        truncated = len(rows) > limit
        findings: list[Finding] = []
        for tranid, src, tgt in rows[:limit]:
            src_name, tgt_name = _clean(src), _clean(tgt)
            if src_name is None or tgt_name is None:
                continue
            findings.append(
                Finding(
                    scenario=scenario,
                    severity=severity,
                    title=title,
                    affected_objects=[src_name, tgt_name],
                    evidence=[
                        self.provenance("transformation", {"TRANID": str(tranid), "OBJVERS": "A"})
                    ],
                    recommendation=recommendation,
                    detail=detail,
                    metrics={
                        "tran_id": str(tranid),
                        "source_compositeprovider": src_name,
                        "target": tgt_name,
                        "target_kind": target_kind,
                    },
                )
            )
        return ScenarioReport(
            scenario=scenario,
            title=SCENARIO_TITLES[scenario],
            findings=findings,
            analyzed_count=len(rows[:limit]),
            truncated=truncated,
            caveats=caveats,
        )

    # --- 9.3 CompositeProvider -> DSO ----------------------------------------------------

    def composite_provider_to_dso(self, *, limit: int = 50) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        return self._edge_report(
            "9.3",
            _CP,
            _DSO_IN,
            severity="medium",
            title="CompositeProvider feeds a DSO",
            recommendation=(
                "Treat the calc view behind the CompositeProvider as part of this DSO's load "
                "contract: a calc-view change silently changes DSO content on the next activation "
                "with no BW where-used warning. Re-validate and re-activate the DSO load after any "
                "calc-view change, and pin the calc-view version in the change process."
            ),
            detail=(
                "Calculations may occur in the calc view (HANA) and/or the BW transformation; use "
                "bw_get_calc_view_lineage on the CompositeProvider's calc view and "
                "bw_get_transformation on this TRANID to see the split."
            ),
            target_kind="dso",
            limit=limit,
            caveats=[
                "Calc-view-vs-transformation calculation split is advisory; inspect the calc view "
                "and transformation for the exact division of logic.",
            ],
        )

    # --- 9.4 InfoObject <- CompositeProvider ---------------------------------------------

    def infoobject_from_composite_provider(
        self, *, limit: int = 50
    ) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        return self._edge_report(
            "9.4",
            _CP,
            "TARGETTYPE = ?",
            severity="medium",
            title="InfoObject (master data) loaded from a CompositeProvider",
            recommendation=(
                "Ensure this master-data load completes before any transaction load that reads the "
                "InfoObject: master data must be current first. Schedule the InfoObject load "
                "upstream of dependent transaction chains (prefer event-based triggering)."
            ),
            detail=(
                "Master-data-before-transaction sequencing is required. Which chains currently "
                "violate it cannot be confirmed from BW metadata alone (see caveat)."
            ),
            target_kind="infoobject",
            limit=limit,
            caveats=[
                "Sequencing violations by chain are not derivable: on this landscape RSPCVARIANT "
                "carries no DTP_LOAD linkage, so object -> loading-chain mapping is unavailable. "
                "Verify load order manually against the chains that load these objects.",
            ],
            extra_params=[_IOBJ],
        )

    # --- 9.5 merged multi-stream DSO -----------------------------------------------------

    def merged_stream_dso(
        self, *, limit: int = 25, min_streams: int = 2
    ) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        rows = self._fetch_transform(
            ["TARGETNAME", "COUNT(DISTINCT SOURCENAME) AS STREAMS"],
            [_DSO_IN],
            [],
            limit=_SCAN_CAP,
            group_by=["TARGETNAME"],
        )
        merged = sorted(
            ((str(t).strip(), int(n)) for t, n in rows if _clean(t) and int(n) >= min_streams),
            key=lambda pair: pair[1],
            reverse=True,
        )
        truncated = len(merged) > limit
        findings = [self._merged_finding(tgt, streams) for tgt, streams in merged[:limit]]
        return ScenarioReport(
            scenario="9.5",
            title=SCENARIO_TITLES["9.5"],
            findings=findings,
            analyzed_count=len(merged[:limit]),
            truncated=truncated,
            caveats=[
                "Semantic differences the merge hides (e.g. an order vs. a shipment sharing a key) "
                "are business-level and must be reviewed by a data modeler; this analyzer surfaces "
                "the structural collision risk only.",
                f"Field matrix examines up to {_MERGED_INBOUND_CAP} inbound transformations/DSO.",
            ],
        )

    def _merged_finding(self, target: str, stream_count: int) -> Finding:
        inbound = self._fetch_transform(
            ["TRANID", "SOURCENAME"],
            ["TARGETNAME = ?", _DSO_IN],
            [target],
            limit=_MERGED_INBOUND_CAP,
            order_by=["SOURCENAME", "TRANID"],
        )
        streams = [(str(tr), str(sn)) for tr, sn in inbound if _clean(sn)]
        field_sources, collisions = self._merged_field_matrix(streams)
        severity: Severity = "high" if collisions else "medium"
        evidence = [
            self.provenance("transformation", {"TRANID": tr, "OBJVERS": "A"}) for tr, _ in streams
        ]
        return Finding(
            scenario="9.5",
            severity=severity,
            title="DSO merges multiple source streams into one target",
            affected_objects=[target, *[sn for _, sn in streams]],
            evidence=evidence,
            recommendation=(
                "Confirm the merged key is unique across every contributing stream: fields written "
                "by more than one stream can collide or overwrite. Document each stream's semantic "
                "grain and verify the key mapping per stream (bw_get_transformation per TRANID)."
            ),
            detail=(
                f"{stream_count} distinct source streams feed this DSO; "
                f"{len(collisions)} target field(s) are populated by more than one stream."
            ),
            metrics={
                "target_dso": target,
                "stream_count": stream_count,
                "streams": [sn for _, sn in streams],
                "collision_fields": collisions,
                "field_matrix": field_sources,
            },
        )

    def _merged_field_matrix(
        self, streams: list[tuple[str, str]]
    ) -> tuple[dict[str, list[str]], list[str]]:
        """Map each target field to the streams populating it; return (matrix, collision fields)."""
        field_to_streams: dict[str, set[str]] = {}
        for tran_id, source_name in streams:
            transformation = self._transformations.get_transformation(tran_id)
            if isinstance(transformation, UnsupportedResult):
                continue
            for mapping in transformation.field_mappings:
                for field_name in mapping.target_fields:
                    key = field_name.strip().upper()
                    if key:
                        field_to_streams.setdefault(key, set()).add(source_name)
        matrix = {field: sorted(sources) for field, sources in field_to_streams.items()}
        collisions = sorted(
            field for field, sources in field_to_streams.items() if len(sources) > 1
        )
        return matrix, collisions

    # --- 9.1 full-update loads with routine lookups --------------------------------------

    def check_load_latency(self, *, limit: int = 25) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("dtp", "transformation", "routine_source")
        if unsupported is not None:
            return unsupported
        full_targets = self._full_update_targets()
        candidates = [
            (tran_id, target)
            for tran_id, target in self._routine_transformations_targeting_dsos()
            if target in full_targets
        ]

        # A full-update load whose routines resolve no lookups carries no latency contract to
        # check, so it is not a finding. Keep scanning past those (within a parse budget) instead
        # of letting them consume the page, and report how many were suppressed rather than
        # silently dropping them.
        findings: list[Finding] = []
        evaluated = 0
        no_lookup_count = 0
        for tran_id, target in candidates:
            if len(findings) >= limit or evaluated >= _LATENCY_PARSE_BUDGET:
                break
            evaluated += 1
            finding = self._latency_finding(tran_id, target)
            if finding is None:
                no_lookup_count += 1
                continue
            findings.append(finding)

        truncated = evaluated < len(candidates)
        caveats = [
            "Routine table dependencies are a heuristic lower bound (dynamic SQL / FM / method "
            "calls are not followed).",
            "The looked-up object's refresh frequency vs. this load's run frequency cannot be "
            "auto-verified: object -> loading-chain mapping is not derivable on this landscape "
            "(RSPCVARIANT has no DTP_LOAD linkage). Verify cadence manually per looked-up "
            "object; the risk is that a load running more than once daily enriches new data "
            "against master data refreshed less often.",
        ]
        if no_lookup_count:
            caveats.append(
                f"{no_lookup_count} of {evaluated} evaluated full-update loads have routines whose "
                "reads did not resolve to a BW object; they carry no checkable latency contract "
                "and are excluded from the findings (parser lower bound, not proof of no lookup)."
            )
        if truncated:
            caveats.append(
                f"{evaluated} of {len(candidates)} full-update candidates were parsed "
                f"(page limit {limit}, parse budget {_LATENCY_PARSE_BUDGET}); "
                "raise limit or page through for the remainder."
            )
        return ScenarioReport(
            scenario="9.1",
            title=SCENARIO_TITLES["9.1"],
            findings=findings,
            analyzed_count=evaluated,
            truncated=truncated,
            caveats=caveats,
        )

    def _full_update_targets(self) -> set[str]:
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DISTINCT TGT"],
                    from_logical="dtp",
                    where=["UPDMODE = ?", "OBJVERS = 'A'"],  # RSBK* -> no auto OBJVERS
                    params=["F"],
                ),
                limit=_SCAN_CAP,
            )
        )
        return {str(r[0]).strip() for r in rows if _clean(r[0])}

    def _routine_transformations_targeting_dsos(self) -> list[tuple[str, str]]:
        rows = self._fetch_transform(
            ["TRANID", "TARGETNAME"],
            ["(STARTROUTINE <> '' OR ENDROUTINE <> '' OR EXPERT <> '')", _DSO_IN],
            [],
            limit=_SCAN_CAP,
            order_by=["TARGETNAME", "TRANID"],
        )
        return [(str(tr), str(tgt).strip()) for tr, tgt in rows if _clean(tgt)]

    def _latency_finding(self, tran_id: str, target: str) -> Finding | None:
        """A 9.1 finding, or ``None`` when the routines resolve no lookup to check."""
        looked_up: list[str] = []
        evidence: list[Provenance] = [
            self.provenance("transformation", {"TRANID": tran_id, "OBJVERS": "A"})
        ]
        analyses = self._transformations.analyze_routines(tran_id)
        if not isinstance(analyses, UnsupportedResult):
            for analysis in analyses:
                evidence.append(
                    self.provenance("routine_source", {"CODEID": analysis.code_id, "OBJVERS": "A"})
                )
                for dep in analysis.table_dependencies:
                    obj = dep.resolved_object
                    if obj and obj != target and obj not in looked_up:
                        looked_up.append(obj)
        # Declared lookups (typed rule-step tables) are exact: BW records them itself. They are
        # kept separate from the routine-parsed ones so a finding never blurs a recorded
        # dependency with an inferred one.
        declared: list[str] = []
        declared_detail: list[dict[str, Any]] = []
        for lookup in self._transformations.declared_lookups(tran_id):
            evidence.append(lookup.provenance)
            if lookup.object_name != target and lookup.object_name not in declared:
                declared.append(lookup.object_name)
            declared_detail.append(
                {
                    "object": lookup.object_name,
                    "kind": lookup.kind,
                    "key_date": lookup.key_date,
                    "miss_behaviour": lookup.miss_behaviour,
                }
            )
        if not looked_up and not declared:
            return None  # no resolvable lookup -> no latency contract to evaluate
        all_lookups = declared + [obj for obj in looked_up if obj not in declared]
        delta = self._extractor_constraint(target)
        # A lookup that substitutes a constant on a miss changes data silently instead of failing.
        silent_miss = [
            item["object"] for item in declared_detail if item["miss_behaviour"] == "constant"
        ]
        return Finding(
            scenario="9.1",
            severity="high" if len(all_lookups) >= _MANY_LOOKUPS else "medium",
            title="Full-update load reads other objects while loading",
            affected_objects=[target, *all_lookups],
            evidence=evidence,
            recommendation=(
                "Verify each looked-up object is refreshed at least as often as this full-update "
                "load runs. If the load runs more than once daily and a looked-up object refreshes "
                "once daily, the later run enriches new data against stale data. Prefer delta "
                "loading or event-based sequencing after the looked-up object completes."
                + (
                    " Lookups that substitute a constant when no record is found will not fail the "
                    "load - they change the data silently, so a stale or missing row is invisible."
                    if silent_miss
                    else ""
                )
            ),
            detail=(
                f"Full-update load (UPDMODE='F') with {len(declared)} declared lookup(s) "
                f"(exact, from BW's rule-step tables) and {len(looked_up)} routine-parsed "
                "read(s) (heuristic)."
                + (f" Source extractor delta method: {delta}." if delta else "")
            ),
            metrics={
                "target": target,
                "tran_id": tran_id,
                "declared_lookups": declared,
                "declared_lookup_detail": declared_detail,
                "routine_lookup_objects": looked_up,
                "looked_up_objects": all_lookups,
                "silent_miss_lookups": silent_miss,
                "update_mode": "F",
                "extractor_delta_method": delta,
            },
        )

    def _extractor_constraint(self, target: str) -> str | None:
        """Best-effort delta method of the DataSource feeding a full-update target (9.1 case)."""
        if not self.capability.is_available("extractor"):
            return None
        source = self._full_update_source(target)
        if source is None:
            return None
        try:
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=["DELTA"],
                        from_logical="extractor",
                        where=["OLTPSOURCE = ?", "OBJVERS = 'A'"],  # ROO* -> no auto OBJVERS
                        params=[source],
                    ),
                    limit=1,
                )
            )
        except Exception:
            return None  # extractor column names vary by release; degrade to "unknown"
        return _clean(rows[0][0]) if rows else None

    def _full_update_source(self, target: str) -> str | None:
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["SRC", "SRCTLOGO"],
                    from_logical="dtp",
                    where=["TGT = ?", "UPDMODE = ?", "OBJVERS = 'A'"],
                    params=[target, "F"],
                ),
                limit=1,
            )
        )
        if not rows:
            return None
        src, src_type = _clean(rows[0][0]), _clean(rows[0][1])
        return src if src_type == _DS else None

    # --- 9.2 deep DSO -> calc view -> CP -> query stacks ---------------------------------

    def deep_layer_stacks(self, *, limit: int = 25) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        edges = self._dso_edges()
        roots = self._stack_roots(edges)
        findings: list[Finding] = []
        for root in roots:
            depth, path = self._longest_downstream(root, edges)
            if depth >= _DEEP_STACK_MIN:  # 3+ DSO layers in a straight stack
                findings.append(self._stack_finding(path, depth))
            if len(findings) >= limit:
                break
        return ScenarioReport(
            scenario="9.2",
            title=SCENARIO_TITLES["9.2"],
            findings=findings,
            analyzed_count=len(roots),
            truncated=len(findings) >= limit,
            caveats=[
                "The stack is traced through declared DSO->DSO transformations; the calc-view and "
                "CompositeProvider hops above the top DSO are visible via bw_get_calc_view_lineage "
                "and the query tools. Cumulative latency needs runtime stats per feeding chain "
                "(bw_get_chain_runtimes) and is not summed here.",
            ],
        )

    def _stack_finding(self, path: list[str], depth: int) -> Finding:
        runbook = [
            f"{i + 1}. inspect DSO {name} (bw_describe_object)" for i, name in enumerate(path)
        ]
        runbook.append(
            f"{len(path) + 1}. inspect the calc view / CompositeProvider / query above the top DSO"
        )
        return Finding(
            scenario="9.2",
            severity="high" if depth >= _HIGH_STACK_DEPTH else "medium",
            title=f"Deep {depth + 1}-layer DSO stack",
            affected_objects=list(path),
            evidence=[self.provenance("transformation", {"SOURCENAME": path[0], "OBJVERS": "A"})],
            recommendation=(
                "Long DSO stacks multiply load latency and incident-triage effort. Review whether "
                "intermediate layers can be collapsed, and follow the per-layer runbook top-down "
                "when data looks wrong."
            ),
            detail=f"DSO->DSO chain of depth {depth} ({depth + 1} DSO layers).",
            metrics={"depth": depth, "layers": depth + 1, "path": path, "runbook": runbook},
        )

    def _dso_edges(self) -> dict[str, list[str]]:
        rows = self._fetch_transform(
            ["SOURCENAME", "TARGETNAME"],
            [_DSO_SRC_IN, _DSO_IN],
            [],
            limit=_SCAN_CAP,
            order_by=["SOURCENAME"],
        )
        adjacency: dict[str, list[str]] = {}
        for src, tgt in rows:
            s, t = _clean(src), _clean(tgt)
            if s and t and s != t:
                adjacency.setdefault(s, []).append(t)
        return adjacency

    @staticmethod
    def _stack_roots(edges: dict[str, list[str]]) -> list[str]:
        targets = {t for tgts in edges.values() for t in tgts}
        return sorted(node for node in edges if node not in targets)

    @staticmethod
    def _longest_downstream(root: str, edges: dict[str, list[str]]) -> tuple[int, list[str]]:
        best: tuple[int, list[str]] = (0, [root])

        def walk(node: str, path: list[str], visiting: set[str]) -> None:
            nonlocal best
            extended = False
            for nxt in edges.get(node, []):
                if nxt in visiting:
                    continue  # cycle guard
                extended = True
                walk(nxt, [*path, nxt], visiting | {nxt})
            if not extended and len(path) - 1 > best[0]:
                best = (len(path) - 1, path)

        walk(root, [root], {root})
        return best

    # --- 9.6 ECC extractor enhancements (BW heuristic + connector-gated) -----------------

    def extractor_enhancements(self, *, limit: int = 50) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("datasource_field")
        if unsupported is not None:
            return unsupported
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["DATASOURCE", "COUNT(*) AS FIELD_COUNT"],
                    from_logical="datasource_field",
                    where=["(FIELDNM LIKE 'Z%' OR FIELDNM LIKE 'Y%')"],
                    group_by=["DATASOURCE"],
                ),
                limit=_SCAN_CAP,
            )
        )
        enhanced = sorted(
            ((str(ds).strip(), int(n)) for ds, n in rows if _clean(ds)),
            key=lambda pair: pair[1],
            reverse=True,
        )
        truncated = len(enhanced) > limit
        reason = self._registry.unpopulated_reason("ecc")
        findings = [
            self._enhancement_finding(datasource, field_count, reason)
            for datasource, field_count in enhanced[:limit]
        ]
        return ScenarioReport(
            scenario="9.6",
            title=SCENARIO_TITLES["9.6"],
            findings=findings,
            analyzed_count=len(enhanced[:limit]),
            truncated=truncated,
            connector_required="ECC" if reason else None,
            caveats=[
                "HEURISTIC: customer-namespace (Z*/Y*) fields in the DataSource replica indicate "
                "an enhancement likely exists; they do not reveal what the exit code does, which "
                "tables it reads, or whether it performs per-record SELECTs.",
                "The enhancement logic lives in the ECC source system (ABAP, unreachable over the "
                "BW HANA connection); populate it via an ECC connector or a source bundle.",
            ],
        )

    def _enhancement_finding(
        self, datasource: str, field_count: int, reason: str | None
    ) -> Finding:
        return Finding(
            scenario="9.6",
            severity="low",
            title="DataSource likely carries an extractor enhancement (heuristic)",
            affected_objects=[datasource],
            evidence=[
                self.provenance("datasource_field", {"DATASOURCE": datasource, "OBJVERS": "A"})
            ],
            recommendation=(
                "Review the ECC extractor exit for this DataSource: confirm which tables it reads "
                "(cross-team coordination risk) and whether it does per-record SELECTs "
                "(performance risk). Provide an ECC connector or exported exit source to analyze."
            ),
            detail=f"{field_count} customer-namespace (Z*/Y*) field(s) in the DataSource replica.",
            metrics={"datasource": datasource, "zy_field_count": field_count},
            unpopulated_reason=reason,
        )

    # --- 9.7 report schedules vs. chain completion (connector-gated) ---------------------

    def schedule_risk(self, *, limit: int = 100) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("chain_attr", "log_chain")
        if unsupported is not None:
            return unsupported
        matrix = self._chains.get_schedule_matrix(limit=limit)
        chains_with_p95 = 0
        if not isinstance(matrix, UnsupportedResult):
            entries, _ = matrix
            chains_with_p95 = sum(1 for e in entries if e.p95_completion is not None)
        reason = self._registry.unpopulated_reason("tableau")
        finding = Finding(
            scenario="9.7",
            severity="info",
            title="Report-side schedules require an external BI connector",
            affected_objects=[],
            evidence=[],
            recommendation=(
                "Configure a Tableau or BOBJ connector to compare each report/extract's scheduled "
                "start against the p95 completion of the chain feeding its provider. Flag negative "
                "or sub-30-minute margins and switch those reports to event-based triggering."
            ),
            detail=(
                f"BW-side feeding-chain p95 completion is available for {chains_with_p95} chain(s);"
                " report/extract schedules needed to compute safety margins are not in BW."
            ),
            metrics={
                "chains_with_p95_completion": chains_with_p95,
                "min_safe_margin_minutes": latency.MIN_SAFE_MARGIN_MINUTES,
            },
            unpopulated_reason=reason,
        )
        return ScenarioReport(
            scenario="9.7",
            title=SCENARIO_TITLES["9.7"],
            findings=[finding],
            analyzed_count=chains_with_p95,
            connector_required="Tableau/BOBJ" if reason else None,
            caveats=[
                "Safety-margin math is implemented (services.latency) and will populate per report "
                "once an external BI connector supplies report start times.",
            ],
        )

    # --- 9.8 dashboards on calc views (connector-gated) ----------------------------------

    def dashboards_on_calc_views(self, *, limit: int = 100) -> ScenarioReport | UnsupportedResult:
        bw_consuming = self._bw_consuming_calc_view_count(limit)
        reason = self._registry.unpopulated_reason("tableau")
        finding = Finding(
            scenario="9.8",
            severity="info",
            title="Dashboard-to-calc-view bypass detection requires a Tableau connector",
            affected_objects=[],
            evidence=[],
            recommendation=(
                "Configure a Tableau connector to list dashboards reading calc views directly. For "
                "each, determine whether its calc view is the same one a CompositeProvider uses: a "
                "shared view means one change breaks both paths; separate views can silently "
                "diverge in numbers."
            ),
            detail=(
                f"{bw_consuming} BW-consuming calc view(s) exist that a dashboard could read "
                "directly, bypassing BW; matching them to dashboards needs Tableau metadata."
            ),
            metrics={"bw_consuming_calc_views": bw_consuming},
            unpopulated_reason=reason,
        )
        return ScenarioReport(
            scenario="9.8",
            title=SCENARIO_TITLES["9.8"],
            findings=[finding],
            analyzed_count=bw_consuming,
            connector_required="Tableau" if reason else None,
            caveats=[
                "Calc views that read BW tables are candidate bypass paths; confirming a dashboard "
                "actually reads one, and whether it is shared with the CompositeProvider path, "
                "needs Tableau lineage metadata (mission Known Limitation 2).",
            ],
        )

    def _bw_consuming_calc_view_count(self, limit: int) -> int:
        if not self.capability.is_available("object_dependencies"):
            return 0
        result = self._hana.list_calc_views(bw_consuming_only=True, limit=1, offset=0)
        if isinstance(result, UnsupportedResult):
            return 0
        _, total = result
        return total

    # --- layer violations ----------------------------------------------------------------

    def find_layer_violations(
        self, *, limit: int = 100, max_dso_depth: int = 3
    ) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported
        findings: list[Finding] = []
        findings += self._violation_edges(
            _CP,
            _DSO_IN,
            extra_params=[],
            label="CompositeProvider -> DSO",
            target_kind="dso",
            limit=limit,
        )
        findings += self._violation_edges(
            _CP,
            "TARGETTYPE = ?",
            extra_params=[_IOBJ],
            label="CompositeProvider -> InfoObject",
            target_kind="infoobject",
            limit=limit,
        )
        findings += self._deep_stack_violations(max_dso_depth, limit)
        return ScenarioReport(
            scenario="layer_violations",
            title=SCENARIO_TITLES["layer_violations"],
            findings=findings,
            analyzed_count=len(findings),
            truncated=len(findings) >= limit,
            caveats=[
                f"Deep-stack threshold is {max_dso_depth} DSO->DSO hops; adjust via max_dso_depth.",
            ],
        )

    def _violation_edges(
        self,
        source_type: str,
        target_clause: str,
        *,
        extra_params: list[str],
        label: str,
        target_kind: str,
        limit: int,
    ) -> list[Finding]:
        rows = self._fetch_transform(
            ["TRANID", "SOURCENAME", "TARGETNAME"],
            ["SOURCETYPE = ?", target_clause],
            [source_type, *extra_params],
            limit=limit,
            order_by=["TARGETNAME", "TRANID"],
        )
        findings: list[Finding] = []
        for tranid, src, tgt in rows:
            src_name, tgt_name = _clean(src), _clean(tgt)
            if src_name is None or tgt_name is None:
                continue
            findings.append(
                Finding(
                    scenario="layer_violation",
                    severity="medium",
                    title=f"Layer violation: {label}",
                    affected_objects=[src_name, tgt_name],
                    evidence=[
                        self.provenance("transformation", {"TRANID": str(tranid), "OBJVERS": "A"})
                    ],
                    recommendation=(
                        "A CompositeProvider is a virtual/consumption layer; feeding it back into "
                        "a persisted DSO or InfoObject inverts the intended layering. Review "
                        "whether the target should source from the underlying providers instead."
                    ),
                    detail=f"{label} transformation.",
                    metrics={"tran_id": str(tranid), "kind": label, "target_kind": target_kind},
                )
            )
        return findings

    def _deep_stack_violations(self, max_dso_depth: int, limit: int) -> list[Finding]:
        edges = self._dso_edges()
        findings: list[Finding] = []
        for root in self._stack_roots(edges):
            depth, path = self._longest_downstream(root, edges)
            if depth >= max_dso_depth:
                findings.append(
                    Finding(
                        scenario="layer_violation",
                        severity="high",
                        title=f"Layer violation: deep DSO stack ({depth} hops)",
                        affected_objects=list(path),
                        evidence=[
                            self.provenance(
                                "transformation", {"SOURCENAME": path[0], "OBJVERS": "A"}
                            )
                        ],
                        recommendation=(
                            "DSO stacks deeper than the configured threshold multiply latency and "
                            "maintenance cost. Review whether intermediate layers can be collapsed."
                        ),
                        detail=f"DSO->DSO chain of {depth} hops ({depth + 1} layers).",
                        metrics={"depth": depth, "path": path},
                    )
                )
            if len(findings) >= limit:
                break
        return findings
