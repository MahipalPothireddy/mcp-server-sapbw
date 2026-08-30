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
* **Honest gaps.** Where BW alone cannot answer (ECC/Tableau/BOBJ metadata lives outside BW; a
  provider with no resolvable loading chain has no knowable cadence), the analyzer records a caveat
  or an ``unpopulated_reason`` rather than guessing (Rules 2/3).
"""

from __future__ import annotations

from typing import Any

from ..connectors.base import ConnectorRegistry
from ..connectors.bi import BiConnector
from ..connectors.ecc import EccConnector
from ..models.chains import FrequencyClass, ScheduleMatrixEntry
from ..models.completeness import COMPLETE, Completeness, bounded
from ..models.ecc import ExitBranch, ExitInventory
from ..models.findings import Finding, ScenarioReport, Severity
from ..models.objects import BwObjectRef, normalise_object_type
from ..models.provenance import Provenance, UnsupportedResult
from ..repositories.base import Repository
from ..repositories.chains import ChainsRepository
from ..repositories.hana import HanaRepository
from ..repositories.providers import ProvidersRepository
from ..repositories.queries import QueriesRepository
from ..repositories.sources import SourcesRepository
from ..repositories.transformations import TransformationsRepository
from . import latency
from .exit_analysis import ExitAnalysisService
from .graph import ObjectGraph
from .load_closure import LoadClosureService, cadence_of

# RSTLOGO endpoint type codes (verified live, B5).
_CP = "HCPR"  # CompositeProvider
_IOBJ = "IOBJ"  # InfoObject
_DS = "RSDS"  # DataSource
_DSO_TYPES = ("ODSO", "ADSO")
_DSO_IN = "TARGETTYPE IN ('ODSO', 'ADSO')"
_DSO_SRC_IN = "SOURCETYPE IN ('ODSO', 'ADSO')"


def _capped(hit: bool, *, scope: str, limit: int) -> Completeness:
    """``Completeness`` for a scenario stopped by the caller's object cap (D6).

    Every analyzer bounds its candidate set the same way, so the bound is named in one place rather
    than repeated at ten call sites - which is also what stops the ten from drifting apart.
    """
    if not hit:
        return COMPLETE
    return bounded("row_cap", scope=scope, limit=limit)


def _orderable(columns: list[str]) -> list[str]:
    """The plain column names among ``columns``, usable in an ORDER BY.

    Skips aggregates and aliased expressions (``COUNT(DISTINCT X) AS Y``), which cannot be ordered
    on by name in an ungrouped query, and strips a leading ``DISTINCT``.
    """
    plain: list[str] = []
    for column in columns:
        candidate = column.strip()
        if candidate.upper().startswith("DISTINCT "):
            candidate = candidate[len("DISTINCT ") :].strip()
        if "(" in candidate or " " in candidate:
            continue
        plain.append(candidate)
    return plain


SCENARIO_TITLES: dict[str, str] = {
    "9.1": "Full-update loads with routine lookups on less-frequently-refreshed objects",
    "9.2": "Deep DSO -> calc view -> CompositeProvider -> query layer stacks",
    "9.3": "CompositeProviders feeding DSOs (silent activation-order dependency)",
    "9.4": "InfoObjects (master data) loaded from CompositeProviders",
    "9.5": "Merged multi-stream DSOs (key-collision and semantic risk)",
    "9.6": "ECC extractor enhancements",
    "9.7": "Downstream report schedules vs. feeding-chain completion",
    "9.8": "Dashboards reading calc views directly (bypassing BW)",
    "layer_violations": (
        "Structural layer violations (CP->DSO, CP->InfoObject, deep DSO stacks, "
        "circular dependencies)"
    ),
    "unused_providers": "Providers with no maintained consumer (decommission candidates)",
}

_SCAN_CAP = 5000  # hard cap on rows pulled for bulk edge scans
# A cycle of 40 objects would otherwise cite 40 transformations; a finding points at its evidence
# rather than reproducing all of it, and metrics.tran_ids carries the complete list.
_CYCLE_EVIDENCE_CAP = 6
_PAIR = 2  # a two-member cycle reads better as "each feeds the other" than "across 2 objects"
_MERGED_INBOUND_CAP = 20  # inbound transformations examined per merged DSO for the field matrix
_CADENCE_CHECK_CAP = 12  # looked-up objects whose cadence is resolved per finding
_MANY_ENH_FIELDS = 5  # appended fields at/above which an enhancement is substantial
_DEEP_STACK_MIN = 2  # >= this many DSO->DSO hops (3+ layers) is the 9.2 "deep stack" shape
_HIGH_STACK_DEPTH = 3  # >= this many hops escalates a deep-stack finding to high severity
# 9.1 parses routine source per candidate (RSAABAP), so scanning is budgeted rather than unbounded:
# candidates without a resolvable lookup are skipped, and the scan stops at this many parses.
_LATENCY_PARSE_BUDGET = 250
_MANY_LOOKUPS = 3  # >= this many looked-up objects escalates a 9.1 finding to high severity
# Consumer analysis scans. Queries are read once and bucketed by origin; CompositeProviders are
# resolved individually (their part list needs the calc-view route), so that scan is kept small.
_QUERY_CONSUMER_CAP = 5000
_COMPOSITE_SCAN_CAP = 500


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _exit_index(exits: Any) -> dict[str, list[Any]]:
    """``{datasource: [(code_id, branch), ...]}`` across every readable exit slot and satellite.

    Keyed on the branch rather than the slot so a finding's risk reflects only the code that runs
    for that DataSource. ``code_id`` is the include or satellite program name, carried for citation.

    A satellite program is folded in as a branch of its own. It is a stronger form of the same fact
    - one DataSource's exit logic, isolated - so the aggregation downstream needs no special case,
    and a DataSource whose include branch is empty because the logic was dispatched at runtime stops
    reading as "enhanced but does nothing".
    """
    index: dict[str, list[Any]] = {}
    if exits is None:
        return index
    for slot in exits.exits:
        if not slot.available:
            continue
        for branch in slot.branches:
            index.setdefault(branch.datasource, []).append((slot.include_name, branch))
    for satellite in getattr(exits, "satellites", []):
        if not satellite.available:
            continue
        index.setdefault(satellite.datasource, []).append(
            (
                satellite.program_name,
                ExitBranch(
                    datasource=satellite.datasource,
                    resolved=True,
                    line_count=satellite.line_count,
                    table_reads=satellite.table_reads,
                    per_record_selects=satellite.per_record_selects,
                    anti_pattern_kinds=satellite.anti_pattern_kinds,
                    unresolved_call_count=satellite.unresolved_call_count,
                ),
            )
        )
    return index


def _satellite_index(exits: Any) -> dict[str, list[Any]]:
    """``{datasource: [satellite, ...]}`` including the ones that turned out not to exist.

    A probe that came back ``absent`` is kept because it changes what can be said: "no satellite
    program exists for this DataSource" is a measurement, whereas omitting it would leave the reader
    unable to tell a checked absence from an unchecked one.
    """
    index: dict[str, list[Any]] = {}
    for satellite in getattr(exits, "satellites", []) or []:
        index.setdefault(satellite.datasource, []).append(satellite)
    return index


def _exit_risk(entries: list[Any]) -> tuple[list[str], int, bool]:
    """Aggregate a DataSource's branches: tables read, per-record SELECTs, every branch resolved."""
    tables: list[str] = []
    per_record = 0
    resolved = True
    for _include, branch in entries:
        if not branch.resolved:
            resolved = False
            continue
        tables.extend(table for table in branch.table_reads if table not in tables)
        per_record += branch.per_record_selects
    return tables, per_record, resolved


