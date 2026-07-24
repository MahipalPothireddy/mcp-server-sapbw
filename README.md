# mcp-server-sapbw

A read-only, system-agnostic [MCP](https://modelcontextprotocol.io) server that exposes the
metadata of any **SAP BW-on-HANA** system as model-callable tools, resources, and prompts. Point
it at DEV, QA, PRD, or a different client's landscape through a named connection profile — no code
changes. It auto-detects BW release and object-model variant at connect time, answers
impact-analysis and incident-triage questions in one call (including dependencies invisible to BW's
own where-used lists), and can render a full markdown knowledge base on demand.

> **Status: functional (build prompts B0–B10 complete).** The metadata extraction, lineage,
> routine analysis, BEx query, HANA, risk-analyzer, and knowledge-base subsystems are implemented
> and exercised against a live BW 7.50 system: **29 tools and 6 prompts**. Still planned:
> URI-addressable **resources** and the pluggable ECC/Tableau/BOBJ connectors (deferred). See
> `PROGRESS.md` for the full build log and `.kiro/specs/mcp-server-sapbw/` for the spec.

## What it does

- **Process chains & scheduling** — structure, recursive meta-chains, frequency (from job
  periodicity, not names), and runtime statistics (p95, success rate, critical path).
- **Load lineage** — a directed graph across transformations, DTPs, providers, calc views, and
  queries, including advisory edges parsed from ABAP routine source.
- **Transformations & routines** — field mappings, rule types, and full ABAP source with parsed
  table dependencies and anti-pattern detection.
- **BEx queries** — complete definitions with field-level lineage down to the DataSource field.
- **HANA calc views** — dependencies and every BW↔HANA boundary crossing.
- **Descriptions** — for every object, with explicit provenance (stored vs. generated).
- **Risk analyzers** — eight landscape-specific analyses (latency contracts, schedule risk,
  layer violations, and more).
- **Knowledge base** — a full markdown documentation set rendered on demand.

## Supported releases

| Release | Status |
|---|---|
| BW 7.5 (on HANA) | **Validated live** — the reference system for the build (SAP_BW 7.50) |
| BW 7.4 (on HANA) | Expected to work; validated at runtime by the capability resolver, not yet tested live |
| BW/4HANA | Expected to work; runtime-validated, not yet tested live |

Portability is achieved by a runtime **capability resolver** that discovers which tables and
object-model variants actually exist before any tool builds SQL — no release is assumed. On a
release where a table is absent, the affected tool returns a structured "unsupported on this
release" result naming what is missing, rather than guessing.

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
| `bw_list_chains` | `system`, `name_pattern?`, `active_only=true`, `limit`, `offset` | Chains filtered by name/active status |
| `bw_get_chain` | `system`, `chain_id` | Structure, processes, event-linked edges, nested sub-chains (recursive) |
| `bw_get_chain_runtimes` | `system`, `chain_id`, `days=90` | min/median/mean/p95/max, success rate, bottleneck steps over the measured window |
| `bw_get_schedule_matrix` | `system`, `active_only=true`, `window_days=30`, `limit`, `offset` | Chain × observed frequency × typical start × p95 completion |

### Objects & search

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_search_objects` | `system`, `query`, `limit`, `offset` | Fuzzy search by technical name or description across object types |
| `bw_describe_object` | `system`, `name` | Universal deep-dive: type, fields, key, parts, description (stored vs. generated) |

### Transformations & routines

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_transformations` | `system`, `source_name?`, `target_name?`, `with_routines_only=false`, `limit`, `offset` | Transformations filtered by endpoint or routine presence |
| `bw_get_transformation` | `system`, `tran_id` | Header, field-level rule mappings, routine references |
| `bw_get_routine_code` | `system`, `tran_id` | Full ABAP source for start/end/expert/field routines |
| `bw_analyze_routine` | `system`, `tran_id` | Parsed table dependencies + anti-patterns (heuristic lower bound) |

### Lineage

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_get_lineage` | `system`, `name`, `direction="both"`, `depth=6` | Directed data-flow graph, with advisory routine edges |
| `bw_impact_analysis` | `system`, `name`, `depth=3` | Full downstream blast radius, **including routine-embedded consumers** invisible to BW where-used |
| `bw_trace_to_source` | `system`, `name`, `depth=8` | Trace upstream, hop by hop, to the DataSource boundary |

### BEx queries

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_queries` | `system`, `provider?`, `owner?`, `limit`, `offset` | Executable BEx queries (not reusable components) |
| `bw_get_query` | `system`, `query` | Definition: element tree, restrictions, variables with processing types |
| `bw_get_query_lineage` | `system`, `query` | Field-level lineage per InfoObject toward the DataSource; customer-exit dead ends flagged |
| `bw_get_query_usage` | `system`, `query`, `stale_days=365` | Last-used and decommission-candidate flag |

### HANA layer

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_list_calc_views` | `system`, `bw_consuming_only=false`, `limit`, `offset` | Calc views (`_SYS_BIC`), optionally only those reading BW tables |
| `bw_get_calc_view_lineage` | `system`, `view_name` | A calc view's direct base tables, resolved to BW objects (advisory) |
| `bw_get_hana_crossings` | `system`, `calc_view?`, `limit`, `offset` | Every BW↔HANA boundary crossing, both directions |

### Risk analyzers (mission Section 9)

| Tool | Parameters | Purpose |
|---|---|---|
| `bw_check_load_latency` | `system`, `limit=25` | Scenario 9.1: full-update loads whose routines look up other objects (stale-data risk) |
| `bw_check_schedule_risk` | `system`, `limit` | Scenario 9.7: report schedules vs. feeding-chain p95 (needs a BI connector) |
| `bw_find_layer_violations` | `system`, `max_dso_depth=3`, `limit` | CompositeProvider→DSO, CompositeProvider→InfoObject, deep DSO stacks |
| `bw_review_scenario` | `system`, `scenario`, `limit=50` | Run any scenario by id (`9.1`–`9.8` or `layer_violations`) |

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
