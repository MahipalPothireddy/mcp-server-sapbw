---
inclusion: always
---

# SAP BW-on-HANA Metadata MCP Server — Kiro Build Prompt

**What this builds:** a reusable, open-source MCP server (`mcp-server-sapbw`) that exposes any BW-on-HANA system's metadata as model-callable tools. Point it at DEV, QA, PRD, or a different client's landscape via a connection profile — no code changes. The documentation knowledge base becomes one *output* of the server, not the product itself.

**How to use this file in Kiro**

1. Create project `mcp-server-sapbw`. Save this file as `.kiro/steering/mission.md` with front matter `inclusion: always`.
2. Connection profiles go in `.env` / `profiles.yaml` (git-ignored). Never in the repo, never in chat.
3. Run Kiro's spec workflow using the seed prompt in **Section 12**.
4. Execute **Section 13** build prompts one per session, in order.
5. Register the finished server in `.kiro/settings/mcp.json` (workspace) or `~/.kiro/settings/mcp.json` (user); workspace config wins on name conflicts.

---

## 1. Mission

Build an MCP server that turns SAP BW-on-HANA metadata into a queryable, system-agnostic interface. It must:

- Connect to **any** BW-on-HANA system through a named profile, auto-detecting release and object-model variant at connect time.
- Expose process chains, load lineage, transformation logic including every routine type, BEx query definitions with field-level lineage, HANA calculation view dependencies, and object descriptions — as MCP **tools**, **resources**, and **prompts**.
- Answer impact-analysis and incident-triage questions in one call, including dependencies that are invisible to BW's own where-used lists.
- Generate a full markdown knowledge base on demand as a tool output.

Every returned fact must be traceable to a metadata row actually read. A plausible-but-wrong dependency is worse than a documented gap.

## 2. Non-Negotiable Rules

1. **Read-only, permanently.** `SELECT` only. No DDL, DML, activation, chain triggering, or writing RFC/BAPI calls. The server must have no code path that can write to a BW system. Enforce it in the connection layer, not by convention.
2. **Never invent metadata.** If a table or column doesn't exist in a release, the capability resolver reports it missing and the affected tool returns a structured "unsupported on this release" result. No guessing table names from memory.
3. **Provenance on every fact.** Every record carries `source_table` and `source_key`. Format: `{"source_table": "RSTRAN", "source_key": {"TRANID": "0ABC123", "OBJVERS": "A"}}`.
4. **No customer metadata in the Git repository, ever.** The repo holds the *server*. Extracted BW content — ABAP routine source, query definitions, object names, chain schedules — is customer intellectual property and often contains business logic. Enforced via `.gitignore` and a CI check.
5. **Secrets by environment variable only.** `${VAR}` interpolation in MCP config. No credentials in code, tests, fixtures, logs, or error messages. Scrub connection strings from exception text before returning them.
6. **Active version only** unless explicitly comparing: `OBJVERS = 'A'` on all `RSD*`, `RSO*`, `RSTRAN*`, `RSZ*` tables. Skipping this silently multiplies row counts with modified and delivered versions.
7. **Generated content is labelled as generated.** See Section 8. A synthesized description must never be indistinguishable from one stored in BW.

## 3. Architecture

Four layers. Keep them strictly separated so a release quirk never leaks into tool logic.

```
MCP layer          tools / resources / prompts  (FastMCP decorators)
  ↓
Service layer      lineage graph, routine parser, latency analyzer, doc generator
  ↓
Repository layer   one module per domain: chains, providers, transformations, queries, hana
  ↓
Core layer         profile manager · connection pool · capability resolver · SQL dialect · cache
```

### Connection profiles

```yaml
# profiles.yaml — git-ignored
systems:
  prd:
    host: ${BW_PRD_HOST}
    port: 30015
    user: ${BW_PRD_USER}
    password: ${BW_PRD_PASSWORD}
    abap_schema: auto        # resolve at connect, do not hardcode SAPABAP1
    encrypt: true
    read_only_user: true     # assert and fail closed if the user has write grants
```

