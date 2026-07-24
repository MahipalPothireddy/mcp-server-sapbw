# mcp-server-sapbw

A read-only, system-agnostic [MCP](https://modelcontextprotocol.io) server that exposes the
metadata of any **SAP BW-on-HANA** system as model-callable tools, resources, and prompts. Point
it at DEV, QA, PRD, or a different client's landscape through a named connection profile — no code
changes. It auto-detects BW release and object-model variant at connect time, answers
impact-analysis and incident-triage questions in one call (including dependencies invisible to BW's
own where-used lists), and can render a full markdown knowledge base on demand.

> **Status: pre-alpha scaffold (build prompt B0).** No BW logic is implemented yet. The build
> proceeds in ordered stages (B0–B11); see `.kiro/specs/mcp-server-sapbw/tasks.md` and `PROGRESS.md`.

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
| BW 7.4 | Planned (validated at runtime by the capability resolver) |
| BW 7.5 | Planned |
| BW/4HANA | Planned |

Portability is achieved by a runtime **capability resolver** that discovers which tables and
object-model variants actually exist before any tool builds SQL. No release is assumed.

## Quickstart

> Placeholder — completed in build prompt B11 once the server runs.

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

> Placeholder — the full catalog with parameters is generated in build prompt B11. See
> `.kiro/steering/mission.md` Section 4 and `.kiro/specs/mcp-server-sapbw/requirements.md` for the
> authoritative list of 28 tools, 7 resources, and 6 prompts.

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
