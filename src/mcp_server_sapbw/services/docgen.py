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
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..connectors.base import ConnectorRegistry
from ..models.capability import CapabilityRecord
from ..models.chains import LoadedProvider
from ..models.description import Description
from ..models.hana import HanaCrossingReport
from ..models.lineage import LineageGraph
from ..models.provenance import UnsupportedResult
from ..models.providers import ObjectNotFound, Provider
from ..models.queries import Query
from ..repositories.base import Repository
from ..repositories.chains import ChainsRepository
from ..repositories.hana import HanaRepository
from ..repositories.providers import ProvidersRepository
from ..repositories.queries import QueriesRepository
from ..repositories.threex import ThreeXRepository
from ..repositories.transformations import TransformationsRepository
from .analyzers import SCENARIO_TITLES, Analyzers
from .lineage import LineageService
from .load_closure import LoadClosureService

# A stored description and a synthesized one must never look identical (mission Rule 7).
GENERATED_MARKER = "**[GENERATED - synthesized from metadata, not stored in BW]**"

_PROVIDER_KINDS = {"dso", "adso", "infocube", "multiprovider", "compositeprovider", "infoobject"}
_SCENARIOS = ("9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7", "9.8", "layer_violations")

# Header table + id column per InfoProvider family, for the independent coverage denominator.
# InfoObjects are excluded: they are loaded like providers but counted separately, and RSDIOBJ runs
# to thousands of rows, most of them SAP-delivered and never loaded.
_PROVIDER_HEADERS: tuple[tuple[str, str], ...] = (
    ("dso_header", "ODSOBJECT"),
    ("adso_header", "ADSONM"),
    ("cube_header", "INFOCUBE"),
    ("composite_header", "HCPRNM"),
)
# A provider enumeration that hit this bound is reported as a lower bound rather than a count.
_ENUMERATION_CAP = 5000
# Loading chains listed inline in a coverage-table cell before the rest are summarised as a count.
_CHAINS_PER_CELL = 4
# Hops to walk for a per-object lineage page. Four covers the deepest flow shape this documentation
# set is asked about - EDW DSO -> ADM DSO -> calc view -> CompositeProvider -> BEx query, which is
# four hops - and the graph still reports when it truncates. Deeper walks cost disproportionately:
# expanding a node means analysing the routines of every transformation targeting it, and node count
# grows with the fan-out, so depth 6 measured at roughly 3x the time of depth 3 for redundant
# breadth that the neighbouring objects' own pages already cover.
_LINEAGE_DEPTH = 4
# Section writers, in output order. Exposed so a long run can be split across processes: a
# full-system generation takes hours and the HANA session is closed by the server well before that.
SECTIONS: tuple[str, ...] = (
    "inventory",
    "load-coverage",
    "chains",
    "lineage",
    "providers",
    "transformations",
    "transfer-rules",
    "queries",
    "hana",
    "scenarios",
)

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


# How many attributes a provider page renders before eliding. A characteristic can carry 175.
_MAX_ATTRIBUTES = 60
# How many value-altering elements a query page lists before eliding.
_MAX_ALTERED_ELEMENTS = 40