Every tool takes a `system: str` parameter naming a profile. The server is stateless across calls; connections are pooled per profile.

### Capability resolution

On first connect to a profile, run discovery once and cache it (TTL configurable, default 24h, `bw_refresh_capabilities` to bust):

```sql
SELECT TABNAME FROM <ABAP_SCHEMA>.DD02L WHERE TABNAME IN (...);
SELECT SCHEMA_NAME FROM SYS.TABLES WHERE TABLE_NAME = 'RSTRAN';
```

Produce a capability record: BW release, ABAP schema name, which of `{classic DSO, ADSO, CompositeProvider, MultiProvider, Open ODS View}` are present and populated, whether HANA repository (`_SYS_REPO`) or HDI containers are in use, and `RSPCPROCESSLOG` retention window. Every repository method checks capabilities before building SQL.

This is the mechanism that makes the server portable across BW 7.4, 7.5, and BW/4HANA. Build it first.

### Caching

Metadata changes slowly; queries against `RSTRAN`/`RSZ*` on large systems are expensive. Cache extracted structures in a local SQLite file per profile, keyed by object + extraction timestamp. Expose `bw_refresh_cache(system, scope)`. Runtime statistics are never cached beyond an hour.

## 4. MCP Surface

**Naming constraint (verified against Kiro):** tool names must match `^[a-zA-Z][a-zA-Z0-9_]*$` and stay under 64 characters *including the server prefix Kiro prepends*. No hyphens, no dots. Keep names ≤40 chars. Tools violating this are silently excluded with a validation error.

### Tools

| Tool | Purpose |
|---|---|
| `bw_list_systems` | Configured profiles and connection status |
| `bw_system_profile` | Release, capabilities, object counts, log retention window |
| `bw_search_objects` | Fuzzy search by technical name or description across all object types |
| `bw_describe_object` | Universal deep-dive: definition, fields, descriptions, lineage summary, consumers |
| `bw_list_chains` | Chains filtered by frequency, status, or name pattern |
| `bw_get_chain` | Structure, steps, nested chains resolved recursively |
| `bw_get_chain_runtimes` | Min/median/mean/p95/max, success rate, critical path over N days |
| `bw_get_schedule_matrix` | All chains × frequency × start time × p95 completion |
| `bw_get_lineage` | Upstream/downstream/both to depth N, as graph JSON |
| `bw_impact_analysis` | Complete blast radius incl. routine lookups, calc views, queries, reports |
| `bw_trace_to_source` | Any object back to originating DataSources, hop by hop |
| `bw_list_transformations` | Filter by source, target, or routine presence |
| `bw_get_transformation` | Header, field mappings, rule types per target field |
| `bw_get_routine_code` | Full ABAP source for start/end/expert/field routines |
| `bw_analyze_routine` | Parsed table dependencies, anti-patterns, complexity signals |
| `bw_list_queries` | BEx queries by provider, owner, or usage rank |
| `bw_get_query` | Complete definition: element tree, RKFs, CKFs, filters, variables, layout |
| `bw_get_query_lineage` | Query → provider → … → DataSource, field-level, chains annotated |
| `bw_get_query_usage` | Execution counts, runtimes, last-used from BW statistics |
| `bw_list_calc_views` | Calculation views, optionally filtered to BW-consuming ones |
| `bw_get_calc_view_lineage` | View → base tables → resolved BW objects |
| `bw_get_hana_crossings` | Every BW↔HANA boundary crossing, both directions |
| `bw_check_load_latency` | Latency-contract violations (Scenario 9.1) |
| `bw_check_schedule_risk` | Downstream report schedules vs. chain p95 completion (Scenario 9.7) |
| `bw_find_layer_violations` | CompositeProvider→DSO, CompositeProvider→InfoObject, deep DSO stacks |
| `bw_generate_docs` | Render the full markdown knowledge base to a local directory |
| `bw_refresh_capabilities` | Re-run release/capability discovery |
| `bw_refresh_cache` | Invalidate cached extracts by scope |

