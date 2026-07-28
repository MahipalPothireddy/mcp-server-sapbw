"""Markdown knowledge-base generator (B10, mission Section 8).

``bw_generate_docs`` renders a browsable, citable markdown tree to a git-ignored / outside-repo
directory by composing every repository and service built so far. It never invents facts: each page
cites the source tables it was built from, generated descriptions render with a visible marker, and
everything unverified, truncated, or connector-gated is collected into an always-non-empty
``99-gaps-and-risks.md`` (a mission acceptance criterion).

Generation is bounded: each section renders an index catalogue plus a capped number of per-object
detail pages, and any truncation is recorded in the gaps register - an on-demand call never tries to
serialize an entire large system.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..connectors.base import ConnectorRegistry
from ..models.capability import CapabilityRecord
from ..models.description import Description
from ..models.hana import HanaCrossingReport
from ..models.lineage import LineageGraph
from ..models.provenance import UnsupportedResult
from ..models.providers import ObjectNotFound
from ..repositories.base import Repository
from ..repositories.chains import ChainsRepository
from ..repositories.hana import HanaRepository
from ..repositories.providers import ProvidersRepository
from ..repositories.queries import QueriesRepository
from ..repositories.transformations import TransformationsRepository
from .analyzers import SCENARIO_TITLES, Analyzers
from .lineage import LineageService

# A stored description and a synthesized one must never look identical (mission Rule 7).
GENERATED_MARKER = "**[GENERATED - synthesized from metadata, not stored in BW]**"

_PROVIDER_KINDS = {"dso", "adso", "infocube", "multiprovider", "compositeprovider", "infoobject"}
_SCENARIOS = ("9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7", "9.8", "layer_violations")

# Writing docs into the tracked repo tree would risk committing customer metadata (mission Section
# 10). Paths under these components (or entirely outside any git repo) are allowed.
_GITIGNORED_COMPONENTS = frozenset({"output", "extracts", "cache"})


class DocGenError(Exception):
    """Documentation generation could not proceed (e.g. an unsafe output location)."""


class DocGenResult(BaseModel):
    """Manifest returned by ``bw_generate_docs`` (the markdown itself is written to disk)."""

    model_config = ConfigDict(extra="forbid")

    system: str
    output_dir: str
    files: list[str] = Field(default_factory=list)
    page_count: int = 0
    gaps_count: int = 0
    truncated: bool = False
    generated_at: datetime


def _safe_output_dir(output_dir: str | Path) -> Path:
    """Resolve ``output_dir`` and refuse to write into a git-tracked location."""
    base = Path(output_dir).expanduser().resolve()
    for parent in [base, *base.parents]:
        if (parent / ".git").exists():
            if not (_GITIGNORED_COMPONENTS & set(base.parts)):
                allowed = ", ".join(sorted(_GITIGNORED_COMPONENTS))
                raise DocGenError(
                    f"refusing to generate docs into a git-tracked location: {base}. "
                    f"Use a path outside the repository or under a git-ignored dir ({allowed})."
                )
            break
    base.mkdir(parents=True, exist_ok=True)
    return base


def _slug(name: str) -> str:
    """Filesystem-safe slug for an object technical name."""
    out = "".join(c if c.isalnum() else "_" for c in name).strip("_")
    return out or "object"


def _render_description(description: Description | None) -> str:
    """Render a Description, marking generated text visibly (mission Rule 7)."""
    if description is None:
        return "_no description available_"
    text = description.description_long or description.description_short or "_(empty)_"
    marker = f"{GENERATED_MARKER}  \n" if description.origin != "stored" else ""
    origin = f"_origin: {description.origin}, quality: {description.quality_flag}_"
    return f"{marker}{text}  \n{origin}"


class _Gaps:
    """Aggregates everything unverified, truncated, or connector-gated during generation."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, section: str, note: str) -> None:
        entry = f"- **{section}**: {note}"
        if entry not in self.items:
            self.items.append(entry)

    def unsupported(self, section: str, result: UnsupportedResult) -> None:
        self.add(section, f"unsupported on release {result.release}: {result.detail}")

    def truncated(self, section: str, shown: int, total: int) -> None:
        if total > shown:
            self.add(section, f"showing {shown} of {total}; the rest were not rendered")


