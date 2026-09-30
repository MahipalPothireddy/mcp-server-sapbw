"""Generate the performance profile: what each tool costs, and what a bigger system does to it.

**The question this answers**, which nothing else in the project does:

    We have 40,000 InfoObjects and 1,200 chains. Which of these calls will still return, and what
    will they cost?

The server already bounds every call, so nothing runs away. But a bound you only discover by hitting
it is not a performance expectation, and a customer sizing this cannot plan around it.

**What is measured and what is declared, and why the two are never blended.**

*Payload bytes* are measured, by invoking every tool through a real client against the synthetic
fixtures and serialising the reply. Deterministic, so CI can check it. It is a floor rather than a
forecast: the fixture holds roughly one object per type, so the number isolates the fixed overhead
of a reply's shape from its per-row cost. That is the useful part - a 10 KiB reply about one object
is a shape problem, not a data problem.

*Growth* is declared, with the code constant that bounds it named. Whether a call's work scales with
the customer's system is a property of the code, and a fixture with one DSO cannot demonstrate what
four thousand do. Declaring it and citing the cap can be checked against the source; fitting a curve
to a one-object fixture could not.

Statement counts are **not** measured here. Budget charging lives in ``ReadOnlyConnection``, and the
offline fixtures substitute their own connection, so no statement count is observable offline. The
profile says ``not_measured`` rather than reporting zero, which would read as "issues no queries".

Usage::

    python scripts/performance_profile.py            # regenerate the data file and the doc
    python scripts/performance_profile.py --check    # non-zero exit when either is stale

**Regenerate this after the support matrix, not before.** One of the payloads measured here is
``bw_support_matrix``'s own reply, which changes whenever the matrix is rebuilt - so running these
two generators in the other order leaves this one stale on the very next check.

Exit codes: 0 clean, 1 stale, 2 usage error.
"""

from __future__ import annotations

import asyncio
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from fastmcp import Client  # noqa: E402

# The test harness supplies the argument mapping, which is asserted to name every registered tool -
# so a tool cannot be added without appearing here too. Imported at module scope rather than lazily
# because measuring is the script's only job; there is no path through it that does not measure.
from tests.test_server import _CHAIN_TABLES, FakeRuntime  # noqa: E402
from tests.test_tool_surface import _ARGS, _NEEDS_OUTPUT_DIR  # noqa: E402

from mcp_server_sapbw import server  # noqa: E402
from mcp_server_sapbw.core.budget import DEFAULT_MAX_QUERIES, DEFAULT_MAX_SECONDS  # noqa: E402
from mcp_server_sapbw.models.performance import (  # noqa: E402
    CostBound,
    GrowthClass,
    PerformanceProfile,
    ToolCost,
)

DATA_PATH = _ROOT / "src" / "mcp_server_sapbw" / "data" / "performance_profile.json"
DOC_PATH = _ROOT / "docs" / "performance.md"

# --- bounds, each citing the constant that sets it ----------------------------------------

PAGE = CostBound(
    bound="500 rows per page",
    constant="server._MAX_PAGE",
    on_hit="total_count reports the full size; page with limit/offset",
)
GRAPH = CostBound(
    bound="400 graph nodes, depth 12",
    constant="services.lineage._MAX_NODES / _MAX_DEPTH",
    on_hit="truncated=true plus a caveat naming which cap stopped the walk",
)
ANALYSIS_DEPTH = CostBound(
    bound="depth clamped to 5",
    constant="server._MAX_ANALYSIS_DEPTH",
    on_hit="the requested depth is silently lowered; the reply reports the depth used",
)
SHAPE_FIELDS = CostBound(
    bound="40 fields, 60 nodes, 90 edges inlined",
    constant="server._MAX_INLINE_FIELDS / _MAX_INLINE_NODES / _MAX_INLINE_EDGES",
    on_hit="the reply carries the shape plus the bw:// resource URI holding the full record",
)
REGISTER = CostBound(
    bound="500 routines parsed (default 100)",
    constant="services.routine_register._MAX_PARSE_BUDGET",
    on_hit="analyzed=false on unparsed entries - no pattern counts rather than zeroes",
)
BUDGET = CostBound(
    bound=f"{DEFAULT_MAX_QUERIES} statements / {DEFAULT_MAX_SECONDS:.0f}s per call",
    constant="core.budget.DEFAULT_MAX_QUERIES / DEFAULT_MAX_SECONDS",
    on_hit="BudgetResult naming what was spent and where it stopped",
)