class Analyzers(Repository):
    """The eight risk-scenario analyzers plus the layer-violation finder."""

    def __init__(
        self, connection: Any, capability: Any, cache: Any = None, registry: Any = None
    ) -> None:
        super().__init__(connection, capability, cache)
        self._transformations = TransformationsRepository(connection, capability, cache)
        self._chains = ChainsRepository(connection, capability, cache)
        self._hana = HanaRepository(connection, capability, cache)
        self._closure = LoadClosureService(connection, capability, cache)
        self._sources = SourcesRepository(connection, capability, cache)
        self._providers = ProvidersRepository(connection, capability, cache)
        self._queries = QueriesRepository(connection, capability, cache)
        self._registry: ConnectorRegistry = registry or ConnectorRegistry()
        # Cadence lookups repeat across findings (many loads read the same master data).
        self._frequency_cache: dict[str, FrequencyClass] = {}

    # --- dispatch ------------------------------------------------------------------------

    def run_scenario(self, scenario: str, *, limit: int = 50) -> ScenarioReport | UnsupportedResult:
        """Run one analysis by id ("9.1".."9.8", "layer_violations", "unused_providers")."""
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
            "unused_providers": self.find_unused_providers,
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
        """One capped read of RSTRAN. Always ordered, whether or not the caller said so.

        Every call here is bounded by ``limit``, so an unordered read returns an arbitrary subset -
        and nine call sites feed scenario findings from it. Defaulting the order rather than fixing
        each caller means a *new* caller cannot reintroduce the defect by omitting it (D8).
        """
        if order_by is None:
            order_by = list(group_by) if group_by else _orderable(columns)
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
            completeness=_capped(truncated, scope="candidates", limit=limit),
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
                "Which chains violate the master-before-transaction order is not asserted here: "
                "use bw_get_load_closure on each object to compare the loading chains' observed "
                "cadence and position.",
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
            completeness=_capped(truncated, scope="candidates", limit=limit),
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
            "calls are not followed). Declared lookups are exact.",
            "Cadence is compared from observed run history (RSPCLOGCHAIN) through the chain -> "
            "provider closure. A provider whose loading chain cannot be resolved is reported as "
            "cadence unknown rather than assumed safe.",
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
            completeness=_capped(truncated, scope="candidates", limit=limit),
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
                    # Capped scan feeding scenario 9.1's candidate set: an arbitrary slice would
                    # report a different set of latency findings on each run (D8).
                    order_by=["TGT"],
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
        cadence = self._cadence_contract(target, all_lookups)
        # A lookup that substitutes a constant on a miss changes data silently instead of failing.
        silent_miss = [
            item["object"] for item in declared_detail if item["miss_behaviour"] == "constant"
        ]
        stale = cadence["stale_risk_objects"]
        severity: Severity = "medium"
        if stale:
            severity = "high"  # substantiated: consumer runs more often than its input refreshes
        elif len(all_lookups) >= _MANY_LOOKUPS:
            severity = "high"
        return Finding(
            scenario="9.1",
            severity=severity,
            title=(
                "Full-update load enriches against less-frequently-refreshed data"
                if stale
                else "Full-update load reads other objects while loading"
            ),
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
                **cadence,
            },
        )

    def _cadence_contract(self, target: str, lookups: list[str]) -> dict[str, Any]:
        """Compare the load's observed cadence against each looked-up object's.

        This is the latency contract the scenario is actually about: if the consuming load runs more
        often than an object it reads is refreshed, the later run enriches new data against stale
        data. Both cadences come from run history via the chain -> provider closure; an object whose
        loading chain cannot be resolved is reported ``unknown``, never assumed safe.
        """
        consumer = self._governing_frequency(target)
        stale: list[str] = []
        unknown: list[str] = []
        per_object: dict[str, str] = {}
        for name in lookups[:_CADENCE_CHECK_CAP]:
            frequency = self._governing_frequency(name)
            per_object[name] = frequency
            if frequency == "unknown" or consumer == "unknown":
                unknown.append(name)
            elif latency.is_stale_master_risk(consumer, frequency):
                stale.append(name)
        return {
            "consumer_frequency": consumer,
            "lookup_frequency": per_object,
            "stale_risk_objects": stale,
            "cadence_unknown_objects": unknown,
        }

    def _governing_frequency(self, provider: str) -> FrequencyClass:
        """Observed cadence of the most frequent chain loading ``provider`` (cached per run)."""
        if provider in self._frequency_cache:
            return self._frequency_cache[provider]
        frequency: FrequencyClass = "unknown"
        closure = self._closure.provider_to_chains(provider)
        if not isinstance(closure, UnsupportedResult):
            governing = cadence_of(closure.loading_chains)
            if governing is not None:
                frequency = governing.frequency
        self._frequency_cache[provider] = frequency
        return frequency

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
                        order_by=["DELTA"],  # one row taken from possibly many; see D8
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
                    # Several full DTPs can target one object, and this takes one of them to name
                    # the extractor: unordered, the finding cited an arbitrary source (D8).
                    order_by=["SRC"],
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
            completeness=_capped(len(findings) >= limit, scope="findings", limit=limit),
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

    # --- providers with no maintained consumer -------------------------------------------

    def find_unused_providers(self, *, limit: int = 100) -> ScenarioReport | UnsupportedResult:
        """Providers nothing maintained depends on: decommission candidates, stated as candidates.

        Three consumer routes are checked, and all three must come up empty:

        1. **Feeds a transformation** - the provider is some transformation's source.
        2. **Has a designed query** - a query authored in Query Designer reads it. Queries whose
           technical name marks them ad hoc are counted separately and do not qualify, since a
           throwaway navigation is not a maintained report.
        3. **Is a CompositeProvider part** - a CompositeProvider consumes its parts through a
           generated calc view, not a transformation. Omitting this route would report every DSO
           under a CompositeProvider as unused, which is the single most likely way this analysis
           could be wrong.

        What it still cannot see is a consumer outside BW reading the generated table directly.
        ``bw_get_hana_crossings`` covers that, and the caveat says so. Findings are therefore
        ``low`` severity and worded as candidates to confirm, never as safe-to-delete.
        """
        unsupported = self.require("transformation")
        if unsupported is not None:
            return unsupported

        catalog = self._providers.provider_catalog()
        candidates: list[tuple[str, str]] = [
            (name, kind) for kind, names in sorted(catalog.items()) for name in names
        ]
        if not candidates:
            return ScenarioReport(
                scenario="unused_providers",
                title=SCENARIO_TITLES["unused_providers"],
                findings=[],
                analyzed_count=0,
                caveats=[
                    "No provider catalogue is available on this release, so no provider could be "
                    "examined. This is not evidence that every provider is used."
                ],
            )

        sources = self._transformation_source_names()
        designed, ad_hoc = self._query_consumer_names()
        parts = self._composite_part_names()

        findings: list[Finding] = []
        for name, kind in candidates:
            if name in sources or name in designed or name in parts:
                continue
            findings.append(self._unused_finding(name, kind, ad_hoc.get(name, 0)))
            if len(findings) >= limit:
                break
        return ScenarioReport(
            scenario="unused_providers",
            title=SCENARIO_TITLES["unused_providers"],
            findings=findings,
            analyzed_count=len(candidates),
            completeness=_capped(len(findings) >= limit, scope="findings", limit=limit),
            caveats=[
                "A provider is reported only when it feeds no transformation, has no "
                "Query-Designer query, and is no CompositeProvider part. These are candidates to "
                "confirm, not objects proven safe to delete.",
                "Consumption from outside BW - a calculation view or reporting tool reading the "
                "generated table directly - is not covered here. Check bw_get_hana_crossings "
                "before acting on any of these.",
                "Ad-hoc queries (technical name prefixed '!!') are reported per provider but do "
                "not count as maintained consumers; that classification reads the name shape, as "
                "BW stores no flag for it.",
                f"{len(designed)} provider(s) have a designed query, {len(parts)} are "
                f"CompositeProvider parts, and {len(sources)} feed a transformation.",
            ],
        )

    def _unused_finding(self, name: str, kind: str, ad_hoc_count: int) -> Finding:
        detail = (
            "No transformation reads this provider, no Query-Designer query reports on it, and it "
            "is not a CompositeProvider part."
        )
        recommendation = (
            "Confirm nothing outside BW reads the generated table, then retire the provider and "
            "the chain steps that load it. A provider still being loaded but never read costs "
            "runtime and memory on every run."
        )
        if ad_hoc_count:
            detail += (
                f" {ad_hoc_count} ad-hoc quer(y/ies) exist against it, so someone has been looking "
                "at the data directly without a maintained report."
            )
            recommendation = (
                f"Before retiring this, find out who runs the {ad_hoc_count} ad-hoc quer(y/ies) "
                "against it: an ad-hoc query is often a real reporting need that never got built "
                "properly. Then confirm nothing outside BW reads the generated table."
            )
        return Finding(
            scenario="unused_providers",
            severity="low",
            title="Provider has no maintained consumer",
            affected_objects=[name],
            evidence=[self.provenance("transformation", {"SOURCENAME": name, "OBJVERS": "A"})],
            recommendation=recommendation,
            detail=detail,
            metrics={
                "provider": name,
                "object_type": kind,
                "feeds_transformation": False,
                "has_designed_query": False,
                "is_composite_part": False,
                "ad_hoc_query_count": ad_hoc_count,
            },
        )

    def _transformation_source_names(self) -> set[str]:
        rows = self._fetch_transform(
            ["SOURCENAME"],
            ["SOURCENAME <> ''"],
            [],
            limit=_SCAN_CAP,
            group_by=["SOURCENAME"],
            order_by=["SOURCENAME"],
        )
        return {name for row in rows if (name := _clean(row[0]))}

    def _query_consumer_names(self) -> tuple[set[str], dict[str, int]]:
        """``(providers with a designed query, {provider: ad-hoc query count})``."""
        if not self.capability.is_available("query_provider") or not self.capability.is_available(
            "query_dir"
        ):
            return set(), {}
        queries = self._queries.list_queries(limit=_QUERY_CONSUMER_CAP)
        if isinstance(queries, UnsupportedResult):
            return set(), {}
        summaries, _total = queries
        designed: set[str] = set()
        ad_hoc: dict[str, int] = {}
        for summary in summaries:
            provider = summary.provider
            if not provider:
                continue
            if summary.origin == "ad_hoc":
                ad_hoc[provider] = ad_hoc.get(provider, 0) + 1
            else:
                designed.add(provider)
        return designed, ad_hoc

    def _composite_part_names(self) -> set[str]:
        """Every provider consumed as a CompositeProvider part (via its generated calc view)."""
        if not self.capability.is_available("composite_header"):
            return set()
        rows = self.select(
            self.dialect.paginate(
                self.dialect.build_select(
                    columns=["HCPRNM"], from_logical="composite_header", order_by=["HCPRNM"]
                ),
                limit=_COMPOSITE_SCAN_CAP,
            )
        )
        parts: set[str] = set()
        for row in rows:
            cp_name = _clean(row[0])
            if cp_name is None:
                continue
            resolved, _source, _caveats = self._providers.composite_parts(cp_name)
            parts.update(part.name for part in resolved if part.name)
        return parts

    # --- 9.6 ECC extractor enhancements (BW heuristic + connector-gated) -----------------

    def extractor_enhancements(self, *, limit: int = 50) -> ScenarioReport | UnsupportedResult:
        """Scenario 9.6, backed by the enhancement inventory rather than a field count alone.

        Each finding now carries the delta method, extractor program and extraction method alongside
        the customer-namespace fields, so the coordination risk is described with evidence. The exit
        *logic* still requires a source-system connector, which each finding names.
        """
        inventory = self._sources.enhancement_inventory(limit=limit)
        if isinstance(inventory, UnsupportedResult):
            return inventory
        reason = self._registry.unpopulated_reason("ecc")
        # BW holds the authoritative list of enhanced DataSources, so it is BW that supplies the
        # satellite candidates. Under runtime dispatch a satellite can exist for a DataSource the
        # exit include never names, which is precisely the case a source-only list would miss.
        candidates = [item.datasource for item in inventory.enhanced if item.datasource]
        exits = None if reason else self._exit_evidence(candidates)
        exit_index = _exit_index(exits)

        satellite_index = _satellite_index(exits)
        findings = [
            self._enhancement_finding(item, exit_index, satellite_index, reason)
            for item in inventory.enhanced
        ]
        caveats = [
            *inventory.caveats,
            f"{inventory.enhanced_count} of {inventory.total_datasources} DataSources carry "
            "customer-namespace fields; the most heavily enhanced are reported first.",
        ]
        if exits is not None:
            declared = {item.datasource for item in inventory.enhanced}
            findings.extend(self._exit_only_findings(exits, declared))
            caveats.extend(exits.caveats)
            caveats.append(
                f"Exit ABAP read from source-system profile '{exits.profile}' client "
                f"{exits.client}: {exits.available_count} of 4 slot(s) implemented, dispatching on "
                f"{len(exits.handled_datasources)} DataSource(s)."
            )
            if exits.satellite_prefixes:
                caveats.append(
                    f"{exits.satellites_found_count} per-DataSource exit program(s) found from "
                    f"{exits.satellite_candidates_considered} candidate(s). Candidates are the "
                    "DataSources BW reports as enhanced, so a satellite program serving a "
                    "DataSource with no appended field is not probed and would not appear here."
                )
        return ScenarioReport(
            scenario="9.6",
            title=SCENARIO_TITLES["9.6"],
            findings=findings,
            analyzed_count=inventory.total_datasources,
            completeness=inventory.completeness,
            connector_required=inventory.connector_required if reason else None,
            caveats=caveats,
        )

    def _exit_evidence(self, datasources: list[str] | None = None) -> ExitInventory | None:
        """Read the extractor-exit ABAP, or ``None`` when the connector cannot supply it."""
        connector = self._registry.get("ecc")
        if not isinstance(connector, EccConnector):
            return None
        try:
            return ExitAnalysisService(connector).inventory(datasources=datasources)
        except Exception:
            # A source-system outage must not fail a BW scenario; the finding degrades to the
            # BW-only evidence instead, and the caveat list simply omits the exit summary.
            return None

    def _enhancement_finding(
        self,
        item: Any,
        exit_index: dict[str, list[Any]],
        satellite_index: dict[str, list[Any]],
        reason: str | None,
    ) -> Finding:
        entries = exit_index.get(item.datasource, [])
        satellites = satellite_index.get(item.datasource, [])
        live_satellites = [s for s in satellites if s.available]
        confirmed = bool(entries)
        detail = (
            f"{item.customer_field_count} customer-namespace field(s) appended to the "
            f"extract structure. Delta method: {item.delta_method or 'unknown'}; "
            f"extraction method: {item.extraction_method or 'unknown'}; "
            f"request type: {item.request_type or 'unknown'}."
        )
        metrics: dict[str, Any] = {
            "datasource": item.datasource,
            "logical_system": item.logical_system,
            "customer_field_count": item.customer_field_count,
            "customer_fields": item.customer_fields,
            "delta_method": item.delta_method,
            "extraction_method": item.extraction_method,
            "extractor": item.extractor,
            "application": item.application,
        }
        severity: Severity = "medium" if item.customer_field_count >= _MANY_ENH_FIELDS else "low"
        recommendation = (
            "Review the source-system exit for this DataSource: confirm which tables it "
            "reads (a read into another team's data is a coordination risk) and whether it "
            "does per-record SELECTs (a performance risk that scales with extract volume). "
            "Re-verify after any source-system upgrade."
        )
        if confirmed:
            suffix, severity, recommendation = self._exit_evidence_detail(
                entries, live_satellites, metrics, severity
            )
            detail += suffix
        elif reason is None:
            detail += self._exit_absent_detail(satellites, metrics)
        return Finding(
            scenario="9.6",
            severity=severity,
            title="DataSource carries an extractor enhancement",
            affected_objects=[item.datasource],
            evidence=(
                list(item.provenance) if isinstance(item.provenance, list) else [item.provenance]
            ),
            recommendation=recommendation,
            detail=detail,
            metrics=metrics,
            unpopulated_reason=reason,
        )

    @staticmethod
    def _exit_evidence_detail(
        entries: list[Any],
        live_satellites: list[Any],
        metrics: dict[str, Any],
        severity: Severity,
    ) -> tuple[str, Severity, str]:
        """Describe what the exit code does for one DataSource, and re-rate it accordingly."""
        reads, per_record, resolved = _exit_risk(entries)
        satellite_names = sorted(s.program_name for s in live_satellites)
        code_ids = sorted({code_id for code_id, _ in entries})
        includes = [name for name in code_ids if name not in satellite_names]
        unguarded_fae = sum(s.unguarded_for_all_entries for s in live_satellites)
        metrics.update(
            exit_confirmed=True,
            exit_includes=includes,
            exit_branch_resolved=resolved,
            exit_table_reads=reads,
            exit_per_record_selects=per_record,
        )
        if satellite_names:
            metrics["exit_satellite_programs"] = satellite_names
            metrics["exit_satellite_unguarded_for_all_entries"] = unguarded_fae
            # The satellite is the more precise citation: one program, one DataSource, so its reads
            # need no hedging about which branch actually runs.
            detail = (
                " The exit code was read: this DataSource's logic lives in its own program "
                f"{', '.join(satellite_names)}, reached by a runtime-named dispatch from "
                f"{', '.join(includes) or 'the exit include'}, and reads {len(reads)} table(s)."
            )
        elif resolved:
            detail = (
                f" The exit code was read: {', '.join(code_ids)} dispatches on this DataSource and "
                f"its branch reads {len(reads)} table(s)."
            )
        else:
            detail = (
                f" The exit code was read: {', '.join(code_ids)} dispatches on this DataSource, "
                "but its branch could not be delimited, so no table read is attributed to it."
            )

        if unguarded_fae:
            # Reported but deliberately not escalated. This count is an upper bound - an sy-subrc
            # test after filling the driver table is a guard the parser does not follow - and
            # raising severity on a signal that over-reports would push genuine findings down the
            # page. The per-record SELECT below is exact, and that is what moves severity.
            detail += (
                f" Up to {unguarded_fae} FOR ALL ENTRIES read(s) there have no is-not-initial "
                "guard, which would read the whole table if the driver were empty; verify each, as "
                "an sy-subrc check counts as guarded and is not detected."
            )

        recommendation = (
            "Review the source-system exit for this DataSource: confirm which tables it reads (a "
            "read into another team's data is a coordination risk) and whether it does per-record "
            "SELECTs (a performance risk that scales with extract volume). Re-verify after any "
            "source-system upgrade."
        )
        if per_record:
            severity = "high"
            detail += (
                f" {per_record} SELECT(s) sit inside a LOOP, so the read cost scales with extract "
                "volume."
            )
            recommendation = (
                "Rework the per-record SELECT(s) in the exit into a single set-based read before "
                "the loop (FOR ALL ENTRIES or a sorted buffer table). Confirm the table(s) read "
                f"belong to this functional area: {', '.join(reads) or 'none'}."
            )
        elif resolved:
            recommendation = (
                "Confirm the table(s) the exit code reads belong to this functional area — a read "
                f"into another team's data is a coordination risk: {', '.join(reads) or 'none'}."
            )
        return detail, severity, recommendation

    @staticmethod
    def _exit_absent_detail(satellites: list[Any], metrics: dict[str, Any]) -> str:
        """Say what was checked when the exit turns out not to carry this DataSource."""
        metrics["exit_confirmed"] = False
        probed = sorted(s.program_name for s in satellites)
        if not probed:
            return (
                " The exit code was read but does not dispatch on this DataSource, so the "
                "enhancement is implemented elsewhere (a BAdI, a different include, or a dynamic "
                "dispatch this parser cannot follow)."
            )
        # Both routes were checked and both came up empty, which is a measurement rather than a
        # shrug, and it narrows where the logic can be.
        metrics["exit_satellite_probed"] = probed
        return (
            " The exit code was read and does not dispatch on this DataSource, and no "
            f"per-DataSource exit program exists either ({', '.join(probed)} were checked and are "
            "absent), so the enhancement is implemented somewhere neither route reaches — a BAdI, "
            "or a class-based dispatch."
        )

    def _exit_only_findings(self, exits: Any, declared: set[str]) -> list[Finding]:
        """DataSources the exit handles that BW's appended-field evidence does not reveal.

        An enhancement that overwrites an existing field adds no field to the extract structure, so
        BW shows nothing. Only the exit source exposes it.
        """
        findings: list[Finding] = []
        for slot in exits.exits:
            if not slot.available or slot.provenance is None:
                continue
            missed = [name for name in slot.handled_datasources if name not in declared]
            for name in sorted(missed):
                findings.append(
                    Finding(
                        scenario="9.6",
                        severity="medium",
                        title="DataSource enhanced in the exit with no appended fields",
                        affected_objects=[name],
                        evidence=[
                            Provenance(
                                source_table="ADT",
                                source_key={
                                    "INCLUDE": slot.include_name,
                                    "CLIENT": exits.client,
                                    "PROFILE": exits.profile,
                                },
                            )
                        ],
                        recommendation=(
                            "Confirm what this exit branch changes. Because no field was appended "
                            "to the extract structure, it most likely overwrites a standard "
                            "field's value — a change invisible to every BW-side check."
                        ),
                        detail=(
                            f"The {slot.data_kind.replace('_', ' ')} exit ({slot.include_name}) "
                            "dispatches on this DataSource, but its extract structure carries no "
                            "customer-namespace field."
                        ),
                        metrics={
                            "datasource": name,
                            "exit_include": slot.include_name,
                            "exit_data_kind": slot.data_kind,
                            "detected_from": "exit_source",
                        },
                    )
                )
        return findings

    # --- 9.7 report schedules vs. chain completion (connector-gated) ---------------------

    def schedule_risk(self, *, limit: int = 100) -> ScenarioReport | UnsupportedResult:
        unsupported = self.require("chain_attr", "log_chain")
        if unsupported is not None:
            return unsupported
        matrix = self._chains.get_schedule_matrix(limit=limit)
        entries_by_provider: dict[str, ScheduleMatrixEntry] = {}
        chains_with_p95 = 0
        if not isinstance(matrix, UnsupportedResult):
            entries, _ = matrix
            chains_with_p95 = sum(1 for e in entries if e.p95_completion is not None)
            entries_by_provider = {e.chain_id.upper(): e for e in entries}

        # With a BI connector the margins are computable, so compute them.
        populated = self._schedule_risk_findings(entries_by_provider, limit=limit)
        if populated is not None:
            return populated

        reason = self._registry.bi_unpopulated_reason()
        finding = Finding(
            scenario="9.7",
            severity="info",
            title="Report-side schedules require an external BI connector",
            affected_objects=[],
            evidence=[],
            recommendation=(
                "Configure a BI connector (any platform: export a report inventory to YAML/JSON "
                "and point 'bi_systems' at it) to compare each report's scheduled start against "
                "the p95 completion of the chain feeding its provider. Negative or sub-30-minute "
                "margins are then flagged, and those reports should move to event-based triggering."
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
            connector_required="BI platform" if reason else None,
            caveats=[
                "Safety-margin math is implemented (services.latency) and populates per report as "
                "soon as a BI connector supplies report start times.",
            ],
        )

    def _schedule_risk_findings(
        self, entries_by_chain: dict[str, ScheduleMatrixEntry], *, limit: int
    ) -> ScenarioReport | None:
        """Per-report safety margins, when a BI connector supplies the report side.

        ``None`` when no connector is configured, so the caller emits the gap-reporting finding.
        """
        connector = self._registry.bi()
        if connector is None or not isinstance(connector, BiConnector):
            return None
        reports = connector.report_schedules()
        platform = connector.platform() or "the configured BI platform"

        findings: list[Finding] = []
        unmatched: list[str] = []
        for report in reports[:limit]:
            entry = self._feeding_entry(report.provider, entries_by_chain)
            if entry is None or entry.p95_completion is None or report.scheduled_start is None:
                unmatched.append(report.name)
                continue
            margin = latency.safety_margin_minutes(report.scheduled_start, entry.p95_completion)
            severity, why = latency.classify_margin(margin)
            findings.append(
                Finding(
                    scenario="9.7",
                    severity=severity,
                    title=f"Report schedule vs feeding chain: {report.name}",
                    affected_objects=[report.name, entry.chain_id],
                    evidence=[
                        self.provenance("log_chain", {"CHAIN_ID": entry.chain_id}),
                    ],
                    recommendation=(
                        "Switch this report to event-based triggering on the feeding chain's "
                        "completion event rather than a clock time. A clock-based start cannot "
                        "adapt when the chain runs long."
                        if severity in {"critical", "high"}
                        else "Margin is adequate; re-check if the chain's runtime grows."
                    ),
                    detail=why,
                    metrics={
                        "report": report.name,
                        "platform": platform,
                        "provider": report.provider,
                        "chain_id": entry.chain_id,
                        "report_start": report.scheduled_start,
                        "chain_p95_completion": entry.p95_completion,
                        "safety_margin_minutes": margin,
                        "min_safe_margin_minutes": latency.MIN_SAFE_MARGIN_MINUTES,
                    },
                )
            )

        caveats = [
            f"Report side supplied by the {platform} inventory; the BW side is observed p95 "
            "completion from run history.",
            "Margins are same-day wall-clock differences: a feeding chain that completes after "
            "midnight is not resolved, so treat a near-zero margin as advisory.",
        ]
        if unmatched:
            caveats.append(
                f"{len(unmatched)} report(s) could not be matched to a feeding chain with a p95 "
                "completion (no provider given, provider not loaded by a chain with run history, "
                "or no scheduled start); they are excluded rather than assumed safe."
            )
        return ScenarioReport(
            scenario="9.7",
            title=SCENARIO_TITLES["9.7"],
            findings=findings,
            analyzed_count=len(reports[:limit]),
            completeness=_capped(len(reports) > limit, scope="reports", limit=limit),
            caveats=caveats,
        )

    def _feeding_entry(
        self, provider: str | None, entries_by_chain: dict[str, ScheduleMatrixEntry]
    ) -> ScheduleMatrixEntry | None:
        """The schedule-matrix entry for the chain that loads ``provider``, if resolvable."""
        if not provider:
            return None
        closure = self._closure.provider_to_chains(provider)
        if isinstance(closure, UnsupportedResult):
            return None
        governing = cadence_of(closure.loading_chains)
        if governing is None:
            return None
        return entries_by_chain.get(governing.chain_id.upper())

    # --- 9.8 dashboards on calc views (connector-gated) ----------------------------------

    def dashboards_on_calc_views(self, *, limit: int = 100) -> ScenarioReport | UnsupportedResult:
        populated = self._dashboard_bypass_findings(limit=limit)
        if populated is not None:
            return populated

        bw_consuming = self._bw_consuming_calc_view_count(limit)
        reason = self._registry.bi_unpopulated_reason()
        finding = Finding(
            scenario="9.8",
            severity="info",
            title="Dashboard-to-calc-view bypass detection requires a BI connector",
            affected_objects=[],
            evidence=[],
            recommendation=(
                "Configure a BI connector (export your platform's dashboard inventory to "
                "YAML/JSON and point 'bi_systems' at it) to list dashboards reading calc views "
                "directly. Each is then classified automatically: a view shared with a "
                "CompositeProvider means one change breaks both paths, while a separate view means "
                "the two can silently diverge in numbers."
            ),
            detail=(
                f"{bw_consuming} BW-consuming calc view(s) exist that a dashboard could read "
                "directly, bypassing BW; matching them to dashboards needs your BI platform's "
                "inventory, which BW does not hold."
            ),
            metrics={"bw_consuming_calc_views": bw_consuming},
            unpopulated_reason=reason,
        )
        return ScenarioReport(
            scenario="9.8",
            title=SCENARIO_TITLES["9.8"],
            findings=[finding],
            analyzed_count=bw_consuming,
            connector_required="BI platform" if reason else None,
            caveats=[
                "Calc views that read BW tables are candidate bypass paths; confirming a dashboard "
                "actually reads one, and whether it is shared with the CompositeProvider path, "
                "needs Tableau lineage metadata (mission Known Limitation 2).",
            ],
        )

    def _dashboard_bypass_findings(self, *, limit: int) -> ScenarioReport | None:
        """Dashboards reading a calc view directly, and whether that view is shared with BW.

        The mission's distinction is the point of the scenario: a dashboard on the **same** calc
        view a CompositeProvider uses means one change breaks both paths at once; a **separate**
        view means the two paths can silently diverge in numbers. Both are findings, and which one
        applies is determined from the calc view's consumers rather than guessed.
        """
        connector = self._registry.bi()
        if connector is None or not isinstance(connector, BiConnector):
            return None
        sources = [d for d in connector.dashboard_sources() if d.source_kind != "bw_provider"]
        platform = connector.platform() or "the configured BI platform"

        findings: list[Finding] = []
        for dashboard in sources[:limit]:
            shared_with = self._providers_consuming(dashboard.source_object)
            if shared_with:
                severity: Severity = "high"
                detail = (
                    f"reads calc view '{dashboard.source_object}' directly, and the same view is "
                    f"consumed by BW provider(s) {', '.join(shared_with)}. One change to the view "
                    "affects both the dashboard and the BW path simultaneously."
                )
                recommendation = (
                    "Treat this calc view as a shared contract: changes need both the BW and the "
                    "dashboard owner in the loop, and BW's where-used list will not warn either of "
                    "them."
                )
            else:
                severity = "medium"
                detail = (
                    f"reads calc view '{dashboard.source_object}' directly, and no BW provider "
                    "consumes that view. The dashboard and the BW path are therefore independent "
                    "and can diverge in numbers without either side erroring."
                )
                recommendation = (
                    "Reconcile this dashboard's figures against the BW path deliberately, or point "
                    "it at the provider's view so both read one definition."
                )
            findings.append(
                Finding(
                    scenario="9.8",
                    severity=severity,
                    title=f"Dashboard bypasses BW: {dashboard.name}",
                    affected_objects=[dashboard.name, dashboard.source_object, *shared_with],
                    evidence=[
                        self.provenance(
                            "object_dependencies", {"CALC_VIEW": dashboard.source_object}
                        )
                    ],
                    recommendation=recommendation,
                    detail=detail,
                    metrics={
                        "dashboard": dashboard.name,
                        "platform": platform,
                        "calc_view": dashboard.source_object,
                        "shared_with_bw_providers": shared_with,
                        "path": "shared_view" if shared_with else "separate_view",
                    },
                )
            )
        return ScenarioReport(
            scenario="9.8",
            title=SCENARIO_TITLES["9.8"],
            findings=findings,
            analyzed_count=len(sources[:limit]),
            completeness=_capped(len(sources) > limit, scope="sources", limit=limit),
            caveats=[
                f"Dashboard side supplied by the {platform} inventory; whether the view is shared "
                "with BW comes from the generated '0BW:BIA:' provider views in "
                "SYS.OBJECT_DEPENDENCIES.",
                "Dashboards reading a BW provider (rather than a calc view) are not bypasses and "
                "are excluded.",
            ],
        )

    def _providers_consuming(self, calc_view: str) -> list[str]:
        """BW providers whose generated views read ``calc_view`` (empty when none/unresolvable)."""
        lineage = self._hana.get_calc_view_lineage(calc_view)
        if isinstance(lineage, UnsupportedResult):
            return []
        return [consumer.provider for consumer in lineage.consuming_bw_providers]

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
        findings += self._self_loop_violations(limit)
        findings += self._cycle_violations(limit)
        return ScenarioReport(
            scenario="layer_violations",
            title=SCENARIO_TITLES["layer_violations"],
            findings=findings,
            analyzed_count=len(findings),
            completeness=_capped(len(findings) >= limit, scope="findings", limit=limit),
            caveats=[
                f"Deep-stack threshold is {max_dso_depth} DSO->DSO hops; adjust via max_dso_depth.",
                "Circular dependencies are detected at any length, not just pairs: each finding "
                "names every object in the loop. An object that feeds itself is reported "
                "separately. The only bound is the edge scan, capped at "
                f"{_SCAN_CAP} rows - a cycle whose edges fall outside that scan would be missed.",
            ],
        )

    # --- circular dependencies -------------------------------------------------------------
    #
    # Deliberately not called "write-back": in BW that term already means planning data written back
    # to a provider, which is a different and legitimate feature. Using it for a dependency cycle is
    # ambiguous inside BW's own vocabulary, and to anyone outside it reads as though this server
    # modifies the system. It does not - it issues SELECT only.

    def _self_loop_violations(self, limit: int) -> list[Finding]:
        """A transformation whose source and target are the same object: it reads what it writes.

        The load reads the object it writes, so the result depends on how much of the target was
        already populated when the DTP ran. Re-running it does not reproduce the same data, which is
        why a failed load cannot simply be repeated.
        """
        rows = self._fetch_transform(
            ["TRANID", "SOURCENAME", "TARGETNAME", "TARGETTYPE"],
            ["SOURCENAME = TARGETNAME", "SOURCENAME <> ''"],
            [],
            limit=limit,
            order_by=["SOURCENAME", "TRANID"],
        )
        findings: list[Finding] = []
        for tranid, src, _tgt, tgt_type in rows:
            name = _clean(src)
            if name is None:
                continue
            findings.append(
                Finding(
                    scenario="layer_violation",
                    severity="high",
                    title="Circular dependency: a transformation reads and writes the same object",
                    affected_objects=[name],
                    evidence=[
                        self.provenance("transformation", {"TRANID": str(tranid), "OBJVERS": "A"})
                    ],
                    recommendation=(
                        "Split the read from the write: stage the derived records in a separate "
                        "object and load from there. Until then, treat this load as non-repeatable "
                        "and check the target's contents before re-running a failed request."
                    ),
                    detail=(
                        "Source and target are the same object, so the load's output depends on "
                        "the target's existing contents and re-running it does not reproduce the "
                        "same result."
                    ),
                    metrics={
                        "tran_id": str(tranid),
                        "kind": "self_loop",
                        "object": name,
                        "object_type": _clean(tgt_type),
                    },
                )
            )
        return findings

    def _cycle_violations(self, limit: int) -> list[Finding]:
        """Every cyclic group of objects, of any length.

        This used to look only for pairs that feed each other, and said so: ``A -> B -> C -> A``
        was not searched. A three-object loop has no correct load order either, so it is the same
        finding with a longer member list - and on a landscape where objects write back to their own
        layer it is the more likely shape.

        The graph component reports strongly connected components, which is both complete and
        linear: every member of a component is reachable from every other, so no load order for the
        group produces a defined result. The edge scan is still capped, and that cap is the only
        reason this could be incomplete - stated in the caveats.
        """
        rows = self._fetch_transform(
            # Both endpoint types are read, not just the target's. Typing one side and leaving the
            # other 'unknown' gives one object two graph keys - it appears as unknown:X when it is a
            # source and dso:X when it is a target - so the edges never join up and no cycle is ever
            # found. Exactly the identity split the canonical object model exists to prevent.
            ["TRANID", "SOURCENAME", "SOURCETYPE", "TARGETNAME", "TARGETTYPE"],
            # Self-loops are excluded here rather than filtered afterwards: they are reported
            # separately with their own explanation, and against a 5,000-row cap it is worth not
            # spending rows on edges that will be discarded.
            ["SOURCENAME <> ''", "TARGETNAME <> ''", "SOURCENAME <> TARGETNAME"],
            [],
            limit=_SCAN_CAP,
            order_by=["SOURCENAME", "TARGETNAME"],
        )
        graph = ObjectGraph()
        tran_by_edge: dict[tuple[str, str], str] = {}
        for tranid, src, src_type, tgt, tgt_type in rows:
            source, target = _clean(src), _clean(tgt)
            if not source or not target:
                continue
            src_ref = BwObjectRef(object_type=normalise_object_type(src_type), name=source)
            tgt_ref = BwObjectRef(object_type=normalise_object_type(tgt_type), name=target)
            graph.add_edge(src_ref, tgt_ref)
            tran_by_edge.setdefault((src_ref.id, tgt_ref.id), str(tranid).strip())

        findings: list[Finding] = []
        for cycle in graph.cycles():
            names = [key.split(":", 1)[1] for key in cycle.members]
            tran_ids = sorted(
                {tran for edge in cycle.edges if (tran := tran_by_edge.get((edge.src, edge.dst)))}
            )
            findings.append(
                Finding(
                    scenario="layer_violation",
                    severity="high",
                    title=(
                        "Circular dependency: two objects each feed the other"
                        if len(names) == _PAIR
                        else f"Circular dependency across {len(names)} objects"
                    ),
                    affected_objects=names,
                    evidence=[
                        self.provenance("transformation", {"TRANID": tran, "OBJVERS": "A"})
                        for tran in tran_ids[:_CYCLE_EVIDENCE_CAP]
                    ],
                    recommendation=(
                        "Break the cycle. Decide which object is authoritative for the shared "
                        "fields and derive the others from it one way, or introduce a separate "
                        "object for the derived values. A cycle has no correct load order, so this "
                        "cannot be resolved by scheduling."
                    ),
                    detail=(
                        "Transformations connect these objects in a loop, so every one of them is "
                        "downstream of every other. Which of them holds current data depends on "
                        "which chain ran last, and a failed load cannot be re-run to a defined "
                        "state."
                    ),
                    metrics={
                        "kind": "cycle",
                        "member_count": len(names),
                        "objects": names,
                        # Canonical ids too, so a caller can hand a member straight to
                        # bw_describe_object or match it against a lineage node.
                        "object_ids": cycle.members,
                        "tran_ids": tran_ids,
                    },
                )
            )
            if len(findings) >= limit:
                break
        return findings

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
