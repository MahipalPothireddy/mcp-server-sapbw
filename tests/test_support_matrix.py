"""The support matrix: shipped, offline, and honest about what it does not know.

The tests fall into three groups, each guarding a different way this artefact could mislead.

**It has to be answerable without a connection.** That is the whole reason it exists, so a test
calls the tool with no system named and asserts a full answer comes back.

**It has to stay in step with the code.** A generated file committed to the repo drifts the moment
someone adds a tool, so the staleness check is a test rather than only a CI step, and the tool list
is compared against the live registration rather than a maintained copy.

**It must never turn "we did not check" into "it works".** ``unverified`` has to survive for every
release with no evidence, ``requires`` has to be labelled a lower bound, and no verdict may be
spelled ``supported``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from mcp_server_sapbw import server
from mcp_server_sapbw.core.contract import contract
from mcp_server_sapbw.core.support import support_matrix
from mcp_server_sapbw.models.capability import VALIDATION_RANK
from mcp_server_sapbw.models.support import SupportMatrix
from tests.test_server import FakeRuntime

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "src" / "mcp_server_sapbw" / "data" / "support_matrix.json"
_DOC = _ROOT / "docs" / "support-matrix.md"

# The build scripts are not part of the installed package, so they are imported by path. Both are
# needed here to assert the two generated artefacts agree about the verification release.
sys.path.insert(0, str(_ROOT / "scripts"))
import capability_contract  # noqa: E402
import support_matrix as matrix_script  # noqa: E402


def _matrix() -> SupportMatrix:
    loaded = support_matrix()
    assert loaded is not None, "the shipped support matrix is missing"
    return loaded


def _call(args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp) as client:
            return await client.call_tool("bw_support_matrix", args)

    result = asyncio.run(run())
    payload = result.structured_content
    return payload["result"] if isinstance(payload, dict) and "result" in payload else payload


# --- it ships, and it answers without a system --------------------------------------------


def test_the_matrix_is_shipped_inside_the_package() -> None:
    """Read through importlib.resources, so an installed wheel behaves like a source checkout."""
    matrix = _matrix()
    assert matrix.tools
    assert matrix.releases
    assert matrix.server_version


def test_the_tool_answers_with_no_system_named() -> None:
    """The reason this exists: the question arrives before there is a profile to connect with."""
    body = _call({})
    assert body["tools"], "no tools came back"
    assert body["releases"]
    assert body["caveats"]


def test_filtering_by_tool_returns_that_tool_only() -> None:
    body = _call({"tool": "bw_get_chain_runtimes"})
    assert [t["tool"] for t in body["tools"]] == ["bw_get_chain_runtimes"]


def test_filtering_by_release_narrows_the_verdicts_and_the_totals() -> None:
    body = _call({"release": "BW 7.50"})
    assert [r["release"] for r in body["releases"]] == ["BW 7.50"]
    assert set(body["totals"]) == {"BW 7.50"}
    for entry in body["tools"]:
        assert set(entry["releases"]) == {"BW 7.50"}


def test_an_unknown_tool_is_reported_as_not_found() -> None:
    body = _call({"tool": "bw_does_not_exist"})
    assert body["code"] == "object_not_found"


def test_an_unknown_release_names_the_ones_this_build_knows() -> None:
    body = _call({"release": "BW 9.9"})
    assert body["code"] == "invalid_argument"
    assert "BW 7.50" in body["message"]


def test_naming_a_system_crosses_the_matrix_with_that_system() -> None:
    """Optional sharpening: the shipped matrix cannot know a customer's release, its record can."""
    server.set_runtime(FakeRuntime())
    body = _call({"tool": "bw_get_chain_runtimes", "system": "qa"})
    assert any("crossed with qa" in caveat for caveat in body["caveats"])


def test_crossing_with_a_system_does_not_upgrade_a_release_verdict() -> None:
    """Presence is not evidence the tool was ever run there, so the verdict must not move."""
    server.set_runtime(FakeRuntime())
    offline = _call({"tool": "bw_get_chain"})["tools"][0]["releases"]
    crossed = _call({"tool": "bw_get_chain", "system": "qa"})["tools"][0]["releases"]
    assert offline == crossed


# --- it stays in step with the code -------------------------------------------------------


def test_the_matrix_covers_exactly_the_registered_tools() -> None:
    """Compared against live registration, not a maintained list, so a new tool cannot be missed."""

    async def names() -> set[str]:
        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    registered = asyncio.run(names())
    listed = {entry.tool for entry in _matrix().tools}
    assert listed == registered, f"drift: {sorted(listed ^ registered)}"


def test_every_requirement_is_a_declared_capability() -> None:
    """A requirement naming nothing in the contract would be unresolvable for a reader."""
    declared = set(contract())
    for entry in _matrix().tools:
        unknown = [name for name in entry.requires if name not in declared]
        assert not unknown, f"{entry.tool} requires undeclared {unknown}"