All list tools paginate (`limit`, `offset`) and return a `total_count`. Large results return a summary plus a resource URI rather than dumping thousands of rows into context.

### Resources

URI-addressable read-only context, so a client can pull an object into context without a tool round-trip:

```
bw://{system}/profile
bw://{system}/catalog
bw://{system}/chain/{chain_id}
bw://{system}/provider/{name}          # DSO, ADSO, cube, MultiProvider, CompositeProvider
bw://{system}/transformation/{tran_id}
bw://{system}/query/{query_id}
bw://{system}/calcview/{view_name}
```

### Prompts

Reusable templates that compose several tools into an analyst workflow:

| Prompt | Use |
|---|---|
| `analyze_impact` | "I want to change object X" → full downstream + upstream review |
| `troubleshoot_missing_data` | Report shows wrong/missing data → layer-by-layer diagnostic walk |
| `document_dataflow` | Produce a narrative document for one end-to-end flow |
| `review_scenario` | Run one of the eight risk scenarios in Section 9 |
| `onboard_analyst` | Orientation brief for a functional area (FI, SD, MM…) |
| `pre_change_checklist` | Everything to verify before transporting a change to object X |

## 5. Metadata Table Reference

Starting map only. **The capability resolver validates every one at runtime** — availability varies by release, and entries marked ⚠️ need discovery rather than assumption.

### Process chains and scheduling
| Table | Content |
|---|---|
| `RSPCCHAIN` | Chain edge list: `CHAIN_ID`, `TYPE`, `VARIANTE`, `NEXT`, `LNKPROCESSID` |
| `RSPCCHAINATTR` / `RSPCCHAINT` | Chain header / descriptions |
| `RSPCVARIANT` / `RSPCVARIANTT` | Process variant parameters and texts |
| `RSPCLOGCHAIN` | Run headers: `LOG_ID`, `DATUM`, `ZEIT`, `ANALYZED_STATUS` |
| `RSPCPROCESSLOG` | **Per-step runtimes**: `LOG_ID`, `TYPE`, `VARIANTE`, `INSTANCE`, start/end date-time, `STATE` |
| `RSPCLOGS` | Step messages |
| `TBTCO`, `TBTCP`, `TBTCS` | Job header/steps/schedule — **the only reliable periodicity source** |

### Data flow and transformations
| Table | Content |
|---|---|
| `RSTRAN` | Transformation header; source/target name+type; `STARTROUTINE`, `ENDROUTINE`, `EXPERTROUTINE` code IDs |
| `RSTRANFIELD` | Field-level mapping |
| `RSTRANRULE` | Rule definitions, rule type, routine reference |
| `RSTRANSTEP`, `RSTRANSTEPRULE` | Rule-to-step assignment |
| `RSAABAP` | **ABAP source lines for every routine** — join on code ID, order by `LINE_NO` |
| `RSBKDTP` | DTP header incl. `UPDMODE` (full/delta) |
| `RSBKREQUEST` | DTP request history: record counts, durations |
| `RSLDPIO`, `RSLDPSEL` | InfoPackages and selections |
| `RSDS`, `RSDSSEGFD` | BW DataSources and fields |
| `RSSTATMANPART` | Request status per provider — actual data currency |

### InfoProviders and their descriptions
| Table | Content |
|---|---|
| `RSDODSO` / `RSDODSOT` / `RSDODSOIOBJ` | Classic DSO header / **texts** / fields |
| `RSOADSO` / `RSOADSOT` / `RSOADSOIOBJ` | ⚠️ Advanced DSO — verify names per release |
| `RSDCUBE` / `RSDCUBET` / `RSDCUBEIOBJ` | InfoCubes and **texts**. MultiProviders live here too — filter on cube type |
| `RSDCUBEMULTI` | MultiProvider → part-provider assignment |
| `RSOHCPR*` | ⚠️ CompositeProvider — discover via `DD02L WHERE TABNAME LIKE 'RSOHCPR%'`; expect a header, a texts, and a part-provider/mapping table |
| `RSDIOBJ` / `RSDIOBJT` | InfoObjects and **texts** |
| `RSDCHA`, `RSDKYF`, `RSDBCHATR`, `RSDATRNAV` | Characteristics, key figures, attributes, navigation attributes |

