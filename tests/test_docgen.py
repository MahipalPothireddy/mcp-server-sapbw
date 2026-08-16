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
from mcp_server_sapbw.models.chains import LoadedProvider
from mcp_server_sapbw.models.description import Description
from mcp_server_sapbw.models.hana import HanaCrossing, HanaCrossingReport
from mcp_server_sapbw.models.lineage import LineageEdge, LineageGraph, LineageNode
from mcp_server_sapbw.models.provenance import Provenance
from mcp_server_sapbw.models.providers import AttributeRef, Provider
from mcp_server_sapbw.services.docgen import (
    GENERATED_MARKER,
    DocGenerator,
    DocGenError,
    _LoadClosure,
    _render_attributes,
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
            # write-back loop scans (self-loop / two-cycle) and the unused-provider source scan
            or "SOURCENAME = TARGETNAME" in sql
            or "SOURCENAME <> TARGETNAME" in sql
            or "GROUP BY" in sql
            # routine-register ownership scan (selects the five header routine columns)
            or "GLBCODE2" in sql
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


def test_attribute_table_distinguishes_navigable_from_kind() -> None:
    """The two disagree for an inherited attribute, and the page must not blur them."""
    prov = Provenance(source_table="RSDBCHATR")
    provider = Provider(
        name="SOLD_TO_CHA",
        object_type="infoobject",
        infoobject_kind="characteristic",
        provenance=prov,
        attributes=[
            AttributeRef(
                name="MATL_GROUP",
                kind="navigation",
                position=1,
                navigation_name="SOLD_TO_CHA__MATL_GROUP",
                navigable=True,
                inherited_from="MATERIAL_CHA",
                description="Material group",
            ),
            AttributeRef(
                name="MATL_TYPE",
                kind="navigation",
                position=2,
                navigable=False,
                time_dependent=True,
                auth_relevant=True,
                inherited_from="MATERIAL_CHA",
            ),
        ],
    )
    rendered = "\n".join(_render_attributes(provider))
    assert "## Attributes" in rendered
    assert "MATERIAL_CHA" in rendered  # the inheritance is stated on the page
    assert "| `MATL_GROUP` | Material group | navigation | yes |" in rendered
    # Navigable on the base, not exposed here: kind says navigation, navigable says no.
    assert "| `MATL_TYPE` | - | navigation | no | - | yes | yes |" in rendered


def test_attribute_table_omitted_for_objects_without_attributes() -> None:
    provider = Provider(
        name="SALES_DSO", object_type="dso", provenance=Provenance(source_table="RSDODSO")
    )
    assert _render_attributes(provider) == []


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


def _crossing(bw_object: str, resolved: str | None, kind: str | None, resolution: str) -> Any:
    return HanaCrossing(
        direction="bw_reads_hana",
        hana_object="CV_SALES",
        bw_object=bw_object,
        bw_object_resolved=resolved,
        bw_object_kind=kind,
        resolution=resolution,  # type: ignore[arg-type]
        provenance=Provenance(source_table="OBJECT_DEPENDENCIES"),
    )


def test_calc_view_consumers_table_dedupes_and_flags_verification() -> None:
    report = HanaCrossingReport(
        crossings=[
            _crossing(
                "0BW:BIA:SALES_CP:J1.CALC.1", "SALES_CP", "compositeprovider", "bw_provider_view"
            ),
            _crossing(
                "0BW:BIA:SALES_CP:J2.CALC.1", "SALES_CP", "compositeprovider", "bw_provider_view"
            ),
            _crossing("0BW:BIA:GONE_PROV", "GONE_PROV", None, "bw_provider_view"),
            _crossing("COMPAT_VIEW", None, None, "unresolved"),
        ],
        bw_reads_hana_count=4,
        total_count=4,
    )
    lines = DocGenerator._calc_view_consumers(report)
    rendered = "\n".join(lines)
    assert "Calc view -> consuming InfoProvider" in rendered
    assert rendered.count("| CV_SALES | SALES_CP |") == 1  # both calc nodes -> one row
    assert "| CV_SALES | GONE_PROV | unverified | no |" in rendered
    assert "COMPAT_VIEW" not in rendered  # not a BW provider view


def test_calc_view_consumers_table_omitted_when_none() -> None:
    assert DocGenerator._calc_view_consumers(HanaCrossingReport()) == []


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


# --- load-closure-driven selection -------------------------------------------------------
#
# The minimal landscape above has no chains, which is what exercises the fallback path. This
# second landscape has chains that actually load DTP targets, so the selection contract can be
# checked: an object a chain loads must get a page even when `limit` is small, because a cap must
# only ever cost a provider that nothing loads.

_LOADED_TABLES = {
    **_ABAP_TABLES,
    "adso_header": "RSOADSO",
    "cube_header": "RSDCUBE",
    "composite_header": "RSOHCPR",
}

# PC_MAIN --(CHAIN)--> PC_SUB, and one DTP load each. ADSO_ORPHAN exists but nothing loads it.
_CHAIN_STEPS: dict[str, list[tuple[str, str]]] = {
    "PC_MAIN": [("CHAIN", "PC_SUB"), ("DTP_LOAD", "DTP_A")],
    "PC_SUB": [("DTP_LOAD", "DTP_B")],
}
_DTP_ROWS: dict[str, tuple[str, str, str]] = {
    # dtp -> (target, target type code, UPDMODE)  F=full D=delta
    "DTP_A": ("ADSO_LOADED", "ADSO", "D"),
    "DTP_B": ("CUBE_LOADED", "CUBE", "F"),
}


class LoadedConnection:
    """A landscape with two chains, two DTP loads, and one provider nothing loads."""

    def execute_select(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        params = list(parameters or [])
        # Order matters: the longer table names must be tested before their prefixes.
        if "RSTRANSTEPROUT" in sql or "RSTRANRULE" in sql or "RSTRANFIELD" in sql:
            return []
        if "RSPCCHAINATTR" in sql:
            if "TOTAL_COUNT" in sql:
                return [(len(_CHAIN_STEPS),)]
            return [(name, "APP", "ACT") for name in sorted(_CHAIN_STEPS)]
        if "RSPCCHAINT" in sql:  # chain texts - must precede the RSPCCHAIN check
            return []
        if "RSPCLOGCHAIN" in sql:
            return []
        if "RSPCCHAIN" in sql:  # chain steps, filtered by CHAIN_ID = ?
            # Pagination appends LIMIT/OFFSET to the parameter list, so the chain id is first.
            chain = str(params[0]) if params else ""
            steps = _CHAIN_STEPS.get(chain, [])
            if "LNR" in sql:  # _build_chain wants the full step row
                return [
                    (t, v, f"{i:03d}", None, None, None) for i, (t, v) in enumerate(steps, start=1)
                ]
            return [(t, v) for t, v in steps]
        if "RSBKDTP" in sql:
            wanted = {str(p) for p in params}
            return [(dtp, *_DTP_ROWS[dtp]) for dtp in sorted(_DTP_ROWS) if dtp in wanted]
        if "RSOADSO" in sql:
            return [("ADSO_LOADED",), ("ADSO_ORPHAN",)]
        if "RSDCUBE" in sql:
            return [("CUBE_LOADED",)]
        if "RSTRAN" in sql:
            return [(1,)] if "TOTAL_COUNT" in sql else []
        return []


def _loaded_generator() -> DocGenerator:
    capability = _capability()
    for logical, physical in _LOADED_TABLES.items():
        capability.tables[logical] = TableStatus(
            logical_name=logical, resolved_name=physical, present=True, schema_name=SCHEMA
        )
    return DocGenerator(LoadedConnection(), capability)


def test_load_closure_unions_targets_through_subchains(tmp_path: Path) -> None:
    """A target reached only through a nested sub-chain still lands in the closure."""
    result = _loaded_generator().generate(tmp_path / "kb", limit=5)
    coverage = (Path(result.output_dir) / "01-inventory/load-coverage.md").read_text(
        encoding="utf-8"
    )
    # ADSO_LOADED is a direct step of PC_MAIN; CUBE_LOADED is only reachable via PC_SUB.
    assert "ADSO_LOADED" in coverage
    assert "CUBE_LOADED" in coverage


def test_load_coverage_reports_declared_update_mode(tmp_path: Path) -> None:
    """Full vs delta is read from the DTP, so it must appear verbatim rather than be inferred."""
    result = _loaded_generator().generate(tmp_path / "kb", limit=5)
    coverage = (Path(result.output_dir) / "01-inventory/load-coverage.md").read_text(
        encoding="utf-8"
    )
    adso_row = next(line for line in coverage.splitlines() if line.startswith("| ADSO_LOADED |"))
    cube_row = next(line for line in coverage.splitlines() if line.startswith("| CUBE_LOADED |"))
    assert "delta" in adso_row
    assert "full" in cube_row


def test_load_coverage_names_providers_no_chain_loads(tmp_path: Path) -> None:
    """A completeness claim needs its denominator, so an unloaded provider is named, not hidden."""
    result = _loaded_generator().generate(tmp_path / "kb", limit=5)
    base = Path(result.output_dir)
    coverage = (base / "01-inventory/load-coverage.md").read_text(encoding="utf-8")
    assert "Enumerated providers no chain loads" in coverage
    assert "- ADSO_ORPHAN" in coverage
    # ...and it is escalated to the gaps register rather than left only on the page.
    gaps = (base / "99-gaps-and-risks.md").read_text(encoding="utf-8")
    assert "loaded by no walked chain" in gaps


def test_loaded_objects_get_pages_even_when_limit_is_tiny(tmp_path: Path) -> None:
    """A cap must only ever cost a provider nothing loads: loaded objects lead the list."""
    result = _loaded_generator().generate(tmp_path / "kb", limit=2)
    base = Path(result.output_dir)
    assert (base / "04-providers" / f"{_slug('ADSO_LOADED')}.md").is_file()
    assert (base / "04-providers" / f"{_slug('CUBE_LOADED')}.md").is_file()


def test_provider_page_states_how_it_is_loaded(tmp_path: Path) -> None:
    result = _loaded_generator().generate(tmp_path / "kb", limit=5)
    page = (Path(result.output_dir) / "04-providers" / f"{_slug('ADSO_LOADED')}.md").read_text(
        encoding="utf-8"
    )
    assert "How it is loaded" in page
    assert "PC_MAIN" in page
    assert "delta" in page


def test_catalog_cap_widens_to_match_limit(tmp_path: Path) -> None:
    """Asking for more detail pages than the catalogue holds used to silently reduce the request.

    The detail loop slices the catalogue list, so ``limit`` above ``catalog_cap`` yielded
    ``min(limit, catalog_cap)`` pages with no indication the request had been clamped.
    """
    generator = _loaded_generator()
    result = generator.generate(tmp_path / "kb", limit=50, catalog_cap=1)
    chain_pages = [
        f for f in result.files if f.startswith("02-process-chains/") and "index" not in f
    ]
    assert len(chain_pages) == len(_CHAIN_STEPS)  # both chains, not just catalog_cap=1


def test_loading_chains_are_not_repeated_per_dtp(tmp_path: Path) -> None:
    """A chain loading one provider through several DTPs must be listed once, not per DTP."""
    result = _loaded_generator().generate(tmp_path / "kb", limit=5)
    coverage = (Path(result.output_dir) / "01-inventory/load-coverage.md").read_text(
        encoding="utf-8"
    )
    row = next(line for line in coverage.splitlines() if line.startswith("| CUBE_LOADED |"))
    # PC_MAIN reaches CUBE_LOADED only via PC_SUB, so the owning chain appears exactly once.
    assert row.count("PC_MAIN") == 1


def test_periodic_names_filters_to_repeating_cadence() -> None:
    """Only a chain with an observed repeating cadence makes its targets 'loaded periodically'."""
    closure = _LoadClosure()
    prov = Provenance(source_table="RSBKDTP")
    for name, frequency in (("NIGHTLY", "daily"), ("ONE_OFF", "once"), ("UNKNOWN_FREQ", "unknown")):
        closure.targets[name] = LoadedProvider(name=name, provenance=prov)
        closure.frequency_by_target[name].add(frequency)
    assert closure.periodic_names() == ["NIGHTLY"]


def test_sections_filter_writes_only_what_was_asked_for(tmp_path: Path) -> None:
    """A whole-system run outlives the database session, so it has to be splittable."""
    result = _loaded_generator().generate(tmp_path / "kb", limit=5, sections=["chains"])
    written = set(result.files)
    assert any(f.startswith("02-process-chains/") for f in written)
    # Nothing from the other sections, but the root index and gaps register always land.
    assert not any(f.startswith("04-providers/") for f in written)
    assert not any(f.startswith("06-queries/") for f in written)
    assert "index.md" in written
    assert "99-gaps-and-risks.md" in written


def test_separate_section_runs_compose_into_one_tree(tmp_path: Path) -> None:
    """Two restricted runs must produce the same files as asking for both at once."""
    base = tmp_path / "kb"
    _loaded_generator().generate(base, limit=5, sections=["chains"])
    _loaded_generator().generate(base, limit=5, sections=["load-coverage"])
    assert (base / "02-process-chains" / "index.md").is_file()
    assert (base / "01-inventory" / "load-coverage.md").is_file()


def test_unknown_section_is_rejected_rather_than_silently_ignored(tmp_path: Path) -> None:
    with pytest.raises(DocGenError) as excinfo:
        _loaded_generator().generate(tmp_path / "kb", sections=["chains", "nonsense"])
    assert "nonsense" in str(excinfo.value)


def test_resume_skips_pages_already_on_disk(tmp_path: Path) -> None:
    """A cut-off run must continue, not restart: building a detail page is the expensive part."""
    base = tmp_path / "kb"
    first = _loaded_generator().generate(base, limit=5, sections=["chains"])
    page = base / "02-process-chains" / f"{_slug('PC_MAIN')}.md"
    assert page.is_file()

    # Mark the existing page so it is obvious whether the resume rewrote it.
    page.write_text("SENTINEL", encoding="utf-8")
    second = _loaded_generator().generate(base, limit=5, sections=["chains"], resume=True)

    assert page.read_text(encoding="utf-8") == "SENTINEL"  # not rebuilt
    # The manifest still describes the whole tree, so counts stay comparable between runs.
    assert len(second.files) == len(first.files)


def test_resume_off_by_default_rewrites(tmp_path: Path) -> None:
    base = tmp_path / "kb"
    _loaded_generator().generate(base, limit=5, sections=["chains"])
    page = base / "02-process-chains" / f"{_slug('PC_MAIN')}.md"
    page.write_text("SENTINEL", encoding="utf-8")
    _loaded_generator().generate(base, limit=5, sections=["chains"])
    assert page.read_text(encoding="utf-8") != "SENTINEL"


def test_resumed_runs_accumulate_the_gaps_register(tmp_path: Path) -> None:
    """Each page is built once across a staged run, so its caveats must survive later sections."""
    base = tmp_path / "kb"
    # A section that records a distinctive gap, then a different section on top of it.
    _loaded_generator().generate(base, limit=5, sections=["load-coverage"], resume=True)
    after_first = (base / "99-gaps-and-risks.md").read_text(encoding="utf-8")
    assert "loaded by no walked chain" in after_first

    _loaded_generator().generate(base, limit=5, sections=["chains"], resume=True)
    after_second = (base / "99-gaps-and-risks.md").read_text(encoding="utf-8")
    # The earlier section's finding is still there rather than overwritten.
    assert "loaded by no walked chain" in after_second


def test_gaps_register_is_not_merged_when_not_resuming(tmp_path: Path) -> None:
    """A normal full run is the authority on its own gaps and must not inherit a stale list."""
    base = tmp_path / "kb"
    (base / "99-gaps-and-risks.md").parent.mkdir(parents=True, exist_ok=True)
    (base / "99-gaps-and-risks.md").write_text(
        "- **stale**: from a previous unrelated run\n", encoding="utf-8"
    )
    _loaded_generator().generate(base, limit=5, sections=["chains"])
    text = (base / "99-gaps-and-risks.md").read_text(encoding="utf-8")
    assert "from a previous unrelated run" not in text