def test_the_data_file_and_the_doc_list_the_same_tools() -> None:
    """One matrix, two renderings. A wheel must not disagree with the docs it shipped with.

    Deliberately **not** by invoking ``scripts/support_matrix.py --check``: establishing the
    per-tool measurement means running the suite in a subprocess, so a test that regenerated the
    matrix would run the suite containing it, and that suite would regenerate again. Staleness is a
    CI step for exactly that reason - the same arrangement the capability contract uses - and what
    is asserted here is the property that matters day to day.
    """
    doc = _DOC.read_text(encoding="utf-8")
    in_doc = set(re.findall(r"^\| `(bw_[a-z0-9_]+)` \|", doc, flags=re.MULTILINE))
    assert in_doc == {entry.tool for entry in _matrix().tools}


def test_the_verified_release_agrees_with_the_capability_contract() -> None:
    """Two files claiming different verification releases is drift a customer would find first."""
    assert matrix_script.VERIFIED_RELEASE in capability_contract.LIVE_VERIFIED_RELEASE
    assert matrix_script.VERIFIED_ON in capability_contract.LIVE_VERIFIED_RELEASE


# --- it never turns "not checked" into "works" ---------------------------------------------


def test_no_verdict_is_spelled_supported() -> None:
    """The word carries a promise none of these values makes; every verdict names its basis."""
    verdicts = {v for entry in _matrix().tools for v in entry.releases.values()}
    assert "supported" not in verdicts
    assert verdicts <= {"verified", "expected", "unverified", "needs_connector", "unknown"}


def test_an_unverified_release_reports_unverified_for_every_bw_tool() -> None:
    """Absence of evidence stays visible. Only a connector-gated tool differs, and says why."""
    matrix = _matrix()
    unverified = [r.release for r in matrix.releases if r.evidence == "not_verified"]
    assert unverified, "the matrix lists no unverified release, so silence could read as coverage"
    for release in unverified:
        for entry in matrix.tools:
            verdict = entry.releases[release]
            assert verdict in ("unverified", "needs_connector", "unknown"), (
                f"{entry.tool} claims {verdict!r} on {release}, which has no evidence"
            )


def test_exactly_one_release_is_verified_and_it_names_the_system() -> None:
    verified = [r for r in _matrix().releases if r.evidence == "verified"]
    assert len(verified) == 1
    assert verified[0].verified_on, "a verification claim without a system is not a claim"


def test_the_lower_bound_of_the_requirement_list_is_stated() -> None:
    """A requirement list read as complete would be used to rule things out. It cannot be."""
    joined = " ".join(_matrix().caveats).lower()
    assert "lower bound" in joined


def test_the_capabilities_that_decide_an_unverified_release_are_named() -> None:
    """The actionable part: which capabilities a customer should check on their own release."""
    for release in _matrix().releases:
        assert release.release_conditional, f"{release.release} names nothing to check"


def test_a_measured_empty_requirement_set_is_not_the_same_as_unmeasured() -> None:
    """`bw_list_systems` reads no BW metadata; that is a measurement, not a gap."""
    entry = _matrix().tool("bw_list_systems")
    assert entry is not None
    assert entry.measurement == "measured"
    assert entry.reads_nothing is True
    assert entry.note, "a tool that reads nothing should say why"


def test_nothing_is_left_unmeasured() -> None:
    """Every tool is invoked at the tool boundary by tests/test_tool_surface.py.

    Asserted so that deleting that coverage shows up here as a weakened matrix rather than as a
    quietly growing column of `unknown`.
    """
    unmeasured = [t.tool for t in _matrix().tools if t.measurement == "not_measured"]
    assert not unmeasured, f"no measurement for: {unmeasured}"


def test_a_tool_is_only_as_validated_as_its_weakest_requirement() -> None:
    """A minimum, not a summary - averaging would let one verified capability hide four unproven."""
    entries = contract()
    for entry in _matrix().tools:
        if not entry.requires:
            continue
        weakest = min(
            VALIDATION_RANK[entries[name].validation] for name in entry.requires if name in entries
        )
        assert VALIDATION_RANK[entry.validation] == weakest, entry.tool


def test_the_totals_agree_with_the_rows() -> None:
    matrix = _matrix()
    for release, counts in matrix.totals.items():
        expected: dict[str, int] = {}
        for entry in matrix.tools:
            verdict = entry.releases[release]
            expected[verdict] = expected.get(verdict, 0) + 1
        assert counts == expected, release


def test_the_data_file_declares_itself_generated() -> None:
    """So nobody edits it by hand and has the change overwritten on the next build."""
    payload = json.loads(_DATA.read_text(encoding="utf-8"))
    assert "GENERATED" in payload["_comment"]
    assert "GENERATED" in _DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", ["bw_get_extractor_exit_code", "bw_check_schedule_risk"])
def test_a_connector_gated_tool_says_which_connector(tool: str) -> None:
    """Working-but-unpopulated is a different outcome from failing, and is reported as such."""
    entry = _matrix().tool(tool)
    assert entry is not None
    assert entry.needs_connector
    assert set(entry.releases.values()) == {"needs_connector"}