### BEx queries
| Table | Content |
|---|---|
| `RSZCOMPDIR` | Query directory: `COMPUID`, `COMPID` (technical name), `INFOCUBE`, `DEF_VERS`, owner, last changed |
| `RSZCOMPIC` | Query → InfoProvider assignment |
| `RSZELTDIR` | Element directory: `ELTUID`, `DEFTP` (element type), `MAPNAME` |
| `RSZELTXREF` | **Element hierarchy**: `SELTUID` (parent) → `TELTUID` (child) — walk this recursively |
| `RSZELTTXT` | **Element texts — including the query's own description** |
| `RSZSELECT`, `RSZRANGE` | Restrictions and ranges per element |
| `RSZCALC` | Calculated key figure / formula definitions |
| `RSZGLOBV` | Variables: name, InfoObject, type, processing type |
| `RSZELTPROP` | Element properties and display settings |
| `RSRREPDIR` | Report directory / generation status |
| `RSDDSTAT_OLAP`, `RSDDSTATHEADER` | ⚠️ Query usage statistics — verify names; use to rank by real usage |

### HANA layer and dictionary
| Object | Content |
|---|---|
| `SYS.OBJECT_DEPENDENCIES` | **Calc view → base table lineage.** The highest-value object here |
| `_SYS_REPO.ACTIVE_OBJECT` | Calc view XML definition — parse for joins, calculated columns, input parameters |
| `SYS.VIEWS`, `SYS.VIEW_COLUMNS`, `SYS.COLUMNS` | Runtime catalog in `_SYS_BIC` |
| `SYS.M_CS_TABLES` | Table sizes/memory — flag large full-load targets |
| `DD02L`, `DD02T`, `DD03L`, `DD04T` | Table existence, texts, columns; the `/BIC/A*` naming that links BW objects to HANA tables |

## 6. BEx Query Lineage — Detailed Specification

This is a distinct subsystem, not a byproduct of general lineage. Build it deliberately.

**Query header and description.** From `RSZCOMPDIR` filtered to `OBJVERS = 'A'`. The description is *not* in `RSZCOMPDIR` — a query is itself an element, so join `RSZELTTXT` on `ELTUID = COMPUID` to get short and long text. Missing this join is the usual reason query documentation comes out with blank descriptions.

**Element tree.** Walk `RSZELTXREF` recursively from the query's root `ELTUID`, resolving each node through `RSZELTDIR`. Classify by `DEFTP` — decode the type codes empirically from the system rather than assuming a fixed list, and record the decode table you derived in the output. Expect to find: the query root, structures, selections / restricted key figures, calculated key figures and formulas, variables, and data-provider nodes.

**For each element, capture:**
- Description from `RSZELTTXT` (short and long).
- Restrictions from `RSZSELECT` + `RSZRANGE`: which InfoObject, operator, sign, low/high values, and whether the value is a variable reference.
- Formula/CKF definition from `RSZCALC`, with the operand elements resolved to readable names.
- Variables from `RSZGLOBV`: technical name, InfoObject, variable type, processing type (user entry, customer exit, authorization, replacement path, SAP exit), mandatory flag. **Customer-exit variables are a lineage dead end in metadata** — flag them for manual review with the exit name where derivable.
- Where the element sits: rows, columns, free characteristics, default filter, or global filter.

**Field-level lineage — the real prize.** For every InfoObject referenced anywhere in the query:

