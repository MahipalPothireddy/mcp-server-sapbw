# SAP BW technical-discovery MCP

<sub>Distribution name: `mcp-server-sapbw` · Python package: `mcp_server_sapbw`</sub>

A read-only, system-agnostic [MCP](https://modelcontextprotocol.io) server that exposes the
metadata of any **SAP BW-on-HANA** system as model-callable tools, resources, and prompts. Point
it at DEV, QA, PRD, or a different client's landscape through a named connection profile — no code
changes. It auto-detects BW release and object-model variant at connect time, answers
impact-analysis and incident-triage questions in one call (including dependencies invisible to BW's
own where-used lists), and can render a full markdown knowledge base on demand.

> **Status: functional.** The metadata extraction, lineage, diagram rendering, routine analysis,
> BEx query, HANA, provider-health, security, risk-analyzer, and knowledge-base subsystems are
> implemented:
> **58 tools, 7 resource templates and 6 prompts**, most of them exercised against a live BW 7.50
> system.
> What the server does with each metadata object it declares is published in
> [`docs/capability-contract.md`](docs/capability-contract.md) and enforced by CI; ask
> `bw_capability_report` for the same thing crossed with what your own system actually has.

## Built, versus proven

These are different questions, and the second is the one a buying decision rests on. Every capability
carries both:

| | |
|---|---|
| **Implementation** | `implemented` · `partial` · `discovery_only` · `planned` · `unsupported` · `deprecated` |
| **Validation** | `customer_validated` · `integration_tested` · `unit_tested` · `not_validated` |

`integration_tested` means the capability was read through a real feature against a live BW system
and the output inspected — and the release it was proven on is recorded, because these tables differ
across releases. `unit_tested` means the offline suite exercised it against synthetic fixtures.
`not_validated` means no test touched it; a reader may still exist.

Validation is **measured, not asserted**: the SQL dialect records which logical tables the suite
actually asks for, so `unit_tested` is observed. It is never inferred upward — an implemented
capability that no test touches reports `not_validated`, and a test asserts that some do, because if
every implemented capability reported as validated the column would be decoration.

On the reference BW 7.50 system, of 78 capabilities usable there: **53 integration-tested, 15
unit-tested only, 10 not validated, and 0 validated on a customer's own system.** That last number is
reported rather than omitted — it is the honest position of a pre-1.0 build, and closing it is an
onboarding exercise, not a development one.
> Still planned: a BI connector for scenarios 9.7/9.8 (report schedules and dashboards live outside
> BW, so those two analyses return a template naming the connector required). The ECC source-system
> connector is implemented (ADT, read-only) but not yet exercised against a live source system. See
> `PROGRESS.md` for the build log and `.kiro/specs/mcp-server-sapbw/` for the spec.

## What it does

- **Process chains & scheduling** — structure, recursive meta-chains, observed cadence (from run
  history and job periodicity, never from names), and runtime statistics (p95, success rate,
  critical path).
- **Load closure** — what a chain actually loads, walked through its nested sub-chains, and which
  chains load a given provider, with the cadence that governs it.
- **Load lineage** — a directed graph across transformations, DTPs, providers, calc views, and
  queries, including advisory edges parsed from ABAP routine source.
- **Data-flow diagrams** — the same graph rendered as an image (SVG or PNG) entirely locally, with
  node types colour-coded and advisory edges dashed so a heuristic never looks like a fact.
- **Transformations & routines** — field mappings, rule types (aggregation behaviour, key fields,
  constants, declared lookups), and full ABAP source with parsed table dependencies and
  anti-pattern detection — plus a portfolio-wide register ranking every routine in the system.
- **BEx queries** — complete definitions with field-level lineage down to the DataSource field, and
  a designed-report vs. ad-hoc-navigation distinction.
- **HANA calc views** — dependencies, CompositeProvider part resolution, and every BW↔HANA crossing.
- **Provider health** — how much data a provider holds (active vs. changelog vs. inbound, never
  summed) and how current it is, from BW's own request ledger.
- **Source-system topology** — which systems feed the warehouse, and which DataSources carry
  extractor enhancements; optionally the exit ABAP itself, read from the source system over ADT.
- **Descriptions** — for every object, with explicit provenance (stored vs. generated).
- **Snapshots & environment comparison** — what changed since last week, and what differs between
  QA and production, with the BDLS rewrite and locally-rebuilt objects corrected for rather than
  reported as thousands of differences.
- **Compound analysis** — one call per analyst question (an object, a report, a chain, a proposed
  change, a wrong number), composed from the granular readers and returned with an audit row per
  section naming the tool that reproduces it.
- **Risk analyzers** — the eight landscape-specific analyses (latency contracts, schedule risk,
  layer violations including circular dependencies of any length) plus decommission-candidate
  detection.
- **Knowledge base** — a full markdown documentation set rendered on demand.

## When a call cannot answer

Every failure comes back as a structured result, never an opaque error string:

```json
{
  "status": "error",
  "code": "read_only_violation",
  "category": "guardrail",
  "retryable": false,
  "message": "...",
  "remedy": "This server issues SELECT only ...",
  "detail": { "exception": "ReadOnlyViolation", "tool": "bw_get_chain" }
}
```

`code` is stable and safe to branch on. `category` groups codes a caller would treat alike
(`not_found`, `unsupported`, `invalid_request`, `partial`, `configuration`, `transport`, `guardrail`,
`internal`). `retryable` answers the only question a retry loop has. `remedy` says what to do next.

An exception's own message is forwarded only for families whose text is scrubbed at the raise site;
anything else is reported by type, so a failure path cannot carry a host name or a credential into a
response.

## One name for one object

Every object type comes from a single vocabulary, and every object carries a canonical reference:

```json
"ref": { "object_type": "infocube", "name": "SALES_CUBE", "id": "infocube:SALES_CUBE" }
```

`ref.id` is the key to join a `bw_describe_object` result against a `bw_get_lineage` node, a
`bw_search_objects` hit, or a knowledge-base page. It is type-qualified because BW technical names
are only *near*-unique — a DSO and an InfoObject can share one — so an unqualified name is not a safe
graph key. Legacy spellings still normalise, and the TLOGO code BW stored is kept as `subtype` so an
undecoded value stays visible.

## How firmly a fact is established

Every fact that is not simply a row value carries an `evidence` object, in one vocabulary shared by
every subsystem, so a mixed set of findings can be sorted and filtered by how much to trust it:

| `basis` | Meaning |
|---|---|
| `observed` | A metadata row states it |
| `derived` | Computed from rows by a documented rule — a join, a dictionary decode, an aggregation of run history |
| `inferred` | Rests on a naming convention or a heuristic parse of ABAP; can be wrong even when every input was read correctly |
| `unknown` | Could not be established, and is reported as such rather than omitted |

`method` keeps the specific mechanism (`bw_provider_view`, `bic_table_naming`, `dictionary_domain`,
`routine_select_parse`, `observed_run_history`, …) so nothing is flattened, `detail` says why *this*
fact was concluded, and `completeness` (`complete` / `lower_bound`) is a separate statement: whether
a set is exhaustive is a different question from whether each member is right.

A lineage graph also reports the mix — "7 of 75 edges (9%) are inferred" — because a graph is a
different object depending on whether 2 or 150 of its edges came from a routine parse.

## Supported releases

| Release | Status |
|---|---|
| BW 7.5 (on HANA) | **Validated live** — the reference system for the build (SAP_BW 7.50) |
| BW 7.4 (on HANA) | **Not tested live.** Portability rests on the capability resolver, which is tested against absent-table shapes (see below) |
| BW/4HANA | **Not tested live.** Same as above |

Portability is achieved by a runtime **capability resolver** that discovers which tables and
object-model variants actually exist before any tool builds SQL — no release is assumed. On a
release where a table is absent, the affected tool returns a structured "unsupported on this
release" result naming what is missing, rather than guessing.

Being precise about what that guarantee is worth, since only one release has been seen live:

- **Checked.** `tests/test_release_portability.py` runs all 37 read entry points against seven
  capability shapes — no advanced DSO / CompositeProvider, no classic cube or BW 3.x stack, no HANA
  catalogue, no BEx tables, no run history, and one where *nothing* is available. In every shape, no
  entry point builds SQL naming an absent table and none raises; the affected ones return
  `UnsupportedResult`. That is the mission's "no tool ever queries a non-existent table" criterion,
  enforced by the suite rather than by inspection.
- **Not checked.** Whether those shapes match what SAP actually ships in 7.4 or BW/4HANA. The
  fixtures deliberately do not claim to be a release inventory — the server's rule against asserting
  unread metadata applies to its own tests too. What is proven is that *absence is handled*,
  whichever tables turn out to be absent.

If you run this against a release other than 7.5, `bw_system_profile` reports exactly what it found
and `99-gaps-and-risks.md` in the generated docs lists what could not be read. Both are worth
reading first, and we would welcome the capability report as an issue.

## Quickstart

```bash
# 1. Install (via uv)
uvx --from mcp-server-sapbw mcp-server-sapbw

# 2. Configure profiles (never commit these)
cp profiles.example.yaml profiles.yaml
cp .env.example .env
# edit .env with read-only HANA credentials

# 3. Register with your MCP client (see below)
```

## MCP client configuration

Local **stdio** transport. Example (Kiro `mcp.json`):

> **Keep the config key short.** The server reports itself as *SAP BW technical-discovery MCP*, but
> most clients build each tool's visible name by prefixing **the key you choose here**. With
> `"sapbw"` the longest tool becomes `mcp_sapbw_bw_list_extractor_enhancements` (40 characters),
> comfortably inside the 64-character limit. A long or hyphenated key can push names over it, or
> produce an invalid identifier, and affected tools are then silently dropped.

```json
{
  "mcpServers": {
    "sapbw": {
      "command": "uvx",
      "args": ["--from", "mcp-server-sapbw", "mcp-server-sapbw"],
      "env": {
        "BW_PROFILES_PATH": "${BW_PROFILES_PATH}",
        "BW_PRD_PASSWORD": "${BW_PRD_PASSWORD}",
        "FASTMCP_LOG_LEVEL": "ERROR"
      },
      "disabled": false,
      "autoApprove": ["bw_list_systems", "bw_system_profile", "bw_search_objects"]
    }
  }
}
```

Auto-approve only cheap, read-only tools. Leave long-running queries and file generation subject to
prompting.

**Running from source (before a PyPI release):** point `command` at the console script in the
project venv (e.g. `.../mcp-server-sapbw/.venv/Scripts/mcp-server-sapbw.exe` on Windows), or use
`uvx --from <path-to-checkout> mcp-server-sapbw`. On startup the server loads a git-ignored `.env`
from `BW_DOTENV_PATH`, else next to `BW_PROFILES_PATH`, else `./.env` (existing environment
variables always win). So a client only needs `BW_PROFILES_PATH` set — the connection secrets stay
in `.env` and are picked up automatically; they are never written to the MCP config.

## Tool catalog

Every tool takes a `system: str` naming a configured profile; the server stays stateless across
calls (connections are pooled per profile). List tools accept `limit`/`offset` and return a
`total_count`. Every result carries provenance (the source table and key it came from).

### System

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_systems` | — | Configured profiles and their discovery status (no host/credentials) |
| `bw_system_profile` | `system` | Release, ABAP schema, object-model variants, log window, table presence/counts |
| `bw_refresh_capabilities` | `system` | Re-run capability discovery, replacing the cached record |
| `bw_capability_report` | `system` | Which questions resolve on *this* system: contract state × table presence, verdict per capability |
| `bw_access_report` | `system` | Which deployment mode is in force, which reads were refused, and the exact grants to fix them |
| `bw_support_matrix` | `tool?`, `release?`, `system?` | Which tool works on which BW release — **no connection required** |
| `bw_performance_profile` | `tool?`, `growth?` | What each tool costs and what a bigger system does to it — **no connection required** |
| `bw_cache_status` | `system` | What extracted metadata and how many snapshots are on local disk, and where |
| `bw_refresh_cache` | `system`, `scope="all"` | Invalidate cached extracts by scope |

#### Will this work on my system?

Three answers, in increasing specificity, and the first needs nothing from you:

| Question | Ask | Needs a connection |
|---|---|---|
| Which tools work on BW 7.4 / 7.5 / BW/4HANA? | `bw_support_matrix` | no |
| Which questions resolve on *my* system? | `bw_capability_report` | yes |
| What does this server do with metadata object X? | [`docs/capability-contract.md`](docs/capability-contract.md) | no |
| What do we grant the connecting user? | [`docs/deployment-modes.md`](docs/deployment-modes.md) | no |
| What is this user actually allowed to read? | `bw_access_report` | yes |
| What will this cost on a system our size? | [`docs/performance.md`](docs/performance.md) / `bw_performance_profile` | no |

A fourth answer matters when the first three come back thin: **a refused read is not a missing
feature.** A user without SELECT on `DD02L` used to make the server report `adso: false` and then
"not available on BW 7.50" — pointing you at a BW upgrade for something one `GRANT SELECT` fixes.
Denial and absence are now recorded separately, and `bw_access_report` names the grant.

`bw_support_matrix` is keyed by **tool**, because that is the unit the question is asked in. Its
`requires` field bridges to the capability contract and is **measured**, by attributing each metadata
read to the tool that caused it while the offline suite runs — so it cannot drift the way a
hand-written mapping across 58 tools would. It is a lower bound: everything listed really is read,
and a code path no test reaches contributes nothing.

There is deliberately no `supported` verdict. Every value says where the claim comes from:
`verified` (read through a feature on that release, output inspected), `expected` (implemented, but
not everything verified there), `unverified` (**nobody has run it against that release — not a
prediction**), `needs_connector`, `unknown`.

Only **BW 7.50** has been verified (SAP_BW 750, HANA 2.0): 41 tools `verified`, 15 `expected`, 2
connector-gated. Every other release reports `unverified` for every tool. That is an absence of
evidence stated rather than filled in — which metadata objects a release carries is exactly what the
capability resolver discovers at connect time, and predicting it from a version number would be
guesswork dressed as a support statement. The matrix names the capabilities that decide the answer on
an unverified release, so a proof of concept has a scope. Published as
[`docs/support-matrix.md`](docs/support-matrix.md) and checked by CI.

### Compound analysis

Each of these composes six or seven of the granular tools below into one answer. They exist
**alongside** the granular tools, not instead of them: if you know exactly what you want, ask for
exactly that.

| Tool | Parameters | The question it answers |
|---|---|---|
| `bw_analyze_object` | `system`, `name`, `depth=2`, `detail=auto` | Everything about one provider or InfoObject |
| `bw_analyze_query` | `system`, `query`, `detail=auto` | What a report reads, who sees what, when its data is current |
| `bw_analyze_process_chain` | `system`, `chain_id`, `days=90` | What a chain does, what it loads, how reliably it runs |
| `bw_assess_change_impact` | `system`, `name`, `depth=3`, `detail=auto` | What a change reaches, and what to verify before transporting |
| `bw_troubleshoot_missing_data` | `system`, `target`, `detail=auto` | Why a report shows wrong or missing data, layer by layer |

All five return the same envelope, so you learn one contract:

- **`summary`** — the answer as factual sentences, each supported by a section that actually ran.
- **`dependencies` / `consumers`** — normalised across sections, so one list answers "what feeds
  this" regardless of which reader established each link. Each carries `advisory`, set only where
  the link was *derived* (parsed from ABAP, or a generated table name resolved by convention).
- **`risks`** — judgements, always separable from fact, most severe first, each with a
  recommendation and citing the record it came from. No scenario analyzer is re-run: a risk is a
  reading of a record the analysis already holds.
- **`steps`** — an audit row per reader: its status, the physical tables it read, and **the granular
  tool that reproduces it**. That last field is what makes a composed answer checkable rather than
  merely detailed, and a test asserts every cited name is actually registered.
- **`limitations`** — what cannot be concluded, with a machine-readable `reason`
  (`unsupported_on_release`, `heuristic_lower_bound`, `budget_exhausted`, `truncated`,
  `metadata_dead_end`, `connector_not_configured`, `reader_caveat`). Constituent readers' own
  caveats are carried up rather than dropped — merging results without them produces an answer more
  confident than any of its parts.
- **`confidence`** — coverage and evidence basis as separate components, **never one number**. A
  release missing a metadata table and a dependency parsed out of ABAP are both "less certain", but
  one is fixed by a different BW release and the other cannot be fixed at all; collapsing them into
  a percentage would hide which.
- **`next_actions`** — the next step as a call you can actually make, with the tool and arguments
  named, generated from what was found rather than from a template.

Three behaviours are specific to these tools:

- **A section that could not be read is never an empty one.** `unsupported`, `failed`,
  `connector_required` and `skipped_budget` are distinct statuses, so "this object has no consumers"
  and "consumers cannot be read on this release" never come back the same.
- **A partial answer beats no answer.** Five readers draw on one per-call budget. When it runs out
  mid-composition the sections already gathered are returned, the rest are recorded
  `skipped_budget`, and `stopped_on_budget` is set — where a granular tool returns a `BudgetResult`
  and nothing else.
- **`unsupported` is not `not_found`.** If the release cannot report the subject's object type you
  get an `UnsupportedResult`, not "no such object" — those have different remedies, and reporting
  the first as the second sends you hunting for a typo in a name that is spelled correctly.

`detail` bounds the embedded payloads with the same rule `bw_describe_object` and `bw_get_lineage`
use; counts stay exact and each trimmed payload names the resource holding the whole record.

### Snapshots and environment comparison

BW answers neither "what changed since last week" nor "what differs between QA and production". A
transport log says what *moved*, not what the result was, and nothing at all about a change made
outside transport.

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_create_snapshot` | `system`, `families?`, `keep=true` | Capture the system's structure as fingerprints, for comparing later |
| `bw_list_snapshots` | `system?`, `limit=50` | Stored snapshots, newest first, as summaries |
| `bw_compare_snapshots` | `system`, `left`, `right?` | Diff two stored snapshots — or a stored one against the system as it is now |
| `bw_compare_systems` | `left_system`, `right_system`, `families?` | Capture both systems and diff them: DEV vs QA, QA vs production |

A snapshot holds a **fingerprint per object, not a copy of the metadata**: a reference system's
4,542 dataflow objects take 1.4 MB and 2 seconds, and comparison becomes a set operation.
`families` defaults to providers, transformations, chains, DataSources and DTPs; `queries` and
`infoobjects` are available and an order of magnitude larger.

Four corrections are applied before anything is called a difference, and each is reported rather
than assumed. All four were found by running the comparison against two real environments — every
one of them, left unhandled, produces a diff that looks authoritative and carries no information:

- **Volatile facts are never read.** Timestamps, last-changed-by, last-used dates and record counts
  are not selected at all, so they cannot reach a fingerprint. Include them and every object is
  "changed" on every run.
- **Environment-specific names are separated from identities.** A DataSource endpoint is stored as
  `<DATASOURCE><padding><LOGSYS>` and BDLS rewrites the suffix per environment; 988 of 1,451 DTPs
  carry one. Split out, the logical system becomes a fact that can differ on its own instead of
  making every DataSource and DTP look replaced.
- **Objects re-created under a new technical id are matched on their endpoints.** Transformations
  and DTPs are named by a generated id: between two real environments only 471 of ~1,270
  transformation ids matched, while 779 of the 800 "added" were the same dataflow rebuilt by hand.
  Those pairs are listed in `rekeyed`; `added` and `removed` stay the plain set difference.
- **Capability parity is checked first.** A metadata table present on one system and absent on the
  other would surface as thousands of removed objects. Families only one side can report are
  excluded and named, and `comparable` is set to false.

`changed_by_fact` is the field that makes a large diff readable: on the reference comparison it
reported 852 `SRC_LOGSYS` + 814 `LOGSYS` + 2 `SRCTLOGO`, so 1,664 of 1,666 changed objects were BDLS
doing its job and exactly two were real structural differences.

Snapshots honour the same `cache_enabled` switch as the extract cache — a snapshot names every
provider, transformation and chain in the system, so a profile that keeps nothing at rest gets no
store, and `bw_compare_systems` (which captures both sides live) still works.

### Process chains

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_chains` | `system`, `name_pattern?`, `active_only=true`, `limit`, `offset` | Chains filtered by name pattern (see [Name filters](#name-filters)) / active status |
| `bw_get_chain` | `system`, `chain_id` | Structure, processes, event-linked edges, nested sub-chains (recursive) |
| `bw_get_chain_runtimes` | `system`, `chain_id`, `days=90` | min/median/mean/p95/max, success rate, bottleneck steps over the measured window |
| `bw_get_schedule_matrix` | `system`, `active_only=true`, `window_days=30`, `limit`, `offset` | Chain × observed frequency × typical start × p95 completion |
| `bw_get_load_closure` | `system`, `chain_id?` \| `provider?` | What a chain loads (walked through nested sub-chains) — or which chains load a provider, with the cadence that governs it |

Cadence is classified from the **observed median gap between runs**, with runs-per-day only used to
promote a daily chain to intraday. A chain with a single recorded run is reported `unknown` with low
confidence rather than force-fitted to a band. The reference date is the latest run in the system,
not today, so a restored copy is not read as dormant.

### Objects & search

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_search_objects` | `system`, `pattern`, `object_types?`, `match_descriptions=true`, `limit`, `offset` | Fuzzy search by technical name or description across object types (see [Name filters](#name-filters)) |
| `bw_describe_object` | `system`, `name` | Universal deep-dive: type, fields, key, parts, description (stored vs. generated) |

#### Name filters

Every name filter (`bw_search_objects.pattern`, `bw_list_chains.name_pattern`,
`bw_list_transformations.source_name` / `target_name`) follows one rule:

- a bare term is a **case-insensitive substring match**, with `_` matched **literally** — so
  partial BW names like `SD_O3` work as expected, and a DataSource endpoint (stored as
  `<DATASOURCE><padding><LOGSYS>`) is found by its DataSource name alone;
- a term containing `%` is passed through as an authored SQL `LIKE` pattern, so you control the
  wildcards (`LOAD%` to anchor the start, `%_DSO` to use `_` as a single-character wildcard).

Identity parameters are not patterns: `chain_id`, `tran_id`, `query`, `view_name`, `provider`, and
`owner` are matched exactly.

### Transformations & routines

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_transformations` | `system`, `source_name?`, `target_name?`, `with_routines_only=false`, `limit`, `offset` | Transformations filtered by endpoint pattern (see [Name filters](#name-filters)) or routine presence |
| `bw_get_transformation` | `system`, `tran_id` | Header, field-level rule mappings, routine references |
| `bw_get_routine_code` | `system`, `tran_id` | Full ABAP source for start/end/expert/field routines |
| `bw_analyze_routine` | `system`, `tran_id` | Parsed table dependencies + anti-patterns (heuristic lower bound) |
| `bw_get_routine_register` | `system`, `limit=50`, `offset`, `parse_budget=100` | **Every routine in the system, ranked** by anti-pattern count then size |

`bw_get_transformation` reports each rule's aggregation behaviour (direct assignment vs. summation
vs. min/max — decoded from the ABAP dictionary, not guessed), key fields, constant values, and
**declared** lookups from the `RSTRANSTEP*` tables. A declared lookup is exact, unlike a
routine-parsed one; where a lookup miss substitutes a constant instead of failing, the load changes
data silently and the analyzer says so.

The register's line counts and portfolio totals cover **every** routine, but pattern detection
covers only the largest `parse_budget` of them, because parsing means fetching source. An entry
with `analyzed=false` therefore reports **no** pattern counts rather than zeroes — zeroes would
read as "this routine is clean".

### BW 3.x dataflow

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_3x_flows` | `system`, `datasource?`, `only_without_transformation=false`, `limit`, `offset` | DataSources reaching BW through a 3.x transfer structure, with their rule profile |
| `bw_get_transfer_rules` | `system`, `transfer_structure` | Field-level transfer rules for one transfer structure |
| `bw_list_update_rules` | `system`, `limit=100` | Active 3.x update rules: InfoSource → target |

Not legacy trivia on a 7.50 system. The 3.x path is `DataSource → InfoSource → transfer structure
→ communication structure → update rules → target`, against the 7.x path's single transformation
plus DTP. Where a DataSource has **no** 7.x transformation its transfer rules *are* the live load
logic — over a thousand of them on the reference system, mostly master data — and lineage that
ignores them stops dead at that DataSource. Read `has_seven_x_transformation` first; set
`only_without_transformation` to list just those.

A rule that is a constant or a direct assignment is fully described by the rule itself. A
conversion routine or a formula holds its logic elsewhere, so those are reported as the mechanism
rather than as resolved logic.

### Lineage

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_get_lineage` | `system`, `name`, `direction="both"`, `depth=6` | Directed data-flow graph, with advisory routine edges |
| `bw_impact_analysis` | `system`, `name`, `depth=3` | Full downstream blast radius, **including routine-embedded consumers** invisible to BW where-used |
| `bw_trace_to_source` | `system`, `name`, `depth=8` | Trace upstream, hop by hop, to the DataSource boundary |
| `bw_render_lineage` | `system`, `name`, `direction="both"`, `depth=4`, `image_format="png"`, `output_dir?` | The same graph **as an image**, plus structured metadata |

Diagrams are laid out left-to-right by dependency depth, colour-coded and shaped by BW object type,
with advisory edges (routine-derived, or resolved by naming convention) drawn dashed and grey, edge
labels for routine and calc-view logic, a legend, and an on-canvas warning when the graph was
truncated. Rendering is **entirely local** — SVG needs nothing beyond the standard library, PNG
needs the `viz` extra. No diagram content ever leaves the machine; a hosted renderer would
exfiltrate customer object names.

### BEx queries

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_queries` | `system`, `provider?`, `owner?`, `origin="all"`, `limit`, `offset` | Executable BEx queries (not reusable components), each with its origin |
| `bw_get_query` | `system`, `query` | Definition: element tree, restrictions, variables with processing types |
| `bw_get_query_lineage` | `system`, `query` | Field-level lineage per InfoObject toward the DataSource; customer-exit dead ends flagged |
| `bw_get_query_usage` | `system`, `query`, `stale_days=365` | Last-used and decommission-candidate flag |

`origin` separates a **designed** query — authored in Query Designer, i.e. a maintained report —
from an **ad_hoc** one, whose technical name SAP prefixes `!!` because it was created straight in
the BEx Analyzer. Use `origin="designed"` when counting real reports. The classification reads the
shape of the technical name, since BW stores no flag for it, and the response says so.

### Providers & data currency

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_get_provider_health` | `system`, `provider`, `object_type?` | How much data a provider holds and how current it is |
| `bw_find_unused_providers` | `system`, `limit` | Providers with **no maintained consumer** — decommission candidates |

Volume reports active, inbound (activation queue) and changelog rows **separately**; summing them
would hide changelog bloat. Data age is measured against the latest request in the system rather
than today. "Could not locate the generated tables" and "the tables exist and are empty" are
reported as different facts, because conflating them claims a provider is empty when it is loading
fine.

`bw_find_unused_providers` reports a provider only when three consumer routes all come up empty: it
feeds no transformation, no Query-Designer query reads it, and it is no CompositeProvider part. That
last route matters — a CompositeProvider consumes its parts through a generated calc view rather
than a transformation, so ignoring it would flag every DSO beneath one. Consumption from outside BW
is **not** covered; check `bw_get_hana_crossings` before acting.

### Source systems & extractor enhancements

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_get_source_systems` | `system` | Which systems feed this BW system, and of what kind |
| `bw_list_extractor_enhancements` | `system`, `limit=50` | DataSources whose extract structure carries customer-namespace fields |
| `bw_get_extractor_exit_code` | `ecc_system?`, `include_source=false`, `datasources?` | The exit **ABAP behind** those enhancements, read from the source system over ADT |

Topology is built from the logical systems the DataSources actually extract from, compared against
the source-system registry — so it surfaces logical systems that DataSources reference but the
registry does not know, the usual signature of a system copy where BDLS was not run. System kinds
decoded from the ABAP dictionary are labelled as such; codes the dictionary does not document carry
a conventional reading labelled advisory.

Reading exit ABAP needs an optional `ecc_systems` profile (see `profiles.example.yaml`) and the
`ecc` extra. That connection is **GET-only**, never requests a CSRF token, and runs a stateless ADT
session so it takes no locks. Risk is attributed per `CASE` branch, not per include: one include
serves every enhanced DataSource, so crediting the whole include's table reads to one of them would
manufacture false findings.

**An empty-looking include does not mean a trivial enhancement.** Many sites keep no logic in
`ZXRSAU0n` at all: it builds a program name from the DataSource and calls it
(`PERFORM ... IN PROGRAM (name)`), which ABAP resolves at runtime, so nothing a static reader sees
reflects what the enhancement does. That dispatch is detected and reported as `dynamic_dispatch`, the
naming rule is read out of the source rather than assumed, and passing `datasources` resolves each
`<prefix><DATASOURCE>` satellite program and analyses it — one program per DataSource, so its table
reads and per-record `SELECT`s attribute exactly. On the reference system the include's `CASE` named
10 DataSources while 26 satellite programs existed. Probes are one GET each and bounded by
`max_satellite_fetches` (default 400); a namespaced DataSource resolves with its namespace stripped
(`/PARTNER/SOME_DS` → `<PREFIX>SOME_DS`), and a probe that finds nothing is reported as a checked
absence rather than omitted.

**No naming convention is assumed.** The prefix is read out of your own exit ABAP — whatever it
concatenates — so nothing needs configuring, and any customer-namespace form works (`Z…`, `Y…`,
`/PARTNER/…`). If your site uses a different prefix per DataSource kind, for example one for
transaction data and another for master data, each is attributed to the exit slot whose dispatch
produced it, so the distinction is reported rather than flattened. `satellite_program_prefixes` on
the profile is a supplement for sites whose name-building the parser cannot read; it defaults to
empty, and an empty list costs no requests.

### HANA layer

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_calc_views` | `system`, `bw_consuming_only=false`, `limit`, `offset` | Calc views (`_SYS_BIC`), optionally only those reading BW tables |
| `bw_get_calc_view_lineage` | `system`, `view_name` | A calc view's direct base tables (resolved to BW objects, advisory) **and the InfoProviders consuming it** |
| `bw_get_hana_crossings` | `system`, `calc_view?`, `limit`, `offset` | Every BW↔HANA boundary crossing, both directions, each with how its BW side resolved |

The BW side of a crossing is either a `/BIC/` table (resolved by naming convention, advisory) or a
BW-generated `0BW:BIA:<PROVIDER>` view. For the latter the provider name is parsed from the view
name and its **type confirmed** against the provider header tables, which yields the
calc-view → CompositeProvider hop that BW's own where-used lists do not report. Each crossing
carries `resolution` (`bic_table` / `bw_provider_view` / `unresolved`) so an unverified entry is
never mistaken for a confirmed one.

### Security (analysis authorisations)

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_security_overview` | `system`, `limit=200` | Row-level security posture: catch-all authorisations, unrestricted users, coverage gaps. No concrete values |
| `bw_list_analysis_auths` | `system`, `include_generated=true`, `limit`, `offset` | Authorisations by **shape**: which characteristics, how many ranges, catch-all or not |
| `bw_get_analysis_auth` | `system`, `name` | One authorisation in full, **including its value ranges** and assigned users |
| `bw_get_query_auth_exposure` | `system`, `query` | Whether a query returns different data per user, and on which characteristics |

This subsystem reads a different class of data from the rest of the server. `RSECVAL` holds
permission *values* — "cost centres 1000–1999" is a statement about what a named person may see —
so three constraints are enforced in code rather than by convention:

- **Nothing here is cached, at any tier.** Every other repository persists extracts to the SQLite
  cache. This one is constructed without a cache and ignores one if passed: a stale answer to "who
  can see this" is worse than a slow one, and permission data on disk widens the cache file's blast
  radius. A regression test asserts the repository has no cache.
- **Values are opt-in.** Listing and overview return shape only, so a landscape-wide question cannot
  incidentally place a permission dump into the transcript. Only `bw_get_analysis_auth` returns
  ranges, and its payload is labelled `contains_data_values`.
- **Silence is not safety.** These tables are frequently unreadable by a locked-down reporting user —
  they *are* the authorisation model. Absent or unreadable is reported as a documented gap, never as
  "no authorisations exist". An unreadable assignment table yields `null` user counts, never `0`.

Special values are decoded rather than passed through: `:` grants **aggregated access only** (a total
but not the rows behind it, routinely misread as no access), `#` is the unassigned member, `*` is
everything. A range whose value starts `$` resolves per user at runtime and is flagged, because
metadata cannot state its effective scope. `0BI_ALL` holders are listed as unrestricted rather than
counted as governed. Column names are resolved from `DD03L` before any SQL is built, so a release
with a different `RSEC*` layout degrades to a gap instead of raising.

The finding to look for is `uncovered_characteristics`: a characteristic flagged
authorisation-relevant that no authorisation covers returns **no data** to every user without a
catch-all. That is a live configuration fault, and it is invisible unless both sides are compared.

### Risk analyzers (mission Section 9)

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_check_load_latency` | `system`, `limit=25` | Scenario 9.1: full-update loads whose routines look up other objects (stale-data risk). Loads with no resolvable lookup have no contract to check and are counted in the caveats rather than reported as findings |
| `bw_check_schedule_risk` | `system`, `limit` | Scenario 9.7: report schedules vs. feeding-chain p95 (needs a BI connector) |
| `bw_find_layer_violations` | `system`, `max_dso_depth=3`, `limit` | CompositeProvider→DSO, CompositeProvider→InfoObject, deep DSO stacks, **circular dependencies** |
| `bw_review_scenario` | `system`, `scenario`, `limit=50` | Run any analysis by id (`9.1`–`9.8`, `layer_violations`, `unused_providers`) |

Circular dependencies are the severe ones. A transformation whose source and target are the same
object makes its own load non-repeatable: the output depends on what the target already held, so a
failed request cannot simply be re-run. A loop across two or more objects has no correct load order
at all, which is why scheduling cannot fix it. Loops are detected **at any length** and each finding
names every object involved; the only bound is the edge scan, and the report says so.

(These are dependency cycles in the metadata. They are not "write-back", which in BW means planning
data written back to a provider — a different feature. This server issues `SELECT` only and never
writes to BW.)

Scenario 9.1 compares each full-update load against the **observed cadence** of the objects its
routines look up, so a stale-data risk is substantiated rather than assumed, and escalates only when
the cadence contract is actually violated. Scenario 9.6 reports the appended fields as
metadata-confirmed evidence; with an `ecc_systems` profile configured it also reports which tables
the exit branch reads and escalates to high severity on a per-record `SELECT`.

### Documentation

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_generate_docs` | `system`, `output_dir?`, `limit=15` | Render the full markdown knowledge base to a git-ignored directory; returns a manifest |

### Prompts (analyst workflows)

`analyze_impact`, `troubleshoot_missing_data`, `document_dataflow`, `review_scenario`,
`onboard_analyst`, `pre_change_checklist` — each takes `system` (plus an object/scenario argument)
and returns a guided workflow composing the read-only tools above.

### Resources

Seven URI-addressable read-only resources, so a client can pull one object into context without a
tool round-trip. All seven are read end to end by the test suite and were verified against a live
BW 7.50 system.

| URI template | Returns |
|---|---|
| `bw://{system}/profile` | Release, ABAP schema, object-model variants, table availability |
| `bw://{system}/catalog` | Object counts per type — the system's shape at a glance |
| `bw://{system}/chain/{chain_id}` | One chain: processes, event-linked edges, nested sub-chains |
| `bw://{system}/provider/{name}` | One provider or InfoObject in full, **including every field** |
| `bw://{system}/transformation/{tran_id}` | One transformation: mappings, rule types, routines |
| `bw://{system}/query/{query_id}` | One BEx query: element tree, restrictions, CKFs, variables |
| `bw://{system}/calcview/{view_name}` | One calc view: base tables and consuming providers |

**Percent-encode the identifier.** A URI template expands one path segment and BW technical names
contain slashes — a namespaced object is `/IRM/IP_O02`. Unencoded, the segment splits and the read
resolves to nothing. On the reference system that is **484 objects**, including 20% of the
cube-table objects and 57 of 280 active chains, so it is the common case rather than an edge one:

```
bw://qa/provider/%2FIRM%2FIP_O02      ✓ resolves
bw://qa/provider//IRM/IP_O02          ✗ "Unknown resource"
```

Every URI the server itself emits is encoded, so a summarised response's citation is always
followable. That is the point of the citation: it is the only place a caller is told where the
omitted fields went.

Resources share the tools' failure envelope — a failed read returns a structured error with a
`code`, `category`, `remedy` and `retryable` flag rather than an opaque transport error, and an
absent table returns `unsupported_on_release` naming what is missing. They also share the per-call
budget, so a resource read cannot run unbounded either.

## Cost and scale

Every call runs inside a budget — 5,000 statements and 300 seconds by default, overridable with
`SAPBW_MAX_QUERIES_PER_CALL` / `SAPBW_MAX_SECONDS_PER_CALL`. Exhausting it returns a `BudgetResult`
naming what was spent and where it stopped, never a hang or a silent truncation.

A bound you only discover by hitting it is not something you can plan around, so
[`docs/performance.md`](docs/performance.md) publishes the cost of every tool and
`bw_performance_profile` answers the same question with no connection. Read `growth` first — it is
the answer to "does this get worse on a system our size":

| Growth | Tools | Meaning |
|---|--:|---|
| `constant` | 9 | Bounded by this build, not your landscape. Identical on any system. |
| `per_page` | 12 | One page of rows; cost set by `limit`, with `total_count` behind it. |
| `per_object` | 17 | Proportional to the one object named, not to how many exist. |
| `per_graph_node` | 7 | Follows the connected subgraph — a hub costs far more than a leaf at equal depth. |
| `per_system` | 13 | **Scans a whole class of objects.** Plan for these; each names the cap that stops it. |

Two facts are kept apart rather than blended into a score. `fixture_payload_bytes` is **measured**
against the synthetic fixtures, so CI can check it, but it is a floor: the fixtures hold about one
object per type, so it isolates a reply's fixed shape overhead from its per-row cost. `growth` is
**declared** from the code and cites the constant that bounds it, because a one-object fixture
cannot demonstrate what four thousand objects do. Statement counts report `not_measured` rather
than zero — budget charging lives in the connection layer, which the offline fixtures replace.

A successful call that spent more than 80% of its budget logs a warning naming the spend. That is
the early signal: it fires while the call still succeeds, which is when there is time to narrow it.

## Security model

- **Read-only, permanently.** The server issues `SELECT` only. There is no code path that can write
  to a BW system — enforced in the connection layer, not by convention. Connections configured with
  `read_only_user: true` are refused (fail closed) if the user holds any write grant.
- **Two provisioning postures, both documented.** A full technical read, or a least-privilege
  allow-list. [`docs/deployment-modes.md`](docs/deployment-modes.md) carries both as runnable
  `GRANT` scripts and states per group what withholding it costs; `bw_access_report` reports which
  is in force. A refused read is reported as a refused read, never as a BW release limitation.
- **No data retention off-box.** Metadata is cached locally per profile (git-ignored SQLite);
  runtime statistics are never cached beyond an hour. Nothing is transmitted to third parties.
- **One install can serve several customers without their data meeting.** See below.
- **Secrets by environment variable only.** No credentials in code, config, logs, or error
  messages. Connection strings are scrubbed from all error text.
- **Provenance on every fact.** Every returned record cites the metadata table and key it came from.
- **Generated content is labelled.** Synthesized descriptions are marked as generated and are never
  written back to BW.

### Isolating several customers on one install

A partner or consultancy points one install at several landscapes. Nothing distinguishes those
landscapes by alias, because **everybody calls their production system `prd`** — so set `tenant` on
every profile:

```yaml
systems:
  prd:
    tenant: acme
    environment: prod
```

The tenant and the alias together decide where a profile's data lands. Each stored file is named
`<tenant>-<system>-<digest>`: the prefix so an operator auditing the machine can tell whose data a
file holds, the digest so two identities can never share a file no matter how similar their aliases
look. That applies to the extract cache, the snapshot store and (by tenant directory) a generated
documentation tree.

`environment` is declared, never inferred. The server will not guess it from an alias or a host
name, because `prd_copy` would read as production and `production_2` would not — and being wrong
means someone reads production figures believing they are looking at QA.

Isolation is **verifiable rather than asserted**. `bw_cache_status` reports `storage_key`,
`tenant`, `environment` and `isolated_by_tenant` on every branch, including when a profile keeps
nothing at rest; `bw_list_systems` reports a `label` (`acme/prd (prod)`) that is unambiguous across
tenants. If two answers disagree, those fields say which landscape each came from.

For an install that must keep nothing at all, `cache_enabled: false` per profile writes no cache and
no snapshot store; `bw_compare_systems`, which captures both sides live, still works.

## No customer metadata ships with this package

This repository and the published package contain **only the server**. No customer BW metadata —
object names, ABAP routine source, query definitions, or chain schedules — is included. Extracted
content is customer intellectual property; it stays on your machine (git-ignored) and is never
committed or distributed. CI enforces this.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The entire test suite runs offline against synthetic
fixtures; no live BW system is required or permitted in CI.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