class DocGenerator(Repository):
    """Renders the mission Section 8 markdown tree by composing all repositories/services."""

    def __init__(
        self, connection: Any, capability: Any, cache: Any = None, registry: Any = None
    ) -> None:
        super().__init__(connection, capability, cache)
        self._chains = ChainsRepository(connection, capability, cache)
        self._providers = ProvidersRepository(connection, capability, cache)
        self._transformations = TransformationsRepository(connection, capability, cache)
        self._queries = QueriesRepository(connection, capability, cache)
        self._hana = HanaRepository(connection, capability, cache)
        self._lineage = LineageService(connection, capability, cache)
        self._analyzers = Analyzers(
            connection, capability, cache, registry=registry or ConnectorRegistry()
        )
        self._gaps = _Gaps()
        self._files: list[str] = []
        self._truncated = False

    # --- orchestration -------------------------------------------------------------------

    def generate(
        self, output_dir: str | Path, *, limit: int = 15, catalog_cap: int = 200
    ) -> DocGenResult:
        base = _safe_output_dir(output_dir)
        cap: CapabilityRecord = self.capability

        self._write(base, "index.md", self._index_page(cap))
        self._section_inventory(base)
        self._section_chains(base, limit, catalog_cap)
        self._section_lineage(base, limit)
        self._section_providers(base, limit, catalog_cap)
        self._section_transformations(base, limit, catalog_cap)
        self._section_queries(base, limit, catalog_cap)
        self._section_hana(base, limit, catalog_cap)
        self._section_scenarios(base, limit)
        # The gaps register is always written last and is always non-empty.
        self._write(base, "99-gaps-and-risks.md", self._gaps_page())

        return DocGenResult(
            system=cap.system,
            output_dir=str(base),
            files=sorted(self._files),
            page_count=len(self._files),
            gaps_count=len(self._gaps.items),
            truncated=self._truncated,
            generated_at=datetime.now(UTC),
        )

    # --- writing + shared page furniture -------------------------------------------------

    def _write(self, base: Path, relpath: str, content: str) -> None:
        target = base / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._files.append(relpath.replace("\\", "/"))

    @staticmethod
    def _backlinks(depth: int) -> str:
        root = "../" * depth
        parts = [f"[KB index]({root}index.md)"]
        if depth > 0:
            parts.append("[section index](index.md)")
        return " | ".join(parts)

    @staticmethod
    def _citation(tables: str) -> str:
        return f"\n\n---\n_Source tables: {tables}_\n" if tables else ""

    def _empty_section(self, title: str) -> str:
        return "\n".join(
            [
                f"# {title}",
                "",
                self._backlinks(1),
                "",
                "_This section is unavailable on the connected release; see the gaps register._",
            ]
        )

    def _unwrap(self, section: str, result: Any) -> Any:
        """Return the value, or record an UnsupportedResult in the gaps register and return None."""
        if isinstance(result, UnsupportedResult):
            self._gaps.unsupported(section, result)
            return None
        return result

    # --- index ---------------------------------------------------------------------------

    def _index_page(self, cap: CapabilityRecord) -> str:
        models = ", ".join(k for k in cap.object_models if cap.has_object_model(k)) or "(none)"
        return "\n".join(
            [
                f"# BW-on-HANA knowledge base - {cap.system}",
                "",
                f"- Generated: {datetime.now(UTC).isoformat()}",
                f"- BW release: **{cap.bw_release}**",
                f"- ABAP schema: **{cap.abap_schema}**",
                f"- HANA repository style: **{cap.hana_repo_style}**",
                f"- Object-model variants: {models}",
                f"- Process-log analysis window: {cap.processlog_retention_days} days",
                "",
                "Generated read-only from BW metadata. Every fact cites its source table; "
                "generated descriptions are marked; unverified or connector-gated items are in "
                "the gaps register.",
                "",
                "## Sections",
                "",
                "1. [Inventory](01-inventory/index.md)",
                "2. [Process chains](02-process-chains/index.md)",
                "3. [Lineage](03-lineage/index.md)",
                "4. [Providers](04-providers/index.md)",
                "5. [Transformations](05-transformations/index.md)",
                "6. [Queries](06-queries/index.md)",
                "7. [HANA layer](07-hana/index.md)",
                "8. [Risk scenarios](08-scenarios/index.md)",
                "",
                "**[Gaps and risks](99-gaps-and-risks.md)** - everything unverified/inaccessible.",
                "",
            ]
        )

    # --- 01 inventory --------------------------------------------------------------------

    def _section_inventory(self, base: Path) -> None:
        rows = [
            "| Object type | Count | Source |",
            "|---|---|---|",
            f"| Process chains | {self._chain_total()} | RSPCCHAINATTR |",
            f"| Transformations | {self._tran_total()} | RSTRAN |",
            f"| BEx queries | {self._query_total()} | RSZCOMPDIR |",
            f"| Calc views | {self._calcview_total()} | SYS.VIEWS |",
        ]
        for logical, label in (
            ("dso_header", "Classic DSOs"),
            ("adso_header", "Advanced DSOs"),
            ("cube_header", "InfoCubes/MultiProviders"),
            ("infoobject", "InfoObjects"),
        ):
            status = self.capability.table(logical)
            if status is not None and status.present and status.row_estimate is not None:
                rows.append(
                    f"| {label} (row estimate) | {status.row_estimate:,} | {status.resolved_name} |"
                )
        page = [
            "# Inventory",
            "",
            self._backlinks(1),
            "",
            "Object counts across the connected system. Provider counts are row estimates from "
            "`SYS.M_TABLES` (cheap, approximate).",
            "",
            *rows,
            self._citation("RSPCCHAINATTR, RSTRAN, RSZCOMPDIR, SYS.VIEWS, SYS.M_TABLES"),
        ]
        self._write(base, "01-inventory/index.md", "\n".join(page))

    def _chain_total(self) -> int:
        result = self._unwrap("inventory", self._chains.list_chains(limit=1))
        return result[1] if result else 0

    def _tran_total(self) -> int:
        result = self._unwrap("inventory", self._transformations.list_transformations(limit=1))
        return result[1] if result else 0

    def _query_total(self) -> int:
        result = self._unwrap("inventory", self._queries.list_queries(limit=1))
        return result[1] if result else 0

    def _calcview_total(self) -> int:
        result = self._hana.list_calc_views(limit=1)
        if isinstance(result, UnsupportedResult):
            self._gaps.unsupported("inventory/hana", result)
            return 0
        return result[1]

    # --- 02 process chains ---------------------------------------------------------------

    def _section_chains(self, base: Path, limit: int, catalog_cap: int) -> None:
        result = self._unwrap("process-chains", self._chains.list_chains(limit=catalog_cap))
        if result is None:
            self._write(base, "02-process-chains/index.md", self._empty_section("Process chains"))
            return
        chains, total = result
        self._gaps.truncated("process-chains", len(chains), total)
        self._truncated = self._truncated or total > len(chains)
        rows = ["| Chain | Frequency | Runs/day | Last run |", "|---|---|---|---|"]
        for chain in chains:
            link = f"[{chain.chain_id}]({_slug(chain.chain_id)}.md)"
            rows.append(
                f"| {link} | {chain.frequency} | {chain.runs_per_day or '-'} | "
                f"{chain.last_run or '-'} |"
            )
        page = [
            "# Process chains",
            "",
            self._backlinks(1),
            "",
            f"{total} chains total; detail pages for the first {min(limit, len(chains))}.",
            "",
            *rows,
            self._citation("RSPCCHAINATTR, RSPCLOGCHAIN"),
        ]
        self._write(base, "02-process-chains/index.md", "\n".join(page))
        for chain in chains[:limit]:
            self._write(
                base,
                f"02-process-chains/{_slug(chain.chain_id)}.md",
                self._chain_page(chain.chain_id),
            )

    def _chain_page(self, chain_id: str) -> str:
        lines = [f"# Chain {chain_id}", "", self._backlinks(1), ""]
        structure = self._unwrap("process-chains", self._chains.get_chain(chain_id))
        if structure is not None:
            lines += [
                f"- Description: {structure.description or '-'}",
                f"- Processes: {len(structure.processes)}",
                f"- Sub-chains: {len(structure.subchain_ids)}",
                "",
            ]
        runtimes = self._unwrap("process-chains", self._chains.get_chain_runtimes(chain_id))
        if runtimes is not None:
            dur = runtimes.duration_seconds
            lines += [
                "## Runtime statistics",
                f"- Window: {runtimes.window_days_actual} days",
                f"- Runs: {runtimes.total_runs} (success rate "
                f"{runtimes.success_rate if runtimes.success_rate is not None else '-'})",
                f"- Duration p95: {dur.p95_s if dur.p95_s is not None else '-'} s "
                f"(max {dur.max_s if dur.max_s is not None else '-'} s)",
                f"- Observed overlapping runs: {runtimes.observed_overlap_runs}",
                "",
            ]
            for caveat in runtimes.caveats:
                self._gaps.add(f"chain {chain_id}", caveat)
        lines.append(self._citation("RSPCCHAINATTR, RSPCCHAIN, RSPCLOGCHAIN, RSPCPROCESSLOG"))
        return "\n".join(lines)

    # --- 03 lineage ----------------------------------------------------------------------

    def _section_lineage(self, base: Path, limit: int) -> None:
        seeds = self._lineage_seeds(limit)
        index = [
            "# Lineage",
            "",
            self._backlinks(1),
            "",
            "End-to-end data-flow graphs (Mermaid diagram + graph JSON) traced from a sample of "
            "objects. Routine-derived edges are advisory (dashed).",
            "",
        ]
        if not seeds:
            self._gaps.add(
                "lineage",
                "no seed objects were derivable from the transformation catalogue; "
                "no flow pages generated",
            )
            index.append("_No lineage flows generated._")
            self._write(base, "03-lineage/index.md", "\n".join(index))
            return
        for name in seeds:
            slug = _slug(name)
            index.append(f"- [{name}]({slug}.md)")
            self._write(base, f"03-lineage/{slug}.md", self._lineage_page(name))
        self._write(base, "03-lineage/index.md", "\n".join(index))

    def _lineage_page(self, name: str) -> str:
        lines = [f"# Lineage: {name}", "", self._backlinks(1), ""]
        graph = self._unwrap("lineage", self._lineage.get_lineage(name, direction="both", depth=6))
        if graph is None:
            return "\n".join([*lines, "_lineage unavailable_"])
        lines += ["## Flow diagram", "", self._mermaid(graph), ""]
        trace = self._unwrap("lineage", self._lineage.trace_to_source(name))
        if trace is not None:
            reached = ", ".join(trace.datasources_reached) or "(none reached)"
            lines += [f"- DataSources reached upstream: {reached}", ""]
        for caveat in graph.caveats:
            self._gaps.add(f"lineage {name}", caveat)
        lines += ["## Graph JSON", "", "```json", graph.model_dump_json(indent=2), "```"]
        lines.append(self._citation("RSTRAN, RSBKDTP, RSAABAP"))
        return "\n".join(lines)

    @staticmethod
    def _mermaid(graph: LineageGraph) -> str:
        ids: dict[str, str] = {}
        lines = ["```mermaid", "flowchart LR"]
        for i, node in enumerate(graph.nodes):
            node_id = f"n{i}"
            ids[node.id] = node_id
            lines.append(f'  {node_id}["{node.name}<br/>({node.object_type})"]')
        for edge in graph.edges:
            src, dst = ids.get(edge.src), ids.get(edge.dst)
            if src and dst:
                arrow = "-.->" if edge.confidence == "advisory" else "-->"
                lines.append(f"  {src} {arrow}|{edge.kind}| {dst}")
        lines.append("```")
        return "\n".join(lines)

    def _lineage_seeds(self, limit: int) -> list[str]:
        """A sample of provider-typed transformation targets to root lineage flows at."""
        result = self._unwrap("lineage", self._transformations.list_transformations(limit=100))
        if result is None:
            return []
        seen: list[str] = []
        for summary in result[0]:
            name = summary.target_name
            if name and summary.target_kind in _PROVIDER_KINDS and name not in seen:
                seen.append(name)
            if len(seen) >= limit:
                break
        return seen

    # --- 04 providers --------------------------------------------------------------------

    def _section_providers(self, base: Path, limit: int, catalog_cap: int) -> None:
        names, trans_in, trans_out = self._provider_names_and_edges(catalog_cap)
        query_by_provider = self._query_by_provider(catalog_cap)
        calcviews_by_object = self._calcviews_by_object()
        index = [
            "# Providers",
            "",
            self._backlinks(1),
            "",
            "Providers sampled from the transformation catalogue (BW has no 'list all providers' "
            "call). Each page lists sources, targets, load edges, calc views reading it, and "
            "reports depending on it.",
            "",
        ]
        if not names:
            self._gaps.add("providers", "no provider names derivable from transformations")
            index.append("_No providers rendered._")
            self._write(base, "04-providers/index.md", "\n".join(index))
            return
        for name in names[:limit]:
            slug = _slug(name)
            index.append(f"- [{name}]({slug}.md)")
            self._write(
                base,
                f"04-providers/{slug}.md",
                self._provider_page(
                    name, trans_in, trans_out, query_by_provider, calcviews_by_object
                ),
            )
        self._gaps.truncated("providers", min(limit, len(names)), len(names))
        self._write(base, "04-providers/index.md", "\n".join(index))

    def _provider_page(
        self,
        name: str,
        trans_in: dict[str, list[str]],
        trans_out: dict[str, list[str]],
        query_by_provider: dict[str, list[str]],
        calcviews_by_object: dict[str, list[str]],
    ) -> str:
        lines = [f"# Provider {name}", "", self._backlinks(1), ""]
        provider = self._unwrap("providers", self._providers.describe(name))
        if isinstance(provider, ObjectNotFound):
            self._gaps.add("providers", f"{name}: {provider.detail}")
        elif provider is not None:
            lines += [
                f"- Type: **{provider.object_type}**",
                f"- Active: {provider.active}",
                f"- Key fields: {', '.join(provider.key_field_names) or '-'}",
                f"- Fields: {len(provider.fields)}",
                f"- Part providers: {len(provider.part_providers)} "
                f"(source: {provider.composition_source})",
                "",
                "## Description",
                _render_description(provider.description),
                "",
            ]
            for caveat in provider.caveats:
                self._gaps.add(f"provider {name}", caveat)
        lines += [
            "## Load edges",
            f"- Inbound transformations (target this): {', '.join(trans_in.get(name, [])) or '-'}",
            f"- Outbound transformations (source from this): "
            f"{', '.join(trans_out.get(name, [])) or '-'}",
            "",
            "## HANA & consumers",
            f"- Calc views reading this: {', '.join(calcviews_by_object.get(name, [])) or '-'}",
            f"- Reports/queries on this provider: "
            f"{', '.join(query_by_provider.get(name, [])) or '-'}",
            f"- Routine-embedded consumers (advisory): "
            f"{', '.join(self._routine_lookup_consumers(name)) or '-'}",
            self._citation("RSDODSO, RSOADSO, RSDCUBE, RSDIOBJ, RSTRAN, RSAABAP"),
        ]
        return "\n".join(lines)

    def _routine_lookup_consumers(self, name: str) -> list[str]:
        impact = self._lineage.impact_analysis(name, depth=1)
        if isinstance(impact, UnsupportedResult):
            return []
        return impact.routine_lookup_consumers[:20]

    def _provider_names_and_edges(
        self, cap: int
    ) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
        result = self._unwrap("providers", self._transformations.list_transformations(limit=cap))
        names: list[str] = []
        trans_in: dict[str, list[str]] = defaultdict(list)
        trans_out: dict[str, list[str]] = defaultdict(list)
        if result is None:
            return names, dict(trans_in), dict(trans_out)
        for s in result[0]:
            if s.target_name and s.target_kind in _PROVIDER_KINDS:
                trans_in[s.target_name].append(s.tran_id)
                if s.target_name not in names:
                    names.append(s.target_name)
            if s.source_name and s.source_kind in _PROVIDER_KINDS:
                trans_out[s.source_name].append(s.tran_id)
        return names, dict(trans_in), dict(trans_out)

    def _query_by_provider(self, cap: int) -> dict[str, list[str]]:
        result = self._unwrap("providers", self._queries.list_queries(limit=cap))
        mapping: dict[str, list[str]] = defaultdict(list)
        if result is None:
            return dict(mapping)
        for q in result[0]:
            if q.provider:
                mapping[q.provider].append(q.compid or q.compuid)
        return dict(mapping)

    def _calcviews_by_object(self) -> dict[str, list[str]]:
        report = self._hana.get_hana_crossings(limit=500)
        mapping: dict[str, list[str]] = defaultdict(list)
        if isinstance(report, UnsupportedResult):
            return dict(mapping)
        for crossing in report.crossings:
            key = crossing.bw_object_resolved or crossing.bw_object
            if crossing.direction == "hana_reads_bw" and key:
                mapping[key].append(crossing.hana_object)
        return dict(mapping)

    # --- 05 transformations --------------------------------------------------------------

    def _section_transformations(self, base: Path, limit: int, catalog_cap: int) -> None:
        result = self._unwrap(
            "transformations", self._transformations.list_transformations(limit=catalog_cap)
        )
        if result is None:
            self._write(base, "05-transformations/index.md", self._empty_section("Transformations"))
            return
        summaries, total = result
        self._gaps.truncated("transformations", len(summaries), total)
        self._truncated = self._truncated or total > len(summaries)
        rows = ["| Transformation | Source | Target | Routines |", "|---|---|---|---|"]
        for s in summaries:
            link = f"[{s.tran_id}]({_slug(s.tran_id)}.md)"
            rows.append(
                f"| {link} | {s.source_name or '-'} | {s.target_name or '-'} | "
                f"{'yes' if s.has_routines else 'no'} |"
            )
        page = [
            "# Transformations",
            "",
            self._backlinks(1),
            "",
            f"{total} transformations total; detail pages for the first "
            f"{min(limit, len(summaries))}.",
            "",
            *rows,
            self._citation("RSTRAN"),
        ]
        self._write(base, "05-transformations/index.md", "\n".join(page))
        for s in summaries[:limit]:
            self._write(
                base,
                f"05-transformations/{_slug(s.tran_id)}.md",
                self._transformation_page(s.tran_id),
            )

    def _transformation_page(self, tran_id: str) -> str:
        lines = [f"# Transformation {tran_id}", "", self._backlinks(1), ""]
        tran = self._unwrap("transformations", self._transformations.get_transformation(tran_id))
        if tran is not None:
            src = tran.source.name if tran.source else "-"
            tgt = tran.target.name if tran.target else "-"
            lines += [
                f"- {src} -> {tgt}",
                f"- Field mappings: {len(tran.field_mappings)}",
                f"- Routines: start={tran.has_start_routine}, end={tran.has_end_routine}, "
                f"expert={tran.has_expert_routine}",
                "",
            ]
        analyses = self._transformations.analyze_routines(tran_id)
        if not isinstance(analyses, UnsupportedResult) and analyses:
            lines += ["## Routine analysis (heuristic lower bound)", ""]
            for analysis in analyses:
                deps = (
                    ", ".join(d.resolved_object or d.table for d in analysis.table_dependencies)
                    or "-"
                )
                lines.append(
                    f"- `{analysis.code_id}` ({analysis.kind}): reads {deps}; "
                    f"anti-patterns: {len(analysis.anti_patterns)}"
                )
            self._gaps.add(
                f"transformation {tran_id}", "routine dependencies are a heuristic lower bound"
            )
            lines.append("")
        lines.append(self._citation("RSTRAN, RSTRANRULE, RSTRANFIELD, RSAABAP"))
        return "\n".join(lines)

    # --- 06 queries ----------------------------------------------------------------------

    def _section_queries(self, base: Path, limit: int, catalog_cap: int) -> None:
        result = self._unwrap("queries", self._queries.list_queries(limit=catalog_cap))
        if result is None:
            self._write(base, "06-queries/index.md", self._empty_section("Queries"))
            return
        summaries, total = result
        self._gaps.truncated("queries", len(summaries), total)
        self._truncated = self._truncated or total > len(summaries)
        rows = ["| Query | Provider | Last used |", "|---|---|---|"]
        for q in summaries:
            label = q.compid or q.compuid
            rows.append(
                f"| [{label}]({_slug(label)}.md) | {q.provider or '-'} | {q.last_used or '-'} |"
            )
        page = [
            "# Queries",
            "",
            self._backlinks(1),
            "",
            f"{total} queries total; detail pages for the first {min(limit, len(summaries))}.",
            "",
            *rows,
            self._citation("RSZCOMPDIR, RSZCOMPIC"),
        ]
        self._write(base, "06-queries/index.md", "\n".join(page))
        for q in summaries[:limit]:
            label = q.compid or q.compuid
            self._write(base, f"06-queries/{_slug(label)}.md", self._query_page(q.compuid, label))

    def _query_page(self, compuid: str, label: str) -> str:
        lines = [f"# Query {label}", "", self._backlinks(1), ""]
        query = self._unwrap("queries", self._queries.get_query(compuid))
        if query is not None:
            lines += [
                f"- Description: {query.description or '-'}",
                f"- Provider: {query.provider or '-'}",
                f"- Elements: {len(query.elements)}, variables: {len(query.variables)}",
                "",
            ]
            for caveat in query.caveats:
                self._gaps.add(f"query {label}", caveat)
        lineage = self._unwrap("queries", self._queries.get_query_lineage(compuid))
        if lineage is not None:
            lines += [
                "## Field lineage",
                f"- InfoObject paths: {len(lineage.paths)}",
                f"- Customer-exit variables (lineage dead ends): "
                f"{', '.join(lineage.customer_exit_variables) or '-'}",
                "",
            ]
            if lineage.customer_exit_variables:
                self._gaps.add(
                    f"query {label}",
                    "customer-exit variables resolve in ABAP at runtime (lineage ends there)",
                )
        lines.append(self._citation("RSZCOMPDIR, RSZELTDIR, RSZELTXREF, RSZGLOBV"))
        return "\n".join(lines)

    # --- 07 hana -------------------------------------------------------------------------

    def _section_hana(self, base: Path, limit: int, catalog_cap: int) -> None:
        views = self._hana.list_calc_views(limit=catalog_cap)
        if isinstance(views, UnsupportedResult):
            self._gaps.unsupported("hana", views)
            self._write(base, "07-hana/index.md", self._empty_section("HANA layer"))
            return
        calc_views, total = views
        self._gaps.truncated("hana", len(calc_views), total)
        rows = ["| Calc view | Type | BW-consuming |", "|---|---|---|"]
        for view in calc_views:
            rows.append(f"| {view.name} | {view.view_type} | {view.is_bw_consuming} |")
        page = [
            "# HANA layer",
            "",
            self._backlinks(1),
            "",
            f"{total} calc views (VIEW_TYPE in CALC/JOIN/OLAP).",
            "",
        ]
        crossings = self._hana.get_hana_crossings(limit=limit)
        if not isinstance(crossings, UnsupportedResult):
            page += [
                "## BW <-> HANA crossings",
                f"- Total: {crossings.total_count} "
                f"(HANA reads BW: {crossings.hana_reads_bw_count}, "
                f"BW reads HANA: {crossings.bw_reads_hana_count})",
                "",
            ]
            page += self._calc_view_consumers(crossings)
            for caveat in crossings.caveats:
                self._gaps.add("hana crossings", caveat)
        page += rows
        page.append(self._citation("SYS.VIEWS, SYS.OBJECT_DEPENDENCIES"))
        self._write(base, "07-hana/index.md", "\n".join(page))

    @staticmethod
    def _calc_view_consumers(crossings: HanaCrossingReport) -> list[str]:
        """The calc-view -> consuming-InfoProvider hop, from the crossing rows already fetched."""
        consumers = [
            crossing
            for crossing in crossings.crossings
            if crossing.direction == "bw_reads_hana" and crossing.resolution == "bw_provider_view"
        ]
        if not consumers:
            return []
        lines = [
            "### Calc view -> consuming InfoProvider",
            "",
            "Derived from the BW-generated `0BW:BIA:<PROVIDER>` views. For a CompositeProvider "
            "this is the calc-view -> CompositeProvider hop, which BW's own where-used lists do "
            "not report.",
            "",
            "| Calc view | Consuming provider | Kind | Verified |",
            "|---|---|---|---|",
        ]
        seen: set[tuple[str, str]] = set()
        for crossing in consumers:
            provider = crossing.bw_object_resolved or "?"
            key = (crossing.hana_object, provider)
            if key in seen:
                continue  # the ':J1.CALC.n' internal nodes all name the same provider
            seen.add(key)
            kind = crossing.bw_object_kind or "unverified"
            verified = "yes" if crossing.bw_object_kind else "no"
            lines.append(f"| {crossing.hana_object} | {provider} | {kind} | {verified} |")
        lines.append("")
        return lines

    # --- 08 scenarios --------------------------------------------------------------------

    def _section_scenarios(self, base: Path, limit: int) -> None:
        index = [
            "# Risk scenarios",
            "",
            self._backlinks(1),
            "",
            "The eight risk analyses (mission Section 9) plus the layer-violation finder. "
            "Connector-gated scenarios (9.6 ECC, 9.7/9.8 Tableau/BOBJ) show what BW alone can "
            "derive and name the connector required for the rest.",
            "",
        ]
        for scenario in _SCENARIOS:
            slug = _slug(scenario)
            title = SCENARIO_TITLES.get(scenario, scenario)
            index.append(f"- [{scenario} - {title}]({slug}.md)")
            self._write(base, f"08-scenarios/{slug}.md", self._scenario_page(scenario, limit))
        self._write(base, "08-scenarios/index.md", "\n".join(index))

    def _scenario_page(self, scenario: str, limit: int) -> str:
        title = SCENARIO_TITLES.get(scenario, scenario)
        lines = [f"# Scenario {scenario} - {title}", "", self._backlinks(1), ""]
        report = self._unwrap("scenarios", self._analyzers.run_scenario(scenario, limit=limit))
        if report is None:
            return "\n".join([*lines, "_scenario unavailable on this release_"])
        lines += [
            f"- Findings: {report.finding_count} (analyzed {report.analyzed_count}"
            f"{', truncated' if report.truncated else ''})",
        ]
        if report.connector_required:
            lines.append(f"- **Connector required: {report.connector_required}** (not configured)")
            self._gaps.add(
                f"scenario {scenario}",
                f"needs a {report.connector_required} connector; only the BW-derivable part is "
                "populated",
            )
        lines.append("")
        for finding in report.findings:
            objects = ", ".join(finding.affected_objects[:8]) or "-"
            lines += [
                f"### [{finding.severity}] {finding.title}",
                f"- Affected: {objects}",
                f"- Recommendation: {finding.recommendation}",
            ]
            if finding.unpopulated_reason:
                lines.append(f"- Unpopulated: {finding.unpopulated_reason}")
            lines.append("")
        for caveat in report.caveats:
            self._gaps.add(f"scenario {scenario}", caveat)
        lines.append(self._citation("RSTRAN, RSBKDTP, RSAABAP, RSDSSEGFD, SYS.OBJECT_DEPENDENCIES"))
        return "\n".join(lines)

    # --- 99 gaps and risks ---------------------------------------------------------------

    def _gaps_page(self) -> str:
        # Always non-empty: a standing set of known limitations precedes the run-specific gaps
        # (mission acceptance - an empty gap list would mean the analysis was not honest).
        standing = [
            "- **Routine analysis** is a heuristic lower bound: dynamic SQL, function-module, and "
            "class-method calls are not followed (mission Known Limitation 3).",
            "- **/BIC/ and /BI0/ resolution** to BW objects is advisory (naming-convention based).",
            "- **Object -> loading-chain frequency mapping** is not derivable on this landscape, "
            "so latency-contract cadence checks (9.1/9.7) are marked unverifiable, not guessed.",
            "- **External systems** (ECC extractor source, Tableau/BOBJ schedules) are unreachable "
            "over the BW HANA connection; scenarios 9.6/9.7/9.8 are connector-gated.",
            "- **Runtime statistics** reflect contention where chains overlap and are bounded by "
            "the RSPCPROCESSLOG retention window.",
        ]
        return "\n".join(
            [
                "# Gaps and risks",
                "",
                self._backlinks(0),
                "",
                "Everything unverified, inaccessible, truncated, or connector-gated in this "
                "generation. This register is intentionally never empty.",
                "",
                "## Standing limitations",
                "",
                *standing,
                "",
                "## Gaps recorded during this generation",
                "",
                *(self._gaps.items or ["- (no additional run-specific gaps recorded)"]),
                "",
            ]
        )