```
InfoObject in query
  → field in the InfoProvider (RSDCUBEIOBJ / RSDODSOIOBJ / RSOADSOIOBJ / CompositeProvider mapping)
  → for CompositeProviders: which part-provider supplies it, and via which calc view column
  → target field of the inbound transformation (RSTRANFIELD)
  → rule type (RSTRANRULE): direct, constant, formula, routine, master-data read, time conversion
  → source field(s), or — if the rule is a routine — the routine's parsed dependencies
  → repeat upward through each layer until a DataSource field is reached
```

Emit this as a per-InfoObject path list. Where a hop passes through a routine, mark it `derivation: routine` and attach the routine ID — that is where automated lineage necessarily becomes advisory rather than exact.

**Reuse and usage.** Detect queries reused as InfoProviders, and elements shared across queries (`RSZELTXREF` spanning multiple `COMPUID`s) — changing a shared restricted key figure hits every consumer. Rank all queries by real usage from BW statistics; a query with zero executions in 12 months is a decommissioning candidate and should be labelled as such in generated docs.

**Chain annotation.** Every query resolves to one or more providers; every provider resolves to the chains that load it. Attach chain ID, frequency, and p95 completion time to each query so "when is this report's data current?" is answerable in one call.

## 7. Descriptions for All Objects

Required for: BEx queries, DSOs/ADSOs, InfoCubes, MultiProviders, CompositeProviders, InfoObjects, transformations, and routines.

**Step 1 — read stored text.** Short and long text from the object's text table (Section 5), active version, logon language with English fallback. Transformation text table: discover via `DD02L WHERE TABNAME LIKE 'RSTRAN%'` rather than assuming.

**Step 2 — assess quality.** Flag a stored description as low-value when it is empty, identical to the technical name, a copy artefact (`Copy of …`, `ZZ_TEST`, `tmp`), shorter than four words, or in an unexpected language.

**Step 3 — generate where needed.** For low-value or missing descriptions, synthesize one from evidence the server already holds: source objects, key fields, semantic key, update mode, load frequency, routine logic summary, and known consumers. For routines specifically, extract the leading comment block from `RSAABAP` first — it is often the only documentation that exists — then summarize what the code actually does, which tables it reads, and what it changes about the record set.

**Step 4 — label provenance.** Every description object returns:

```json
{
  "description_short": "...",
  "description_long": "...",
  "origin": "stored | generated | stored_augmented",
  "quality_flag": "ok | missing | generic | copy_artifact",
  "evidence": ["RSDODSOT", "RSTRAN:0ABC123", "RSAABAP:0XYZ789"]
}
```

Generated descriptions render in documentation with a visible marker. They are never written back to BW.

## 8. Knowledge Base Generation

`bw_generate_docs` renders markdown to a local directory — outside the repo, git-ignored:

```
docs/
├── index.md                     # navigation, system profile, generation timestamp
├── 01-inventory/                # counts, catalogs per object type
├── 02-process-chains/           # per chain: structure, schedule, runtime stats, critical path
├── 03-lineage/                  # end-to-end flow diagrams (Mermaid) + graph JSON
├── 04-providers/                # per DSO/ADSO/cube/MultiProvider/CompositeProvider page
├── 05-transformations/          # per transformation: mappings + full routine source + parsed deps
├── 06-queries/                  # per BEx query: definition, element tree, field lineage, usage
├── 07-hana/                     # calc views, dependencies, BW↔HANA crossings
├── 08-scenarios/                # the eight risk analyses
└── 99-gaps-and-risks.md         # everything unverified or inaccessible
```

Every provider page lists: sources, targets, load frequency, update mode, inbound/outbound transformations, **routine-embedded lookups**, calc views reading it, and reports depending on it. Every page carries backlinks and source-table citations.

## 9. Risk Scenario Analyzers

Each is a service-layer analyzer plus an MCP prompt. These encode the specific pathologies in this landscape — treat them as analysis, not description.