def _render_attributes(provider: Provider) -> list[str]:
    """The attribute table for a characteristic InfoObject; nothing for any other object type.

    ``Navigable`` is the column to read, not ``Kind``: an inherited attribute can be navigable on
    the basic characteristic and still have no navigation name here, in which case no query on this
    characteristic can drill down by it.
    """
    if not provider.attributes:
        return []
    rows = provider.attributes[:_MAX_ATTRIBUTES]
    inherited = sorted({a.inherited_from for a in provider.attributes if a.inherited_from})
    lines = ["## Attributes", ""]
    if inherited:
        lines += [
            f"Inherited from basic characteristic **{', '.join(inherited)}** - this is a reference "
            "characteristic, so its attribute list is defined there while its navigation names are "
            "its own.",
            "",
        ]
    lines += [
        "| # | Attribute | Description | Kind | Navigable | Navigation name | Time-dep. | Auth. |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for attribute in rows:
        lines.append(
            f"| {attribute.position or ''} | `{attribute.name}` "
            f"| {attribute.description or '-'} | {attribute.kind} "
            f"| {'yes' if attribute.navigable else 'no'} "
            f"| {f'`{attribute.navigation_name}`' if attribute.navigation_name else '-'} "
            f"| {'yes' if attribute.time_dependent else 'no'} "
            f"| {'yes' if attribute.auth_relevant else 'no'} |"
        )
    if len(provider.attributes) > _MAX_ATTRIBUTES:
        lines.append(f"\n_Showing {_MAX_ATTRIBUTES} of {len(provider.attributes)} attributes._")
    lines.append("")
    return lines


def _render_evidence_mix(graph: LineageGraph) -> list[str]:
    """How much of a flow is declared metadata and how much was inferred.

    The diagram already dashes an inferred edge, which is right for reading one path but useless for
    judging the whole shape. The counts say whether this page is a picture of the system or a
    hypothesis about it.
    """
    summary = graph.evidence_summary
    if summary is None or not summary.total:
        return []
    lines = [
        "## Evidence",
        "",
        "| Basis | Edges | Meaning |",
        "|---|---|---|",
        f"| observed | {summary.observed} | A metadata row declares the edge |",
        f"| derived | {summary.derived} | Assembled from rows by a documented rule |",
        f"| inferred | {summary.inferred} | Rests on a convention or a parsed routine - confirm "
        "before acting |",
        f"| unknown | {summary.unknown} | Could not be established |",
        "",
        f"Mechanisms present: {', '.join(f'`{m}`' for m in summary.methods)}. Each edge in the "
        "graph JSON below carries its own `evidence.detail` saying why it was concluded.",
        "",
    ]
    return lines


def _render_value_altering_elements(query: Query) -> list[str]:
    """The elements that do something to their own value before it is displayed.

    This is the section someone reads when two people disagree about a figure from one report. Only
    settings that change the number appear; formatting choices stay out, because listing them here
    would bury the ones that matter.
    """
    altered = [e for e in query.elements if e.properties and e.properties.changes_the_number]
    if not altered:
        return []
    lines = [
        "## Elements that change their own value",
        "",
        "The figure each of these shows is not the plain sum of the records behind it.",
        "",
        "| Element | Type | What happens to the value |",
        "|---|---|---|",
    ]
    for element in altered[:_MAX_ALTERED_ELEMENTS]:
        reasons = "; ".join(element.properties.changes_the_number) if element.properties else ""
        lines.append(f"| `{element.name or element.eltuid}` | {element.element_type} | {reasons} |")
    if len(altered) > _MAX_ALTERED_ELEMENTS:
        lines.append(f"\n_Showing {_MAX_ALTERED_ELEMENTS} of {len(altered)} elements._")
    lines.append("")
    return lines


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

    def absorb(self, entries: list[str]) -> None:
        """Merge already-recorded entries in, keeping this run's first and de-duplicating."""
        for entry in entries:
            if entry not in self.items:
                self.items.append(entry)


class _LoadClosure:
    """Every object the chains load, unioned across all chains.

    This is the authoritative answer to "what is loaded periodically", and it is a different
    question from "what appears in the transformation catalogue". A DTP can load a provider with no
    transformation at all (a 1:1 move), and a CompositeProvider consumes its parts through a
    generated calc view rather than a transformation - so a transformation-derived object list can
    never be complete no matter how high its cap is raised. The path here is
    ``RSPCCHAIN`` -> DTP id -> ``RSBKDTP``, walked recursively through nested sub-chains.
    """

    def __init__(self) -> None:
        self.targets: dict[str, LoadedProvider] = {}
        self.chains_by_target: dict[str, list[str]] = defaultdict(list)
        self.modes_by_target: dict[str, set[str]] = defaultdict(set)
        self.frequency_by_target: dict[str, set[str]] = defaultdict(set)
        self.chains_walked = 0
        self.chains_total = 0
        self.resolved = False

    @property
    def names(self) -> list[str]:
        """Loaded object names, ordered so the reading is stable across runs."""
        return sorted(self.targets)

    def periodic_names(self) -> list[str]:
        """Names loaded by at least one chain with an observed repeating cadence."""
        periodic = {"multiple_daily", "daily", "weekly", "monthly"}
        return sorted(n for n in self.targets if self.frequency_by_target[n] & periodic)


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
        self._loads = LoadClosureService(connection, capability, cache)
        self._threex = ThreeXRepository(connection, capability, cache)
        self._analyzers = Analyzers(
            connection, capability, cache, registry=registry or ConnectorRegistry()
        )
        self._gaps = _Gaps()
        self._files: list[str] = []
        self._truncated = False
        self._skip_existing = False
        self._skipped = 0

    # --- orchestration -------------------------------------------------------------------

    def generate(
        self,
        output_dir: str | Path,
        *,
        limit: int = 15,
        catalog_cap: int = 200,
        sections: Sequence[str] | None = None,
        resume: bool = False,
    ) -> DocGenResult:
        """Render the tree. ``sections`` restricts which section writers run.

        Both extra arguments exist for one practical reason: a full-system generation runs for
        hours, and the database closes the session long before it finishes. ``sections`` splits the
        work into runs short enough to complete; ``resume`` skips detail pages already on disk, so
        re-running a section that was cut off continues instead of starting over. Generation only
        ever adds files, so separate runs compose into the same tree. Defaults reproduce the
        original single-pass behaviour.
        """
        base = _safe_output_dir(output_dir)
        self._skip_existing = resume
        cap: CapabilityRecord = self.capability
        wanted = set(SECTIONS if sections is None else sections)
        unknown = wanted - set(SECTIONS)
        if unknown:
            raise DocGenError(
                f"unknown section(s): {', '.join(sorted(unknown))}. "
                f"Valid sections: {', '.join(SECTIONS)}"
            )

        # Every section fetches its catalogue with ``limit=catalog_cap`` and then slices that list
        # by ``limit`` for the detail pages. Python slicing clamps silently, so a caller asking for
        # more pages than the catalogue holds used to get ``min(limit, catalog_cap)`` with no
        # indication the request had been reduced. Widening the catalogue to match keeps the two
        # knobs independent in the only direction that matters.
        catalog_cap = max(catalog_cap, limit)

        self._write(base, "index.md", self._index_page(cap))
        # Resolved once and shared: what the chains actually load, from RSBKDTP. This is the
        # authoritative set of periodically-loaded objects, and it drives provider and lineage
        # selection so that nothing a chain loads can be crowded out by a catalogue cap. Skipped
        # entirely when no section needs it, since walking every chain is not free.
        needs_closure = bool(wanted & {"inventory", "load-coverage", "lineage", "providers"})
        closure = self._resolve_load_closure(catalog_cap) if needs_closure else _LoadClosure()

        if "inventory" in wanted:
            self._section_inventory(base, closure)
        if "load-coverage" in wanted:
            self._section_load_coverage(base, closure)
        if "chains" in wanted:
            self._section_chains(base, limit, catalog_cap)
        if "lineage" in wanted:
            self._section_lineage(base, limit, closure)
        if "providers" in wanted:
            self._section_providers(base, limit, catalog_cap, closure)
        if "transformations" in wanted:
            self._section_transformations(base, limit, catalog_cap)
        if "transfer-rules" in wanted:
            self._section_transfer_rules(base, limit)
        if "queries" in wanted:
            self._section_queries(base, limit, catalog_cap)
        if "hana" in wanted:
            self._section_hana(base, limit, catalog_cap)
        if "scenarios" in wanted:
            self._section_scenarios(base, limit)
        # The gaps register is always written last and is always non-empty. In a staged run it must
        # accumulate: each page is built exactly once across the sequence, so the run that builds it
        # is the only one that can record its caveats, and a plain overwrite would leave the file
        # holding just the final section's findings.
        if resume:
            self._gaps.absorb(self._existing_gap_entries(base))
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

    # --- load closure (drives object selection) ------------------------------------------

    def _resolve_load_closure(self, catalog_cap: int) -> _LoadClosure:
        """Union what every chain loads, so selection follows loads rather than transformations."""
        closure = _LoadClosure()
        listed = self._unwrap("load-closure", self._chains.list_chains(limit=catalog_cap))
        if listed is None:
            return closure
        chains, total = listed
        closure.chains_total = total
        self._gaps.truncated("load-closure", len(chains), total)
        self._truncated = self._truncated or total > len(chains)

        for summary in chains:
            result = self._loads.chain_to_providers(summary.chain_id)
            if isinstance(result, UnsupportedResult):
                # Capability-gated for the whole system, not for this one chain: stop rather than
                # retry 280 times, and say so plainly instead of reporting an empty closure.
                self._gaps.unsupported("load-closure", result)
                return closure
            closure.chains_walked += 1
            for loaded in result.providers_loaded:
                closure.targets.setdefault(loaded.name, loaded)
                # One chain can load the same provider through several DTPs (often one per
                # sub-chain), which would list the chain repeatedly. Record it once: the question
                # this answers is which chains load the object, not how many DTPs each uses.
                chains = closure.chains_by_target[loaded.name]
                if summary.chain_id not in chains:
                    chains.append(summary.chain_id)
                if loaded.update_mode:
                    closure.modes_by_target[loaded.name].add(loaded.update_mode)
                if summary.frequency:
                    closure.frequency_by_target[loaded.name].add(summary.frequency)
            for caveat in result.caveats:
                self._gaps.add(f"load closure {summary.chain_id}", caveat)
            if result.truncated_recursion:
                self._gaps.add(
                    f"load closure {summary.chain_id}",
                    "sub-chain recursion hit its depth/step bound, so this chain's load list is a "
                    "lower bound",
                )
        closure.resolved = closure.chains_walked > 0
        return closure

    # --- writing + shared page furniture -------------------------------------------------

    def _write(self, base: Path, relpath: str, content: str) -> None:
        target = base / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._files.append(relpath.replace("\\", "/"))

    @staticmethod
    def _existing_gap_entries(base: Path) -> list[str]:
        """Gap bullets already recorded on disk, so a staged run's register stays cumulative.

        Reads back the register's own bullet format rather than keeping a side-car file: the page is
        the record, and a missing or unreadable one simply means nothing to merge.
        """
        target = base / "99-gaps-and-risks.md"
        if not target.is_file():
            return []
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.startswith("- **")]

    def _write_page(self, base: Path, relpath: str, builder: Callable[[], str]) -> None:
        """Write a per-object detail page, skipping the *build* in resume mode.

        Takes a callable rather than a string because building one of these pages is the expensive
        part - a lineage page walks the graph and analyses routines - so a resume has to avoid the
        work, not just the write.
        """
        rel = relpath.replace("\\", "/")
        if self._skip_existing and (base / relpath).is_file():
            # Already produced by an earlier run. Recorded in the manifest anyway, so the counts
            # describe the tree on disk rather than this call's share of it.
            self._files.append(rel)
            self._skipped += 1
            return
        self._write(base, relpath, builder())

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
                # 00-requirements is not written by this generator - it is where hand-written
                # scenario answers live. Linked anyway, and first, because those pages are the
                # narrative entry point and an unlinked page is one nobody finds. The link is
                # harmless when the directory is absent.
                "0. [Requirement answers](00-requirements/) - hand-written, per scenario",
                "1. [Inventory](01-inventory/index.md)",
                "2. [Process chains](02-process-chains/index.md)",
                "3. [Lineage](03-lineage/index.md)",
                "4. [Providers](04-providers/index.md)",
                "5. [Transformations](05-transformations/index.md)",
                "5b. [Transfer rules — BW 3.x dataflow](09-transfer-rules/index.md)",
                "6. [Queries](06-queries/index.md)",
                "7. [HANA layer](07-hana/index.md)",
                "8. [Risk scenarios](08-scenarios/index.md)",
                "",
                "**[Gaps and risks](99-gaps-and-risks.md)** - everything unverified/inaccessible.",
                "",
            ]
        )

    # --- 01 inventory --------------------------------------------------------------------

    def _section_inventory(self, base: Path, closure: _LoadClosure) -> None:
        rows = [
            "| Object type | Count | Source |",
            "|---|---|---|",
            f"| Process chains | {self._chain_total()} | RSPCCHAINATTR |",
            f"| Transformations | {self._tran_total()} | RSTRAN |",
            f"| BEx queries | {self._query_total()} | RSZCOMPDIR |",
            f"| Calc views | {self._calcview_total()} | SYS.VIEWS |",
        ]
        if closure.resolved:
            rows.append(
                f"| Objects loaded by chains | {len(closure.targets)} | RSPCCHAIN + RSBKDTP |"
            )
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
            "",
            "See **[load coverage](load-coverage.md)** for every object a chain loads, its update "
            "mode, and which enumerated providers no chain loads.",
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

    # --- 01 inventory / load coverage -----------------------------------------------------

    def _section_load_coverage(self, base: Path, closure: _LoadClosure) -> None:
        """Every loaded object with its update mode, plus providers no chain loads.

        The completeness artefact. Coverage claims are only worth anything if the denominator is
        stated, so this enumerates providers from their header tables and reports the difference
        both ways: loaded objects (from the chains), and enumerated providers nothing loads.
        """
        lines = [
            "# Load coverage",
            "",
            self._backlinks(1),
            "",
        ]
        if not closure.resolved:
            self._gaps.add(
                "load-coverage",
                "the chain -> DTP -> provider closure could not be resolved, so no coverage "
                "statement is possible; object selection fell back to the transformation catalogue",
            )
            lines.append("_Load closure unavailable on this connection; see the gaps register._")
            self._write(base, "01-inventory/load-coverage.md", "\n".join(lines))
            return

        enumerated = self._enumerate_providers()
        loaded = set(closure.targets)
        periodic = set(closure.periodic_names())
        unloaded = sorted(n for n in enumerated if n not in loaded)
        loaded_not_enumerated = sorted(n for n in loaded if n not in enumerated)

        lines += [
            f"Walked **{closure.chains_walked} of {closure.chains_total}** chains and unioned "
            f"every DTP target they reach, including through nested sub-chains.",
            "",
            "| Measure | Count |",
            "|---|---|",
            f"| Chains walked | {closure.chains_walked} |",
            f"| Distinct objects loaded | {len(loaded)} |",
            f"| Of those, loaded by a chain with a repeating cadence | {len(periodic)} |",
            f"| Providers enumerated from header tables | {len(enumerated)} |",
            f"| Enumerated providers **no** chain loads | {len(unloaded)} |",
            f"| Loaded objects not in the provider enumeration | {len(loaded_not_enumerated)} |",
            "",
            "A loaded object outside the provider enumeration is normally an InfoObject: master "
            "data is loaded by DTP like any other target, but InfoObjects are enumerated "
            "separately from InfoProviders.",
            "",
            "## Objects loaded by chains",
            "",
            "`Update mode` comes from the DTP, so full vs delta here is declared, not inferred. "
            "Where an object shows both, different chains load it differently - worth a look.",
            "",
            "| Object | Type | Update mode | Loading chains | Cadence |",
            "|---|---|---|---|---|",
        ]
        for name in closure.names:
            loaded_provider = closure.targets[name]
            chains = closure.chains_by_target[name]
            modes = ", ".join(sorted(closure.modes_by_target[name])) or "-"
            freqs = ", ".join(sorted(closure.frequency_by_target[name])) or "-"
            extra = len(chains) - _CHAINS_PER_CELL
            shown = ", ".join(chains[:_CHAINS_PER_CELL]) + (f" (+{extra})" if extra > 0 else "")
            lines.append(
                f"| {name} | {loaded_provider.type_code or '-'} | {modes} | {shown} | {freqs} |"
            )

        lines += ["", "## Enumerated providers no chain loads", ""]
        if unloaded:
            lines += [
                "Each of these exists in BW but no walked chain loads it. That is a **candidate** "
                "reading, not a verdict: it may be loaded by a DTP run outside a chain, by a "
                "process the walk could not resolve, or be a view-like provider "
                "(a CompositeProvider holds no data of its own and is not a DTP target).",
                "",
            ]
            lines += [f"- {name}" for name in unloaded]
            self._gaps.add(
                "load-coverage",
                f"{len(unloaded)} enumerated provider(s) are loaded by no walked chain; "
                "listed in 01-inventory/load-coverage.md",
            )
        else:
            lines.append("_Every enumerated provider is loaded by at least one walked chain._")

        lines.append(self._citation("RSPCCHAIN, RSBKDTP, RSDODSO, RSOADSO, RSDCUBE, RSOHCPR"))
        self._write(base, "01-inventory/load-coverage.md", "\n".join(lines))

    def _enumerate_providers(self) -> set[str]:
        """Provider names straight from their header tables, as a coverage denominator.

        Deliberately not derived from transformations or chains: the point is an independent count
        to measure those against.
        """
        names: set[str] = set()
        for logical, column in _PROVIDER_HEADERS:
            if not self.capability.is_available(logical):
                self._gaps.add(
                    "load-coverage",
                    f"`{logical}` is unavailable on this release, so its providers are missing "
                    "from the coverage denominator",
                )
                continue
            # OBJVERS = 'A' is auto-injected by the dialect for the RSD*/RSO* families, so it is
            # deliberately not repeated here.
            rows = self.select(
                self.dialect.paginate(
                    self.dialect.build_select(
                        columns=[column],
                        from_logical=logical,
                        order_by=[column],
                    ),
                    limit=_ENUMERATION_CAP,
                )
            )
            if len(rows) >= _ENUMERATION_CAP:
                self._gaps.add(
                    "load-coverage",
                    f"`{logical}` enumeration hit its {_ENUMERATION_CAP}-row bound, so the "
                    "coverage denominator is a lower bound",
                )
            for row in rows:
                value = row[0]
                if value and str(value).strip():
                    names.add(str(value).strip())
        return names

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
            self._write_page(
                base,
                f"02-process-chains/{_slug(chain.chain_id)}.md",
                partial(self._chain_page, chain.chain_id),
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

    def _section_lineage(self, base: Path, limit: int, closure: _LoadClosure) -> None:
        seeds = self._lineage_seeds(limit, closure)
        index = [
            "# Lineage",
            "",
            self._backlinks(1),
            "",
            "End-to-end data-flow graphs (Mermaid diagram + graph JSON). Seeded from every object "
            "the chains load, so anything loaded periodically has a flow page; topped up from the "
            "transformation catalogue. Routine-derived edges are advisory (dashed).",
            "",
        ]
        if not seeds:
            self._gaps.add(
                "lineage",
                "no seed objects were derivable from the load closure or the transformation "
                "catalogue; no flow pages generated",
            )
            index.append("_No lineage flows generated._")
            self._write(base, "03-lineage/index.md", "\n".join(index))
            return
        for name in seeds:
            slug = _slug(name)
            index.append(f"- [{name}]({slug}.md)")
            self._write_page(base, f"03-lineage/{slug}.md", partial(self._lineage_page, name))
        self._write(base, "03-lineage/index.md", "\n".join(index))

    def _lineage_page(self, name: str) -> str:
        lines = [f"# Lineage: {name}", "", self._backlinks(1), ""]
        graph = self._unwrap(
            "lineage", self._lineage.get_lineage(name, direction="both", depth=_LINEAGE_DEPTH)
        )
        if graph is None:
            return "\n".join([*lines, "_lineage unavailable_"])
        lines += ["## Flow diagram", "", self._mermaid(graph), ""]
        trace = self._unwrap("lineage", self._lineage.trace_to_source(name))
        if trace is not None:
            reached = ", ".join(trace.datasources_reached) or "(none reached)"
            lines += [f"- DataSources reached upstream: {reached}", ""]
        lines += _render_evidence_mix(graph)
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

    def _lineage_seeds(self, limit: int, closure: _LoadClosure) -> list[str]:
        """Objects to root lineage flows at: everything loaded first, transformations after.

        Load-closure targets lead because they are the objects that actually move data on a
        schedule. The transformation catalogue then tops the list up, which still catches an object
        that has a transformation but no chain loading it - itself worth seeing.
        """
        seen: list[str] = list(closure.names)
        if len(seen) >= limit:
            return seen[:limit]
        result = self._unwrap("lineage", self._transformations.list_transformations(limit=limit))
        if result is None:
            return seen
        known = set(seen)
        for summary in result[0]:
            name = summary.target_name
            if name and summary.target_kind in _PROVIDER_KINDS and name not in known:
                seen.append(name)
                known.add(name)
            if len(seen) >= limit:
                break
        return seen

    # --- 04 providers --------------------------------------------------------------------

    def _section_providers(
        self, base: Path, limit: int, catalog_cap: int, closure: _LoadClosure
    ) -> None:
        derived, trans_in, trans_out = self._provider_names_and_edges(catalog_cap)
        # Loaded objects lead the list so that a cap can only ever cost a provider nothing loads.
        names = list(closure.names)
        known = set(names)
        names += [n for n in derived if n not in known]
        query_by_provider = self._query_by_provider(catalog_cap)
        calcviews_by_object = self._calcviews_by_object(catalog_cap)
        index = [
            "# Providers",
            "",
            self._backlinks(1),
            "",
            "BW has no 'list all providers' call, so this list is assembled: every object the "
            "chains load (from `RSBKDTP`), then every provider-typed transformation endpoint. "
            "Loaded objects are listed first, so truncation can only drop a provider that nothing "
            "loads. Each page lists sources, targets, load edges, calc views reading it, and "
            "reports depending on it.",
            "",
            f"Loaded by a chain: **{len(closure.names)}**. "
            f"Added from the transformation catalogue: **{len(names) - len(closure.names)}**. "
            f"See [load coverage](../01-inventory/load-coverage.md) for the completeness audit.",
            "",
        ]
        if not names:
            self._gaps.add(
                "providers", "no provider names derivable from the load closure or transformations"
            )
            index.append("_No providers rendered._")
            self._write(base, "04-providers/index.md", "\n".join(index))
            return
        for name in names[:limit]:
            slug = _slug(name)
            index.append(f"- [{name}]({slug}.md)")
            self._write_page(
                base,
                f"04-providers/{slug}.md",
                partial(
                    self._provider_page,
                    name,
                    trans_in=trans_in,
                    trans_out=trans_out,
                    query_by_provider=query_by_provider,
                    calcviews_by_object=calcviews_by_object,
                    closure=closure,
                ),
            )
        self._gaps.truncated("providers", min(limit, len(names)), len(names))
        self._truncated = self._truncated or len(names) > limit
        self._write(base, "04-providers/index.md", "\n".join(index))

    def _provider_page(
        self,
        name: str,
        *,
        trans_in: dict[str, list[str]],
        trans_out: dict[str, list[str]],
        query_by_provider: dict[str, list[str]],
        calcviews_by_object: dict[str, list[str]],
        closure: _LoadClosure,
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
            lines += _render_attributes(provider)
            for caveat in provider.caveats:
                self._gaps.add(f"provider {name}", caveat)
        loaded = closure.targets.get(name)
        if loaded is not None:
            chains = closure.chains_by_target[name]
            modes = sorted(closure.modes_by_target[name])
            freqs = sorted(closure.frequency_by_target[name])
            via = f" (via sub-chain {loaded.via_subchain})" if loaded.via_subchain else ""
            lines += [
                "## How it is loaded",
                f"- Loading chains: {', '.join(chains) or '-'}",
                f"- Observed cadence of those chains: {', '.join(freqs) or 'unknown'}",
                f"- Update mode (declared on the DTP): **{', '.join(modes) or 'unknown'}**",
                f"- Example DTP: {loaded.dtp_id or '-'}{via}",
                f"- Target type code: {loaded.type_code or '-'}",
                "",
            ]
            if len(modes) > 1:
                self._gaps.add(
                    f"provider {name}",
                    f"loaded in more than one update mode ({', '.join(modes)}), so full and delta "
                    "loads both write here - check which chain wins on a given day",
                )
        elif closure.resolved:
            lines += [
                "## How it is loaded",
                "_No walked chain loads this provider._ It may be loaded by a DTP run outside a "
                "chain, or be a view-like provider that holds no data of its own. See the "
                "[load coverage](../01-inventory/load-coverage.md) audit.",
                "",
            ]
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

    def _calcviews_by_object(self, cap: int) -> dict[str, list[str]]:
        # Scales with the catalogue cap: a fixed bound here silently dropped the calc views of
        # every provider past it, which reads on the page as "no calc view reads this".
        report = self._hana.get_hana_crossings(limit=max(cap, 500))
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
            self._write_page(
                base,
                f"05-transformations/{_slug(s.tran_id)}.md",
                partial(self._transformation_page, s.tran_id),
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

    # --- 09 transfer rules (BW 3.x dataflow) ---------------------------------------------

    def _section_transfer_rules(self, base: Path, limit: int) -> None:
        """The 3.x layer: DataSources that reach BW without any 7.x transformation.

        Kept as its own section rather than folded into transformations, because these are a
        different mechanism with different tables, and conflating them would make a DataSource on a
        transfer rule look like it had no load logic at all.
        """
        report = self._unwrap("transfer-rules", self._threex.list_flows(limit=max(limit, 1)))
        if report is None:
            self._write(
                base, "09-transfer-rules/index.md", self._empty_section("Transfer rules (BW 3.x)")
            )
            return
        page = [
            "# Transfer rules (BW 3.x dataflow)",
            "",
            self._backlinks(1),
            "",
            "The 3.x path is `DataSource -> InfoSource -> transfer structure (transfer rules) -> "
            "communication structure -> update rules -> target`, against the 7.x path's single "
            "transformation plus DTP. Where a DataSource has no 7.x transformation, these rules "
            "**are** the live load logic.",
            "",
            "| Measure | Count |",
            "|---|---|",
            f"| DataSource -> transfer-structure routes | {report.total_count} |",
            f"| Active transfer structures | {report.active_transfer_structures} |",
            f"| Distinct DataSources on a 3.x route | {report.datasources_with_3x_route} |",
            f"| Of those, also having a 7.x transformation | "
            f"{report.datasources_with_7x_transformation} |",
            f"| **3.x only — no 7.x path at all** | **{report.datasources_3x_only}** |",
            f"| Active update rules | {report.active_update_rules} |",
            "",
        ]
        if report.datasources_3x_only:
            only = report.datasources_3x_only
            self._gaps.add(
                "transfer-rules",
                f"{only} DataSource route(s) have no 7.x transformation, so their load logic "
                "lives in transfer rules; lineage from RSTRAN alone stops short of them",
            )

        update_rules = self._unwrap("transfer-rules", self._threex.list_update_rules(limit=200))
        if update_rules:
            page += [
                "## Active update rules",
                "",
                "`target` is `RSUPDINFO.INFOCUBE`, which despite the column name also carries "
                "InfoObject targets for master-data flows.",
                "",
                "| InfoSource | Target | Start routine | Expert | Routines |",
                "|---|---|---|---|---|",
            ]
            for rule in update_rules:
                page.append(
                    f"| {rule.infosource or '-'} | {rule.target or '-'} | "
                    f"{'yes' if rule.has_start_routine else 'no'} | "
                    f"{'yes' if rule.expert_mode else 'no'} | {rule.routine_count} |"
                )
            page.append("")

        page += [
            "## Flows",
            "",
            "`rules` is the field-rule count; the rest say how those fields are derived. A flow "
            "with routines or formulas holds logic outside these tables.",
            "",
            "| DataSource | Source system | Transfer structure | Rules | Routine | Formula | "
            "Constant | Start routine | Update targets |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for flow in report.flows:
            page.append(
                f"| {flow.datasource} | {flow.logical_system or '-'} | "
                f"{flow.transfer_structure or '-'} | {flow.rule_count} | {flow.rules_with_routine} "
                f"| {flow.rules_with_formula} | {flow.rules_with_constant} | "
                f"{'yes' if flow.has_start_routine else 'no'} | "
                f"{', '.join(flow.update_rule_targets) or '-'} |"
            )
        for caveat in report.caveats:
            self._gaps.add("transfer-rules", caveat)
        page.append(self._citation("RSISOSMAP, RSTS, RSTSRULES, RSUPDINFO, RSUPDROUT, RSAROUT"))
        self._write(base, "09-transfer-rules/index.md", "\n".join(page))

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
            self._write_page(
                base,
                f"06-queries/{_slug(label)}.md",
                partial(self._query_page, q.compuid, label),
            )

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
            lines += _render_value_altering_elements(query)
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
            lines.append(f"### [{finding.severity}] {finding.title}")
            lines.append(f"- Affected: {objects}")
            # Detail carries the evidence behind the finding - the measured values, which code was
            # read, and what was checked and found absent. Omitting it left the page with a
            # recommendation and no way to see what it rested on, which is the opposite of the
            # provenance the corpus exists to provide.
            if finding.detail:
                lines.append(f"- Evidence: {finding.detail}")
            lines.append(f"- Recommendation: {finding.recommendation}")
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