_PER_SYSTEM_BOUNDS: dict[str, CostBound] = {
    "bw_check_load_latency": CostBound(
        bound="250 routine parses",
        constant="services.analyzers._LATENCY_PARSE_BUDGET",
        on_hit="a caveat states how many of the candidates were parsed",
    ),
    "bw_security_overview": CostBound(
        bound="50,000 RSECVAL rows scanned",
        constant="repositories.security._MAX_SCAN_ROWS",
        on_hit="the scan cap is reported, not hidden",
    ),
    "bw_generate_docs": CostBound(
        bound="5,000 detail pages per section",
        constant="server._MAX_DOCGEN_PAGES",
        on_hit="the manifest lists what was written; resume=true continues a cut-off run",
    ),
    "bw_get_routine_register": REGISTER,
    "bw_assess_landscape": CostBound(
        bound="9 analyses, each capped by limit_per_scenario (default 25)",
        constant="services.assessment.ASSESSED_SCENARIOS",
        on_hit="each scenario reports truncated, and the assessment calls its counts lower bounds",
    ),
    "bw_list_business_areas": CostBound(
        bound="one grouped COUNT per provider family",
        constant="repositories.semantics._PROVIDER_SOURCES",
        on_hit="no row cap is needed: the read aggregates in the database, not in the reply",
    ),
    "bw_get_extractor_exit_code": CostBound(
        bound="400 satellite program fetches (configurable)",
        constant="EccProfile.max_satellite_fetches",
        on_hit="the shortfall is reported as a caveat, never as 'no satellite exists'",
    ),
}

# --- growth declaration -------------------------------------------------------------------
#
# Every registered tool must appear here; a test asserts it. An unclassified tool would otherwise
# be omitted from the profile, and a customer would read its absence as "nothing to worry about".

_CONSTANT = (
    "bw_list_systems",
    "bw_system_profile",
    "bw_refresh_capabilities",
    "bw_capability_report",
    # One bulk read of DD07L/DD07T for a fixed set of registered domains - 299 rows across 22
    # domains, 0.22s measured on the reference system. The set is a property of this build, so the
    # cost does not move with the size of the landscape.
    "bw_check_code_decodes",
    "bw_access_report",
    "bw_support_matrix",
    "bw_performance_profile",
    "bw_cache_status",
    "bw_refresh_cache",
)
_PER_PAGE = (
    "bw_list_chains",
    "bw_list_transformations",
    "bw_list_queries",
    "bw_list_calc_views",
    "bw_get_hana_crossings",
    "bw_list_analysis_auths",
    "bw_search_objects",
    "bw_list_3x_flows",
    "bw_list_update_rules",
    "bw_get_schedule_matrix",
    "bw_list_snapshots",
    "bw_list_extractor_enhancements",
)
_PER_OBJECT = (
    "bw_describe_object",
    "bw_get_chain",
    "bw_get_chain_runtimes",
    "bw_get_transformation",
    "bw_get_routine_code",
    "bw_analyze_routine",
    "bw_get_query",
    "bw_get_query_lineage",
    "bw_get_query_usage",
    "bw_get_query_auth_exposure",
    "bw_get_analysis_auth",
    "bw_get_calc_view_lineage",
    # Two bounded reads of one repository row - the size, then the definition - plus a local parse.
    # Cost follows the one view named, and the definition size is bounded before it is fetched.
    "bw_get_calc_view_logic",
    "bw_get_provider_health",
    "bw_get_transfer_rules",
    "bw_get_load_closure",
    "bw_analyze_query",
    "bw_analyze_process_chain",
)
_PER_GRAPH = (
    "bw_get_lineage",
    "bw_impact_analysis",
    "bw_trace_to_source",
    "bw_render_lineage",
    "bw_analyze_object",
    "bw_assess_change_impact",
    "bw_troubleshoot_missing_data",
)
_PER_SYSTEM = (
    "bw_assess_landscape",
    "bw_list_business_areas",
    "bw_get_routine_register",
    "bw_find_unused_providers",
    "bw_find_layer_violations",
    "bw_check_load_latency",
    "bw_check_schedule_risk",
    "bw_review_scenario",
    "bw_security_overview",
    "bw_get_source_systems",
    "bw_create_snapshot",
    "bw_compare_snapshots",
    "bw_compare_systems",
    "bw_generate_docs",
    "bw_get_extractor_exit_code",
)

_GROWTH: dict[str, GrowthClass] = {
    **dict.fromkeys(_CONSTANT, "constant"),
    **dict.fromkeys(_PER_PAGE, "per_page"),
    **dict.fromkeys(_PER_OBJECT, "per_object"),
    **dict.fromkeys(_PER_GRAPH, "per_graph_node"),
    **dict.fromkeys(_PER_SYSTEM, "per_system"),
}