**9.1 Full-update DSOs with lookups on once-daily ADM DSOs.** Cross-reference DTPs with `UPDMODE = 'F'` → their transformations → routine lookups parsed from `RSAABAP` → the load frequency of each looked-up DSO. Produce a **latency contract table**: for each lookup, does the target complete before the consuming DTP starts, on *every* scheduled run? Flag every case where a chain running more than once daily reads an ADM DSO refreshed once daily — the second run enriches new data against stale master data. Include extractor-side constraints (`0FI_AR_4` class: delta method, safety interval, re-init conditions) from `RSDS` / `ROOSOURCE` as named cases.

**9.2 EDW DSO → ADM DSO → calc view → CompositeProvider → BEx.** Trace every complete chain of this shape. For each: layer count, cumulative latency, and a troubleshooting runbook naming the exact table or view to inspect at each layer, in order.

**9.3 CompositeProviders feeding EDW DSOs.** Every transformation where the source is a CompositeProvider and the target is a DSO. Document which calculations happen in the calc view versus the BW transformation, and the activation-order dependency: a calc view change silently changes DSO content on next load with no BW where-used warning.

**9.4 InfoObjects loaded from CompositeProviders.** Every transformation targeting an InfoObject with a CompositeProvider source. Document the sequencing requirement — master data must load before any transaction load reading it — and identify which chains currently violate it.

**9.5 Orders / Billing / Shipments / Deliveries merged into one DSO.** Full 3–4 layer decomposition. Per contributing stream: source DataSource, key mapping, uniqueness keys, collision risk on the merged key, and the semantic differences the merge hides. Produce a per-layer field-lineage matrix: for every field in the final DSO, which stream(s) populate it, through which rule, at which layer.

**9.6 ECC extractor enhancements.** Per enhanced DataSource: appended fields (`ROOSFIELD` vs. standard extract structure in `DD03L`), enhancement technique, tables the enhancement reads, and the functional area owning them. Flag enhancements reading another team's data — those are the coordination-risk items. Note per-record `SELECT`s in exits as performance risks.

**9.7 Tableau / BOBJ schedules vs. chain completion.** Timeline table: each report/extract's scheduled start vs. p95 completion of the chain feeding its provider. Compute the **safety margin**; flag negative or sub-30-minute margins. Recommend event-based triggering over clock-based scheduling for the flagged set.

**9.8 Tableau dashboards directly on calc views.** Identify dashboards bypassing BW. Per dashboard, document the parallel path and whether its calc view is the *same* one the CompositeProvider uses. Shared view → one change breaks both paths at once. Separate views → the two paths can silently diverge in numbers. Both are findings; state which applies.

## 10. Repository and GitHub

### Structure

```
mcp-server-sapbw/
├── src/mcp_server_sapbw/
│   ├── server.py                 # FastMCP instance, tool/resource/prompt registration
│   ├── core/                     # profiles, connection, capabilities, dialect, cache
│   ├── repositories/             # chains, providers, transformations, queries, hana, texts
│   ├── services/                 # lineage, routine_parser, latency, descriptions, docgen
│   ├── models/                   # pydantic models incl. Provenance, Description
│   └── prompts/                  # prompt templates
├── tests/
│   ├── fixtures/                 # ANONYMIZED metadata only — synthetic names
│   └── test_*.py                 # run entirely offline against fixtures
├── docs/                         # server documentation — NOT generated BW output
├── .github/workflows/ci.yml
├── profiles.example.yaml
├── .env.example
├── pyproject.toml
├── README.md  LICENSE  SECURITY.md  CONTRIBUTING.md  CHANGELOG.md
└── .gitignore
```

### `.gitignore` — non-negotiable entries

```
.env
profiles.yaml
output/
extracts/
cache/
*.sqlite
.kiro/settings/mcp.json
```

Add a CI job that fails the build if any commit contains strings matching customer object-naming patterns (`/BIC/`, `/BI0/`, real chain or DSO prefixes) outside `tests/fixtures/`. Test fixtures use synthetic names only.

