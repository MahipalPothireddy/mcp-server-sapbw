# mcp-server-sapbw

A read-only, system-agnostic [MCP](https://modelcontextprotocol.io) server that exposes the
metadata of any **SAP BW-on-HANA** system as model-callable tools, resources, and prompts. Point
it at DEV, QA, PRD, or a different client's landscape through a named connection profile — no code
changes. It auto-detects BW release and object-model variant at connect time, answers
impact-analysis and incident-triage questions in one call (including dependencies invisible to BW's
own where-used lists), and can render a full markdown knowledge base on demand.

> **Status: functional.** The metadata extraction, lineage, diagram rendering, routine analysis,
> BEx query, HANA, provider-health, security, risk-analyzer, and knowledge-base subsystems are
> implemented:
> **45 tools, 7 resources and 6 prompts**, most of them exercised against a live BW 7.50 system.
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
- **Risk analyzers** — the eight landscape-specific analyses (latency contracts, schedule risk,
  layer violations including write-back loops, and more) plus decommission-candidate detection.
- **Knowledge base** — a full markdown documentation set rendered on demand.

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
| `bw_refresh_cache` | `system`, `scope="all"` | Invalidate cached extracts by scope |

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
| `bw_find_layer_violations` | `system`, `max_dso_depth=3`, `limit` | CompositeProvider→DSO, CompositeProvider→InfoObject, deep DSO stacks, **write-back loops** |
| `bw_review_scenario` | `system`, `scenario`, `limit=50` | Run any analysis by id (`9.1`–`9.8`, `layer_violations`, `unused_providers`) |

Write-back loops are the severe ones. A transformation whose source and target are the same object
makes its own load non-repeatable: the output depends on what the target already held, so a failed
request cannot simply be re-run. Two objects that each feed the other have no correct load order at
all, which is why scheduling cannot fix it. Longer cycles (A→B→C→A) are not searched, and the report
says so rather than implying the check was exhaustive.

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

URI-addressable read-only resources (`bw://{system}/profile`, `/catalog`, `/chain/{id}`, …) are
specified (mission Section 4) but **not yet implemented** — the equivalent data is available today
through the tools. Tracked as an open item in `PROGRESS.md`.

## Security model

- **Read-only, permanently.** The server issues `SELECT` only. There is no code path that can write
  to a BW system — enforced in the connection layer, not by convention. Connections configured with
  `read_only_user: true` are refused (fail closed) if the user holds any write grant.
- **No data retention off-box.** Metadata is cached locally per profile (git-ignored SQLite);
  runtime statistics are never cached beyond an hour. Nothing is transmitted to third parties.
- **Secrets by environment variable only.** No credentials in code, config, logs, or error
  messages. Connection strings are scrubbed from all error text.
- **Provenance on every fact.** Every returned record cites the metadata table and key it came from.
- **Generated content is labelled.** Synthesized descriptions are marked as generated and are never
  written back to BW.

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
