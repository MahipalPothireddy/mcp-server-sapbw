"""Every registered tool, called over the protocol at least once.

Two things this gives that no other test does.

**Protocol-level coverage for every tool.** The rest of the suite tests repositories and services
directly and reaches the tool boundary for a subset. A tool can therefore be wired wrong -- a
parameter name that does not match what the reader expects, a return annotation FastMCP cannot build
a schema for, an exception escaping the failure envelope -- and no test would notice. Here every
tool is invoked through a real client and must come back as a structured result.

**The measurement the support matrix rests on.** ``scripts/support_matrix.py`` learns what each tool
needs by attributing reads to the tool that caused them while this suite runs. A tool never invoked
at the tool boundary has no measurement, and the matrix has to report it as ``unknown`` -- so
without this file, 22 of the 56 tools were unanswerable, which is a poor matrix regardless of how
honest it is about being poor.

``_ARGS`` must name every registered tool. That is asserted, so adding a tool without adding it here
fails rather than quietly shrinking the matrix.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from mcp_server_sapbw import server
from tests.test_server import _CHAIN_TABLES, FakeRuntime

_SYSTEM = "qa"

#: Minimal arguments per tool, using the identifiers the fixture connection answers for. The point
#: is one successful invocation each, not exercising every branch -- the per-domain test modules do
#: that. Where the fixture cannot produce a real object the call still runs and returns a documented
#: failure, which is itself worth asserting.
_ARGS: dict[str, dict[str, Any]] = {
    # --- system and support -------------------------------------------------------------
    "bw_list_systems": {},
    "bw_system_profile": {"system": _SYSTEM},
    "bw_refresh_capabilities": {"system": _SYSTEM},
    "bw_capability_report": {"system": _SYSTEM},
    "bw_access_report": {"system": _SYSTEM},
    "bw_support_matrix": {},
    "bw_performance_profile": {},
    "bw_assess_landscape": {"system": _SYSTEM, "limit_per_scenario": 5},
    "bw_list_business_areas": {"system": _SYSTEM},
    "bw_cache_status": {"system": _SYSTEM},
    "bw_refresh_cache": {"system": _SYSTEM},
    # --- snapshots ----------------------------------------------------------------------
    "bw_create_snapshot": {"system": _SYSTEM, "families": ["providers"], "keep": False},
    "bw_list_snapshots": {"system": _SYSTEM},
    "bw_compare_snapshots": {"system": _SYSTEM, "left": "qa-19700101T000000Z"},
    "bw_compare_systems": {
        "left_system": _SYSTEM,
        "right_system": _SYSTEM,
        "families": ["providers"],
    },
    # --- objects and search -------------------------------------------------------------
    "bw_search_objects": {"system": _SYSTEM, "pattern": "SALES"},
    "bw_describe_object": {"system": _SYSTEM, "name": "SALES_DSO"},
    # --- chains -------------------------------------------------------------------------
    "bw_list_chains": {"system": _SYSTEM},
    "bw_get_chain": {"system": _SYSTEM, "chain_id": "DAILY_LOAD"},
    "bw_get_chain_runtimes": {"system": _SYSTEM, "chain_id": "DAILY_LOAD"},
    "bw_get_schedule_matrix": {"system": _SYSTEM},
    "bw_get_load_closure": {"system": _SYSTEM, "chain_id": "DAILY_LOAD"},
    # --- transformations and routines ----------------------------------------------------
    "bw_list_transformations": {"system": _SYSTEM},
    "bw_get_transformation": {"system": _SYSTEM, "transformation_id": "TR1"},
    "bw_get_routine_code": {"system": _SYSTEM, "transformation_id": "TR1"},
    "bw_analyze_routine": {"system": _SYSTEM, "transformation_id": "TR1"},
    "bw_get_routine_register": {"system": _SYSTEM},
    # --- lineage ------------------------------------------------------------------------
    "bw_get_lineage": {"system": _SYSTEM, "name": "ADSO_T", "depth": 2},
    "bw_impact_analysis": {"system": _SYSTEM, "name": "ADSO_T", "depth": 2},
    "bw_trace_to_source": {"system": _SYSTEM, "name": "ADSO_T", "depth": 3},
    # --- queries ------------------------------------------------------------------------
    "bw_list_queries": {"system": _SYSTEM},
    "bw_get_query": {"system": _SYSTEM, "query": "QRY1"},
    "bw_get_query_lineage": {"system": _SYSTEM, "query": "QRY1"},
    "bw_get_query_usage": {"system": _SYSTEM, "query": "QRY1"},
    "bw_get_query_auth_exposure": {"system": _SYSTEM, "query": "QRY1"},
    # --- HANA ---------------------------------------------------------------------------
    "bw_list_calc_views": {"system": _SYSTEM},
    "bw_get_calc_view_lineage": {"system": _SYSTEM, "view_name": "CV1"},
    "bw_get_calc_view_logic": {"system": _SYSTEM, "view_name": "PKG.SUB/CV1"},
    "bw_get_hana_crossings": {"system": _SYSTEM},
    # --- provider health, sources, 3.x ---------------------------------------------------
    "bw_get_provider_health": {"system": _SYSTEM, "provider": "SALES_DSO"},
    "bw_get_source_systems": {"system": _SYSTEM},
    "bw_list_extractor_enhancements": {"system": _SYSTEM},
    "bw_get_extractor_exit_code": {},
    "bw_list_3x_flows": {"system": _SYSTEM},
    "bw_get_transfer_rules": {"system": _SYSTEM, "transfer_structure": "TS1"},
    "bw_list_update_rules": {"system": _SYSTEM},
    # --- security -----------------------------------------------------------------------
    "bw_security_overview": {"system": _SYSTEM},
    "bw_list_analysis_auths": {"system": _SYSTEM},
    # AUTH_* rather than the Z* an authorisation would really carry: Z is a customer namespace, and
    # the leak scan is right to reject it even in a fixture. tests/test_security.py uses the same
    # synthetic form.
    "bw_get_analysis_auth": {"system": _SYSTEM, "name": "AUTH_REGION"},
    # --- analyzers ----------------------------------------------------------------------
    "bw_check_load_latency": {"system": _SYSTEM},
    "bw_check_schedule_risk": {"system": _SYSTEM},
    "bw_find_layer_violations": {"system": _SYSTEM},
    "bw_find_unused_providers": {"system": _SYSTEM},
    "bw_review_scenario": {"system": _SYSTEM, "scenario": "9.3"},
    # --- compound analysis --------------------------------------------------------------
    "bw_analyze_object": {"system": _SYSTEM, "name": "SALES_DSO", "depth": 1},
    "bw_analyze_query": {"system": _SYSTEM, "query": "QRY1"},
    "bw_analyze_process_chain": {"system": _SYSTEM, "chain_id": "DAILY_LOAD"},
    "bw_assess_change_impact": {"system": _SYSTEM, "name": "ADSO_T", "depth": 1},
    "bw_troubleshoot_missing_data": {"system": _SYSTEM, "target": "QRY1"},
}

#: Tools that write to disk, so they need a directory rather than the default output location.
_NEEDS_OUTPUT_DIR = {"bw_generate_docs", "bw_render_lineage"}


async def _names() -> list[str]:
    async with Client(server.mcp) as client:
        return sorted(tool.name for tool in await client.list_tools())


def _call(tool: str, args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def _body(result: Any) -> Any:
    payload = result.structured_content
    return payload["result"] if isinstance(payload, dict) and "result" in payload else payload


def test_every_registered_tool_has_arguments_here() -> None:
    """A new tool must be added to `_ARGS`, or it silently loses its only protocol-level test."""
    registered = set(asyncio.run(_names()))
    covered = set(_ARGS) | _NEEDS_OUTPUT_DIR
    assert registered - covered == set(), f"not covered: {sorted(registered - covered)}"
    assert covered - registered == set(), (
        f"covered but not registered: {sorted(covered - registered)}"
    )


@pytest.mark.parametrize("tool", sorted(_ARGS))
def test_tool_is_callable_over_the_protocol(tool: str) -> None:
    """Each tool returns a structured result rather than raising through the transport.

    Content is not asserted here: what a tool answers on a synthetic fixture is the business of its
    own test module. What is asserted is that the call completes and the reply is shaped -- which
    catches a mismatched parameter name, a return type FastMCP cannot schematise, and any exception
    escaping the failure envelope.
    """
    server.set_runtime(FakeRuntime(extra_tables=_CHAIN_TABLES))
    body = _body(_call(tool, _ARGS[tool]))
    assert body is not None, f"{tool} returned no structured content"
    if isinstance(body, dict) and "status" in body:
        # A documented failure is a valid outcome on a fixture that has no such object; an
        # undocumented one is not.
        assert body.get("code"), f"{tool} failed without an error code: {body}"


@pytest.mark.parametrize("tool", sorted(_NEEDS_OUTPUT_DIR))
def test_file_writing_tool_is_callable_over_the_protocol(tool: str, tmp_path: Path) -> None:
    server.set_runtime(FakeRuntime(extra_tables=_CHAIN_TABLES))
    args: dict[str, Any] = {"system": _SYSTEM, "output_dir": str(tmp_path)}
    if tool == "bw_render_lineage":
        args |= {"name": "ADSO_T", "depth": 2}
    body = _body(_call(tool, args))
    assert body is not None


# --- the README has to describe what is actually registered --------------------------------
#
# Three tools (bw_list_3x_flows, bw_get_transfer_rules, bw_list_update_rules) were registered,
# callable and never mentioned in the README, and two tool counts were a release behind. The
# catalogue is the first thing a customer reads, and nothing re-derives it. These assert it.

_README = Path(__file__).resolve().parents[1] / "README.md"


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


async def _surface() -> tuple[set[str], set[str], set[str]]:
    async with Client(server.mcp) as client:
        tools = {t.name for t in await client.list_tools()}
        templates = {str(t.uriTemplate) for t in await client.list_resource_templates()}
        templates |= {str(r.uri) for r in await client.list_resources()}
        prompts = {p.name for p in await client.list_prompts()}
    return tools, templates, prompts


def test_readme_documents_every_registered_tool() -> None:
    tools, _, _ = asyncio.run(_surface())
    text = _readme()
    missing = sorted(name for name in tools if f"`{name}`" not in text)
    assert missing == [], f"registered but absent from README.md: {missing}"


def test_readme_documents_every_prompt_and_resource() -> None:
    _, templates, prompts = asyncio.run(_surface())
    text = _readme()
    missing_prompts = sorted(name for name in prompts if f"`{name}`" not in text)
    assert missing_prompts == [], f"prompts absent from README.md: {missing_prompts}"

    # A template is documented by its path shape; the {placeholder} names may differ in prose.
    missing_uris = sorted(
        uri for uri in templates if uri.split("://")[-1].split("/", 1)[-1].split("/")[0] not in text
    )
    assert missing_uris == [], f"resource kinds absent from README.md: {missing_uris}"


def test_readme_headline_counts_are_current() -> None:
    """The opening summary is a claim, and it was two behind.

    Only the sentences that state a *total* are checked. A per-release verdict count ("40 tools
    verified") is a different number that legitimately differs, so this asserts the exact phrasings
    rather than scanning for any digit followed by "tools".
    """
    tools, templates, prompts = asyncio.run(_surface())
    text = _readme()
    summary = (
        f"**{len(tools)} tools, {len(templates)} resource templates and {len(prompts)} prompts**"
    )
    assert summary in text, f"the opening summary does not read {summary!r}"
    assert f"across {len(tools)} tools" in text, (
        f"the support-matrix paragraph does not say 'across {len(tools)} tools'"
    )


def test_readme_names_no_tool_that_does_not_exist() -> None:
    """The other direction: a catalogue row for a removed tool reads as a working feature."""
    tools, _, _ = asyncio.run(_surface())
    # Only the catalogue tables are checked. Elsewhere a `bw_`-prefixed token may legitimately be a
    # field value rather than a tool - `bw_provider_view` is a lineage resolution basis, not a tool.
    rows = re.findall(r"^\|\s*`(bw_[a-z0-9_]+)`\s*\|", _readme(), flags=re.MULTILINE)
    ghosts = sorted(set(rows) - tools)
    assert ghosts == [], f"README catalogue lists non-existent tool(s): {ghosts}"