_BASIS: dict[GrowthClass, str] = {
    "constant": (
        "Reads the server's own description or the discovery record, both bounded by this build "
        "rather than by the landscape. The same size on a small system and a huge one."
    ),
    "per_page": (
        "Returns one page of rows. Cost is set by `limit`, not by system size; `total_count` says "
        "how much is behind it."
    ),
    "per_object": (
        "Proportional to the one object named - its fields, rules, elements or run history - not "
        "to how many such objects exist."
    ),
    "per_graph_node": (
        "Walks the dependency graph outward from one object, so cost follows the connected "
        "subgraph. A hub object is far more expensive than a leaf at the same depth."
    ),
    "per_system": (
        "Scans a whole class of objects rather than one. These are the calls to plan for on a "
        "large system, and each names the cap that stops it running away."
    ),
}

#: Tools whose reply is summarised, with a resource URI carrying the full record.
_SHAPED = frozenset(
    {
        "bw_describe_object",
        "bw_get_lineage",
        "bw_impact_analysis",
        "bw_analyze_object",
        "bw_analyze_query",
        "bw_analyze_process_chain",
        "bw_assess_change_impact",
        "bw_troubleshoot_missing_data",
    }
)

_NOTES: dict[str, list[str]] = {
    "bw_analyze_object": [
        "Observed 20.4s on the reference system at depth 2: lineage 11s, calc-view consumers 4.4s. "
        "Composes several walks, so one extra depth level multiplies across sections."
    ],
    "bw_generate_docs": [
        "The one tool expected to page rather than complete in a single call on a large system. "
        "Split a full run across sections and use resume=true."
    ],
    "bw_get_transformation": [
        "The bw:// transformation resource is deliberately unshaped and measured 80 KiB on the "
        "reference system - it is the full record by definition. Prefer the tool for a summary."
    ],
    "bw_get_query": [
        "Measured 37-67 KiB on the reference system. Unshaped on purpose: asking for one query is "
        "a request for exactly that. Composed answers summarise it instead."
    ],
    "bw_security_overview": [
        "Reads RSECVAL, which holds permission values. Nothing here is cached at any tier, so a "
        "repeat call costs the same as the first."
    ],
}


#: Tools that page their reply without being ``per_page`` overall - the database work scales with
#: the landscape even though the reply does not, so both bounds apply and both are worth stating.
_ALSO_PAGINATED = frozenset({"bw_list_business_areas"})


def _bounds_for(tool: str, growth: GrowthClass) -> list[CostBound]:
    bounds: list[CostBound] = []
    if growth == "per_page" or tool in _ALSO_PAGINATED:
        bounds.append(PAGE)
    if growth == "per_graph_node":
        bounds.append(GRAPH)
        if tool in {"bw_analyze_object", "bw_assess_change_impact", "bw_troubleshoot_missing_data"}:
            bounds.append(ANALYSIS_DEPTH)
        if tool == "bw_render_lineage":
            bounds.append(
                CostBound(
                    bound="depth clamped to 8",
                    constant="server._MAX_DIAGRAM_DEPTH",
                    on_hit="a truncated graph says so on the canvas",
                )
            )
    if tool in _PER_SYSTEM_BOUNDS:
        bounds.append(_PER_SYSTEM_BOUNDS[tool])
    if tool in _SHAPED:
        bounds.append(SHAPE_FIELDS)
    # Every tool runs inside the per-call budget. Listed last so the specific bound reads first.
    bounds.append(BUDGET)
    return bounds


# --- payload measurement ------------------------------------------------------------------


#: Tools whose payload cannot be measured here because it *is* what this script writes.
#:
#: ``bw_performance_profile`` reads the shipped profile, so measuring it records a size that depends
#: on the file being written - and the next run measures the new file and gets a different number.
#: Regeneration could not converge, and ``--check`` failed immediately after a rebuild (it did).
#: Reported as ``not_measured`` with the reason, rather than pinned to a value that is wrong by
#: construction.
_SELF_REFERENTIAL = frozenset({"bw_performance_profile"})

#: Fields holding a wall-clock reading, zeroed before the payload is measured.
#:
#: These are genuine measurements and belong in the reply, but they are not part of a reply's
#: *shape*, which is what ``fixture_payload_bytes`` documents. Left alone, an analysis that happened
#: to take 9 ms one run and 11 ms the next changed the recorded byte count, so regeneration never
#: converged and ``--check`` failed straight after a rebuild. Same failure mode as
#: ``_SELF_REFERENTIAL`` above, from a different cause.
_VOLATILE_FIELDS = frozenset({"duration_ms", "time_used_ms"})