### Dependencies

Use **FastMCP** for the server layer. Note that FastMCP 3.x went GA in February 2026 and changed the authentication model from 2.x, so pin an explicit version in `pyproject.toml` and verify decorator and auth APIs against the docs for the pinned version rather than against older tutorials. Core deps: `fastmcp`, `hdbcli`, `pydantic`, `pyyaml`. Optional extra: `pyrfc` for RFC-based query resolution.

### CI

GitHub Actions: `ruff` lint, `mypy` type check, `pytest` against fixtures, secret scanning, and the customer-data check above. **No live BW system in CI** — every test runs offline.

### Publishing

```bash
git init && git branch -M main
git add . && git commit -m "feat: initial SAP BW-on-HANA metadata MCP server"
gh repo create mcp-server-sapbw --private --source=. --remote=origin
git push -u origin main
```

Start private. Before making it public, run a full history scan for leaked object names or credentials — `git filter-repo` if anything is found, since a squashed commit does not remove history. Tag releases with semantic versioning; publish to PyPI only after the repo is confirmed clean.

The README must contain: what the server does, the supported-release matrix, quickstart, the complete tool catalog with parameters, an MCP client config example, the security model (read-only, no data retention), and an explicit statement that no customer metadata ships with the package.

### Kiro registration

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

Auto-approve read-only, cheap tools only. Leave anything that runs long queries or generates files subject to prompting.

## 11. Acceptance Criteria

- Server connects to two different BW systems via profiles with zero code changes.
- Capability resolver correctly reports release and object-model variant; no tool ever queries a non-existent table.
- Every tool response carries provenance; no response contains credentials or host names.
- `bw_impact_analysis` on a known object returns the complete blast radius including at least one routine-embedded lookup invisible to BW's own where-used list.
- `bw_get_query_lineage` returns field-level paths from BEx query InfoObject to DataSource field, with routine hops marked as advisory.
- Every object type returns a description with `origin` and `quality_flag` populated.
- Full test suite runs offline against synthetic fixtures.
- Repository contains zero customer metadata; CI enforces it.
- `99-gaps-and-risks.md` is non-empty. An empty gap list means the analysis was not honest.

## 12. Seed Prompt for Kiro's Spec Workflow

> Create a spec named `mcp-server-sapbw`. Read `.kiro/steering/mission.md` as authoritative scope. Generate `requirements.md` in EARS format covering the four architectural layers, the full MCP tool/resource/prompt surface in Section 4, BEx query lineage per Section 6, the description subsystem per Section 7, and the eight risk analyzers in Section 9. Generate `design.md` covering the capability-resolution mechanism, the lineage graph model, the routine parser, caching, and the security model. Generate `tasks.md` breaking the build into individually executable tasks with explicit dependencies — core layer before repositories, repositories before services, services before MCP surface. Do not connect to any system yet. Flag every table name in Section 5 that requires runtime validation.

## 13. Build Prompts (one per session)

**B0 — Scaffold.** `Create the repository structure, pyproject.toml with pinned FastMCP, .gitignore per Section 10, README skeleton, and CI workflow. No BW logic yet. Verify the FastMCP decorator API against the docs for the version you pinned before writing any server code.`

**B1 — Core layer.** `Build profile manager, connection pool, read-only enforcement, ABAP schema resolution, capability resolver, and SQLite cache. Include the fail-closed check that rejects a connection whose user holds write grants. Unit tests against mocked connections.`

**B2 — Capability discovery, live.** `Connect to the first profile. Validate every table in Section 5. Produce a capability report: release, schema, object-model variants present, log retention window, row counts. List every table that does not exist and the correct alternative for this release. Do not build further tools until this is complete.`

**B3 — Repositories: chains.** `Implement the chains repository. Edge list, recursive meta-chain resolution, frequency classification from TBTCO/TBTCS periodicity — not from chain names — and runtime statistics with p95 and success rate over the retained window. Expose bw_list_chains, bw_get_chain, bw_get_chain_runtimes, bw_get_schedule_matrix.`

