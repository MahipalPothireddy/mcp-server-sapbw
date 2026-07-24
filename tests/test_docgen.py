"""Tests for the documentation generator (B10), offline against a minimal scripted landscape.

Synthetic names only. Verifies the mission Section 8 structure is produced, the gaps register is
non-empty, generated descriptions are marked, output-dir safety refuses a tracked location, and
pages carry Mermaid diagrams and source-table citations.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mcp_server_sapbw.models.capability import CapabilityRecord, TableStatus
from mcp_server_sapbw.models.description import Description
from mcp_server_sapbw.models.lineage import LineageEdge, LineageGraph, LineageNode
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.services.docgen import (
    GENERATED_MARKER,
    DocGenerator,
    DocGenError,
    _render_description,
    _safe_output_dir,
    _slug,
)

SCHEMA = "TESTSCHEMA"
_SYS_TABLES = {"object_dependencies": "OBJECT_DEPENDENCIES", "hana_views": "VIEWS"}
_ABAP_TABLES = {
    "chain_attr": "RSPCCHAINATTR",
    "chain_text": "RSPCCHAINT",
    "chain_edges": "RSPCCHAIN",
    "log_chain": "RSPCLOGCHAIN",
    "process_log": "RSPCPROCESSLOG",
    "transformation": "RSTRAN",
    "transformation_rule": "RSTRANRULE",
    "transformation_field": "RSTRANFIELD",
    "transformation_step_rout": "RSTRANSTEPROUT",
    "routine_source": "RSAABAP",
    "dtp": "RSBKDTP",
    "datasource": "RSDS",
    "datasource_field": "RSDSSEGFD",
    "extractor": "ROOSOURCE",
    "dso_header": "RSDODSO",
    "dso_text": "RSDODSOT",
    "dso_field": "RSDODSOIOBJ",
    "infoobject": "RSDIOBJ",
    "query_dir": "RSZCOMPDIR",
    "query_provider": "RSZCOMPIC",
    "element_dir": "RSZELTDIR",
    "element_xref": "RSZELTXREF",
    "element_text": "RSZELTTXT",
}


class ScriptedConnection:
    """Minimal landscape: one transformation DS1 --TR1--> DSO1 (a DSO). Everything else empty."""

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if "RSTRANSTEPROUT" in sql or "RSTRANRULE" in sql or "RSTRANFIELD" in sql:
            return []
        if "RSTRAN" in sql:
            return self._rstran(sql)
        return []  # all other tables empty in this minimal landscape

    @staticmethod
    def _rstran(sql: str) -> list[tuple[Any, ...]]:
        if "TOTAL_COUNT" in sql:
            return [(1,)]
        if "OBJSTAT" in sql:  # get_transformation header (12 cols)
            return [("ACT", "RSDS", "", "DS1", "ODSO", "", "DSO1", "", "", "", "", "")]
        # analyzer scans + lineage edge queries -> empty
        if (
            "SOURCETYPE = ?" in sql
            or "SOURCETYPE IN ('ODSO', 'ADSO')" in sql
            or "COUNT(DISTINCT SOURCENAME)" in sql
            or "STARTROUTINE <> ''" in sql
            or "SOURCENAME = ?" in sql
            or "TARGETNAME = ?" in sql
        ):
            return []
        return [("TR1", "RSDS", "DS1", "ODSO", "DSO1", "", "", "")]  # list (8 cols)


def _capability() -> CapabilityRecord:
    tables = {
        logical: TableStatus(
            logical_name=logical, resolved_name=physical, present=True, schema_name=SCHEMA
        )
        for logical, physical in _ABAP_TABLES.items()
    }
    tables.update(
        {
            logical: TableStatus(
                logical_name=logical, resolved_name=physical, present=True, schema_name="SYS"
            )
            for logical, physical in _SYS_TABLES.items()
        }
    )
    return CapabilityRecord(
        system="qa",
        bw_release="7.50",
        abap_schema=SCHEMA,
        discovered_at=datetime.now(UTC),
        object_models={"classic_dso": True},
        tables=tables,
    )


def _generator() -> DocGenerator:
    return DocGenerator(ScriptedConnection(), _capability())


# --- pure-helper unit tests --------------------------------------------------------------


def test_slug_is_filesystem_safe() -> None:
    assert _slug("/BIC/" + "A0THING") == "BIC_A0THING"
    assert _slug("") == "object"


def test_render_description_marks_generated() -> None:
    generated = Description(
        description_long="A synthesized summary.", origin="generated", quality_flag="missing"
    )
    stored = Description(description_long="A real stored text.", origin="stored", quality_flag="ok")
    assert GENERATED_MARKER in _render_description(generated)
    assert GENERATED_MARKER not in _render_description(stored)


def test_safe_output_dir_rejects_tracked_repo(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    with pytest.raises(DocGenError):
        _safe_output_dir(repo_root / "docs_scratch_should_not_exist")
    # A path outside the repo is allowed.
    out = _safe_output_dir(tmp_path / "kb")
    assert out.is_dir()


def test_mermaid_renders_nodes_and_advisory_edges() -> None:
    prov = Provenance(source_table="RSTRAN")
    graph = LineageGraph(
        root_id="dso:DSO1",
        direction="both",
        depth=1,
        nodes=[
            LineageNode(id="dso:DSO1", object_type="dso", name="DSO1", provenance=prov),
            LineageNode(id="dso:LOOKUP", object_type="dso", name="LOOKUP", provenance=prov),
        ],
        edges=[
            LineageEdge(
                src="dso:LOOKUP",
                dst="dso:DSO1",
                kind="routine_lookup",
                confidence="advisory",
                provenance=prov,
            )
        ],
    )
    mermaid = DocGenerator._mermaid(graph)
    assert "flowchart LR" in mermaid
    assert "DSO1" in mermaid and "LOOKUP" in mermaid
    assert "-.->" in mermaid  # advisory edge is dashed


# --- integration test --------------------------------------------------------------------


def test_generate_full_structure(tmp_path: Path) -> None:
    result = _generator().generate(tmp_path / "kb", limit=5)
    base = Path(result.output_dir)

    # Every mission Section 8 section index plus the root and the gaps register exist.
    for rel in (
        "index.md",
        "01-inventory/index.md",
        "02-process-chains/index.md",
        "03-lineage/index.md",
        "04-providers/index.md",
        "05-transformations/index.md",
        "06-queries/index.md",
        "07-hana/index.md",
        "08-scenarios/index.md",
        "99-gaps-and-risks.md",
    ):
        assert (base / rel).is_file(), f"missing {rel}"

    # Manifest is consistent and the gaps register is never empty (mission acceptance).
    assert result.page_count == len(result.files)
    assert result.gaps_count > 0
    gaps = (base / "99-gaps-and-risks.md").read_text(encoding="utf-8")
    assert "Standing limitations" in gaps
    assert "Routine analysis" in gaps

    # A lineage page carries a Mermaid diagram; pages carry source-table citations.
    lineage_page = base / "03-lineage" / f"{_slug('DSO1')}.md"
    assert lineage_page.is_file()
    assert "```mermaid" in lineage_page.read_text(encoding="utf-8")
    assert "_Source tables:" in (base / "01-inventory/index.md").read_text(encoding="utf-8")

    # All eight scenarios plus layer violations were rendered.
    for scenario in ("9_1", "9_2", "9_3", "9_4", "9_5", "9_6", "9_7", "9_8", "layer_violations"):
        assert (base / "08-scenarios" / f"{scenario}.md").is_file()


def test_generate_returns_manifest_outside_repo(tmp_path: Path) -> None:
    result = _generator().generate(tmp_path / "kb2", limit=2)
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in Path(result.output_dir).parents
    assert result.generated_at.tzinfo is not None