def _stabilise(value: Any) -> Any:
    """Zero any wall-clock duration, so the measurement reflects shape rather than machine speed."""
    if isinstance(value, dict):
        return {
            k: 0 if k in _VOLATILE_FIELDS and isinstance(v, int | float) else _stabilise(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_stabilise(v) for v in value]
    return value


def _measure_payloads() -> dict[str, int]:
    """Serialised reply size per tool, against the synthetic fixtures.

    A tool that cannot be invoked here is omitted, and the profile reports it as ``not_measured``
    rather than assigning it a size - an unmeasured payload is unknown, not small.

    Every tool is measured twice and the two readings must agree. That guard is the point: a
    volatile field added to a reply later would otherwise reintroduce the non-convergence quietly,
    as a CI failure on an unrelated commit, and the reader would have no way to know why.
    """

    async def one(tool: str, args: dict[str, Any]) -> int:
        async with Client(server.mcp) as client:
            try:
                result = await client.call_tool(tool, args)
            except Exception:
                return -1
            payload = _stabilise(result.structured_content)
            return len(json.dumps(payload, default=str).encode())

    def measure(tool: str, args: dict[str, Any]) -> int:
        server.set_runtime(FakeRuntime(extra_tables=_CHAIN_TABLES))
        return asyncio.run(one(tool, args))

    sizes: dict[str, int] = {}
    for tool, args in sorted(_ARGS.items()):
        if tool in _NEEDS_OUTPUT_DIR or tool in _SELF_REFERENTIAL:
            continue
        size = measure(tool, args)
        if size < 0:
            continue
        again = measure(tool, args)
        if again != size:
            raise SystemExit(
                f"{tool} measured {size} then {again} bytes on identical input, so this profile "
                f"cannot be regenerated reproducibly. Its reply carries a value that varies "
                f"between runs; add that field to _VOLATILE_FIELDS if it is a wall-clock reading, "
                f"or to _SELF_REFERENTIAL if the reply depends on this file."
            )
        sizes[tool] = size
    return sizes


def build() -> PerformanceProfile:
    registered = sorted(_GROWTH)
    payloads = _measure_payloads()

    tools: list[ToolCost] = []
    for tool in registered:
        growth = _GROWTH[tool]
        measured = payloads.get(tool)
        tools.append(
            ToolCost(
                tool=tool,
                growth=growth,
                growth_basis=_BASIS[growth],
                fixture_payload_bytes=measured,
                payload_measurement="measured" if measured is not None else "not_measured",
                fixture_statements=None,
                statements_measurement="not_measured",
                bounds=_bounds_for(tool, growth),
                shaped=tool in _SHAPED,
                notes=_NOTES.get(tool, []),
            )
        )

    totals: dict[str, int] = {}
    for entry in tools:
        totals[entry.growth] = totals.get(entry.growth, 0) + 1

    unmeasured = sorted(t.tool for t in tools if t.payload_measurement == "not_measured")
    caveats = [
        "fixture_payload_bytes is a floor, not a forecast. The fixtures hold roughly one object "
        "per type, so the number shows the fixed overhead of a reply's shape and says nothing "
        "about per-row cost on a real system.",
        "Wall-clock durations in a reply (duration_ms, time_used_ms) are zeroed before the payload "
        "is measured. They are real measurements, but they are a property of the machine that ran "
        "the call rather than of the reply's shape, and leaving them in made the recorded size "
        "differ between two runs on identical input.",
        "growth is declared from the code and cites the constant that bounds it. It is not "
        "extrapolated from the fixture measurement - a one-object fixture cannot demonstrate what "
        "four thousand objects do.",
        "statement counts are not measured. Budget charging lives in ReadOnlyConnection and the "
        "offline fixtures substitute their own connection, so no statement count is observable "
        "offline. Reported as not_measured rather than as zero.",
        "observed latencies come from one reference system on one day, at one data volume. They "
        "are evidence that a call completes, not a service level.",
    ]
    if unmeasured:
        caveats.append(
            f"{len(unmeasured)} tool(s) could not be measured against the fixtures "
            f"({', '.join(unmeasured)}), so their payload is unknown rather than small."
        )
    if _SELF_REFERENTIAL:
        caveats.append(
            f"{', '.join(sorted(_SELF_REFERENTIAL))} read this profile, so measuring their payload "
            "would depend on the file this run writes and regeneration could not converge. Left "
            "unmeasured deliberately rather than pinned to a value that is wrong by construction."
        )

    return PerformanceProfile(
        server_version=version("mcp-server-sapbw"),
        budget={
            "max_queries_per_call": str(DEFAULT_MAX_QUERIES),
            "max_seconds_per_call": str(int(DEFAULT_MAX_SECONDS)),
            "override": "SAPBW_MAX_QUERIES_PER_CALL / SAPBW_MAX_SECONDS_PER_CALL",
            "on_exhaustion": (
                "a BudgetResult naming what was spent and which section stopped, never a hang or "
                "a silent truncation"
            ),
        },
        tools=tools,
        totals=totals,
        observed={
            "bw_analyze_object": "20.4s at depth 2 (lineage 11s, calc-view consumers 4.4s)",
            "bw_create_snapshot": "1.9s for 4,542 objects and 1,269 edges",
            "bw_get_transformation": "80 KiB via the bw:// resource (unshaped by design)",
            "bw_get_query": "37-67 KiB via the bw:// resource (unshaped by design)",
            "bw_get_lineage": (
                "233.8s for depth 3 both directions on a production hub object (308 nodes, "
                "692 edges, 1,624 statements), inside the 300s default. The widest call measured; "
                "a leaf object at the same depth costs a small fraction of it"
            ),
        },
        caveats=caveats,
    )


# --- rendering ----------------------------------------------------------------------------


def _render_doc(profile: PerformanceProfile) -> str:
    lines = [
        "# Performance profile",
        "",
        "<!-- GENERATED by scripts/performance_profile.py - do not edit by hand. -->",
        "",
        "What each tool costs, and what a larger system does to that cost. Ask "
        "`bw_performance_profile` for the same data; it needs no connection.",
        "",
        f"Build `{profile.server_version}`. Every call runs inside a budget of "
        f"**{profile.budget['max_queries_per_call']} statements / "
        f"{profile.budget['max_seconds_per_call']}s**, overridable with "
        f"`{profile.budget['override']}`. On exhaustion: {profile.budget['on_exhaustion']}.",
        "",
        "## Growth classes",
        "",
        "| Class | Tools | What it means |",
        "|---|--:|---|",
    ]
    for growth in ("constant", "per_page", "per_object", "per_graph_node", "per_system"):
        count = profile.totals.get(growth, 0)
        lines.append(f"| `{growth}` | {count} | {_BASIS[growth]} |")

    lines += [
        "",
        "`per_system` is the row to read before a large run. Those calls scan a class of objects "
        "rather than one object, and each names the cap that stops it.",
        "",
        "## Per tool",
        "",
        "| Tool | Growth | Fixture payload | Shaped | Bounds |",
        "|---|---|--:|:-:|---|",
    ]
    for entry in profile.tools:
        payload = (
            f"{entry.fixture_payload_bytes:,} B"
            if entry.fixture_payload_bytes is not None
            else "not measured"
        )
        bounds = "; ".join(b.bound for b in entry.bounds)
        lines.append(
            f"| `{entry.tool}` | `{entry.growth}` | {payload} | "
            f"{'yes' if entry.shaped else '—'} | {bounds} |"
        )

    lines += ["", "## Observed on the reference system", ""]
    for tool, note in sorted(profile.observed.items()):
        lines.append(f"- `{tool}`: {note}")

    lines += ["", "## What these numbers are not", ""]
    lines.extend(f"- {caveat}" for caveat in profile.caveats)
    lines.append("")
    return "\n".join(lines)


def _payload(profile: PerformanceProfile) -> str:
    data = {
        "_comment": "GENERATED by scripts/performance_profile.py - do not edit by hand.",
        **profile.model_dump(mode="json"),
    }
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    if argv and not check:
        print(f"usage: {Path(__file__).name} [--check]", file=sys.stderr)
        return 2

    profile = build()
    data_text = _payload(profile)
    doc_text = _render_doc(profile)

    if check:
        stale: list[str] = []
        for path, text in ((DATA_PATH, data_text), (DOC_PATH, doc_text)):
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current.replace("\r\n", "\n") != text:
                stale.append(str(path.relative_to(_ROOT)))
        if stale:
            print(f"performance profile is stale: {', '.join(stale)}", file=sys.stderr)
            print("regenerate with: python scripts/performance_profile.py", file=sys.stderr)
            return 1
        print("performance profile is current.")
        return 0

    DATA_PATH.write_text(data_text, encoding="utf-8")
    DOC_PATH.write_text(doc_text, encoding="utf-8")
    print(f"wrote {DATA_PATH.relative_to(_ROOT)} and {DOC_PATH.relative_to(_ROOT)}")
    for growth, count in sorted(profile.totals.items()):
        print(f"  {growth:16} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