**B4 — Repositories: providers and texts.** `Implement providers and texts repositories covering DSOs, ADSOs, cubes, MultiProviders, CompositeProviders, InfoObjects, plus the description subsystem in Section 7 including quality assessment and generation with provenance labelling. Expose bw_describe_object, bw_search_objects.`

**B5 — Repositories: transformations and routine parser.** `Implement transformation extraction with field mappings, full ABAP retrieval from RSAABAP for all four routine types, and the parser resolving SELECTs against /BIC/ and /BI0/ tables back to BW objects. Detect anti-patterns: SELECT inside LOOP, missing FOR ALL ENTRIES guards, hardcoded values, record-set-altering DELETE logic. Expose the four transformation tools.`

**B6 — Lineage service.** `Build the directed lineage graph from transformations and DTPs, annotated with update mode and executing chain frequency. Merge in routine-derived edges from B5. Expose bw_get_lineage, bw_impact_analysis, bw_trace_to_source.`

**B7 — BEx queries.** `Implement the full query subsystem per Section 6: header with description via the RSZELTTXT/COMPUID join, recursive element tree from RSZELTXREF, restrictions, CKFs, variables with processing types, field-level lineage to DataSource, shared-element detection, and usage ranking. Expose the four query tools.`

**B8 — HANA layer.** `Extract calc view dependencies from SYS.OBJECT_DEPENDENCIES, resolve /BIC/ base tables to BW objects, parse view definitions, and build the bidirectional BW↔HANA crossing table. Expose the three HANA tools.`

**B9 — Risk analyzers.** `Implement the eight analyzers in Section 9 as services plus MCP prompts. Each returns findings with severity, affected objects, evidence, and recommended action.`

**B10 — Doc generation.** `Implement bw_generate_docs rendering the structure in Section 8, with Mermaid diagrams, backlinks, provenance citations, visible markers on generated descriptions, and the gaps register.`

**B11 — Harden and publish.** `Complete test coverage against anonymized fixtures, scrub secrets from all error paths, write the README tool catalog and security model, run the customer-data CI check across full history, then create the private GitHub repository and push. Report which acceptance criteria in Section 11 are unmet.`

---

## Known Limitations — read before starting

1. **Table names must be validated at runtime.** ADSO (`RSOADSO*`), CompositeProvider (`RSOHCPR*`), BW statistics (`RSDDSTAT*`), and transformation text tables vary across BW 7.4, 7.5, and BW/4HANA. The capability resolver exists specifically for this. It is the first thing to build and the first thing to run.
2. **Tableau and BOBJ metadata does not exist in BW.** Scenarios 9.7 and 9.8 need separate credentials — Tableau Metadata API or `workgroup` repository read access; BOE Query Builder or RESTful RaaS. Without them, the server produces the timeline template but cannot populate it. Design those as pluggable connectors so they can be added later without touching the BW core.
3. **Routine parsing is heuristic and produces a lower bound.** Dynamic SQL, function-module calls, and class methods inside routines will not be caught by SELECT parsing. Every routine dependency list must state this in its response payload, not just in documentation.
4. **Customer-exit variables are a metadata dead end.** BEx variables with customer-exit processing resolve in ABAP at runtime; the server can name the variable and flag it, but cannot determine its values. Mark these explicitly rather than presenting incomplete lineage as complete.
5. **`RSPCPROCESSLOG` retention limits runtime statistics.** Check the earliest available entry before promising a 90-day window; report the actual window used in every runtime response.
6. **Runtime statistics reflect contention, not intrinsic cost,** where chains overlap. Detect and report observed overlaps rather than presenting durations as fixed properties.
7. **First extraction on a large production system is expensive.** `RSAABAP` and `RSZ*` full extracts can run long. Build the cache before the tools that need it, run the first full extraction against QA if one exists, and always paginate.
