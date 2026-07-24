"""Analyst-workflow prompts (B9, mission Section 4).

Each prompt is a reusable template that composes the server's read-only tools into a guided
workflow. Prompts return instructional text naming the exact tools to call in order; they never
call a mutating path (there are none) and never fabricate results. ``register_prompts`` attaches
them to the FastMCP instance from ``server.py`` (kept here to avoid a circular import).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def analyze_impact(system: str, object_name: str) -> str:
    """Full downstream + upstream review before changing an object."""
    return (
        f"Assess the full impact of changing `{object_name}` on system `{system}`.\n\n"
        "Work through these read-only tools in order and summarize the blast radius:\n"
        f"1. `bw_describe_object(system='{system}', name='{object_name}')` - confirm the object "
        "type, fields, and stored/generated description.\n"
        f"2. `bw_impact_analysis(system='{system}', name='{object_name}')` - the complete "
        "downstream blast radius, INCLUDING routine-embedded lookups that BW's own where-used "
        "list misses (these are advisory - state that).\n"
        f"3. `bw_get_lineage(system='{system}', name='{object_name}', direction='both')` - the "
        "declared upstream/downstream graph.\n"
        f"4. `bw_trace_to_source(system='{system}', name='{object_name}')` - trace upstream to the "
        "originating DataSource(s).\n\n"
        "Report: direct consumers, transitive consumers, any query/report affected, any calc-view "
        "or HANA crossing involved, and every dependency marked advisory. Call out what could not "
        "be verified rather than presenting assumptions as fact."
    )


def troubleshoot_missing_data(system: str, target: str) -> str:
    """Layer-by-layer diagnostic walk when a report shows wrong or missing data."""
    return (
        f"A report/object `{target}` on system `{system}` shows wrong or missing data. Diagnose "
        "layer by layer, top-down, and stop at the first layer that explains it:\n\n"
        f"1. If `{target}` is a query, run `bw_get_query_lineage(system='{system}', "
        f"query='{target}')` to map it to its provider and fields; otherwise "
        f"`bw_get_lineage(system='{system}', name='{target}', direction='upstream')`.\n"
        "2. For each feeding provider, check the loading chain's health with "
        f"`bw_get_chain_runtimes` - look for failed/late runs in the window.\n"
        f"3. Run `bw_check_load_latency(system='{system}')` - a full-update load enriching against "
        "stale looked-up data is a common wrong-data cause.\n"
        "4. Inspect the suspect transformation with `bw_get_transformation` and, if it has "
        "routines, `bw_analyze_routine` (routine logic is a heuristic lower bound).\n"
        f"5. If a CompositeProvider/calc view is involved, `bw_get_calc_view_lineage` - a "
        "calc-view change silently changes downstream content with no BW where-used warning.\n\n"
        "Name the exact object/table to inspect at each hop and the evidence (provenance) behind "
        "each conclusion."
    )


def document_dataflow(system: str, object_name: str) -> str:
    """Produce a narrative document for one end-to-end data flow."""
    return (
        f"Write an end-to-end narrative for the data flow around `{object_name}` on system "
        f"`{system}`.\n\n"
        f"1. `bw_trace_to_source(system='{system}', name='{object_name}')` - establish the "
        "originating DataSource(s) and the hop-by-hop path.\n"
        f"2. `bw_get_lineage(system='{system}', name='{object_name}', direction='both')` - the "
        "full graph; note update modes and any advisory routine edges.\n"
        "3. For each transformation on the path, `bw_get_transformation` (and `bw_get_routine_code`"
        " where routines exist) to describe what each hop does to the data.\n"
        "4. If the flow reaches a query, `bw_get_query_lineage` for field-level lineage to the "
        "DataSource, flagging customer-exit variables as dead ends.\n\n"
        "Structure the document source -> target, cite the source table for every fact, and mark "
        "generated descriptions and advisory (routine/heuristic) hops explicitly."
    )


def review_scenario(system: str, scenario: str) -> str:
    """Run one of the eight risk scenarios (mission Section 9) and interpret the findings."""
    return (
        f"Run risk scenario `{scenario}` on system `{system}` and interpret the result.\n\n"
        f"Call `bw_review_scenario(system='{system}', scenario='{scenario}')` (valid ids: 9.1-9.8, "
        "or 'layer_violations'). For 9.1 you may also use `bw_check_load_latency`, for 9.7 "
        "`bw_check_schedule_risk`, and for layer violations `bw_find_layer_violations`.\n\n"
        "Then: order the findings by severity, explain each in plain terms with its affected "
        "objects and evidence, and give the recommended action. If the report has a "
        "`connector_required` (9.6 ECC, 9.7/9.8 Tableau/BOBJ) or `unpopulated_reason`, state "
        "clearly which part is a documented gap versus a substantiated finding, and honor every "
        "caveat (heuristic / advisory / object->chain mapping unavailable)."
    )


def onboard_analyst(system: str, functional_area: str) -> str:
    """Orientation brief for a functional area (FI, SD, MM, ...)."""
    return (
        f"Produce an orientation brief for the `{functional_area}` functional area on system "
        f"`{system}`.\n\n"
        f"1. `bw_search_objects(system='{system}', query='{functional_area}')` - the key "
        "providers, InfoObjects, and queries in this area (both name and description matches).\n"
        f"2. `bw_list_chains(system='{system}')` - the chains that load this area; note observed "
        "frequency.\n"
        f"3. `bw_list_queries(system='{system}')` - the reports analysts use, ranked by usage; "
        "flag zero-usage queries as decommission candidates.\n\n"
        "Summarize: the main data flow, the objects an analyst will touch most, the load cadence, "
        "and where to look first when data looks wrong. Keep it a starting map, not exhaustive."
    )


def pre_change_checklist(system: str, object_name: str) -> str:
    """Everything to verify before transporting a change to an object."""
    return (
        f"Build a pre-transport checklist for a change to `{object_name}` on system `{system}`.\n\n"
        f"1. `bw_impact_analysis(system='{system}', name='{object_name}')` - enumerate every "
        "downstream consumer, including advisory routine-embedded lookups.\n"
        f"2. `bw_find_layer_violations(system='{system}')` - check the change does not create or "
        "worsen a CompositeProvider->DSO / ->InfoObject edge or a deep DSO stack.\n"
        f"3. `bw_check_load_latency(system='{system}')` - confirm the change does not introduce a "
        "full-update load reading less-frequently-refreshed data.\n"
        "4. For each affected query, confirm shared restricted key figures / structures are not "
        "changed unintentionally (`bw_get_query`).\n\n"
        "Output a checklist: objects to re-activate, chains to re-run in order, reports to "
        "re-validate, and any sequencing (master-before-transaction) to preserve. Flag anything "
        "unverifiable from metadata."
    )


def register_prompts(mcp: FastMCP) -> None:
    """Attach every workflow prompt to the FastMCP instance."""
    for func in (
        analyze_impact,
        troubleshoot_missing_data,
        document_dataflow,
        review_scenario,
        onboard_analyst,
        pre_change_checklist,
    ):
        mcp.prompt(func)
