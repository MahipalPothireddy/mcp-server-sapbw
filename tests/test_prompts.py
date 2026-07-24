"""Tests for the analyst-workflow prompts (B9), rendered offline (B11 coverage)."""

from __future__ import annotations

from mcp_server_sapbw.prompts import workflows


def test_every_prompt_renders_with_system_and_tool_references() -> None:
    system = "qa"
    cases = [
        (workflows.analyze_impact(system, "OBJ1"), "bw_impact_analysis", "OBJ1"),
        (workflows.troubleshoot_missing_data(system, "RPT1"), "bw_get_lineage", "RPT1"),
        (workflows.document_dataflow(system, "OBJ2"), "bw_trace_to_source", "OBJ2"),
        (workflows.review_scenario(system, "9.1"), "bw_review_scenario", "9.1"),
        (workflows.onboard_analyst(system, "FI"), "bw_search_objects", "FI"),
        (workflows.pre_change_checklist(system, "OBJ3"), "bw_impact_analysis", "OBJ3"),
    ]
    for text, tool_ref, argument in cases:
        assert isinstance(text, str)
        assert system in text
        assert tool_ref in text  # composes the right read-only tool
        assert argument in text  # threads the caller's argument through


def test_register_prompts_is_idempotent_shape() -> None:
    # The registration helper iterates the six workflow callables; calling the functions is what
    # the render test above exercises. Here we simply assert the module exposes all six.
    for name in (
        "analyze_impact",
        "troubleshoot_missing_data",
        "document_dataflow",
        "review_scenario",
        "onboard_analyst",
        "pre_change_checklist",
    ):
        assert callable(getattr(workflows, name))
