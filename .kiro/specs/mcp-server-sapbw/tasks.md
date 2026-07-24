# Implementation Plan — mcp-server-sapbw

This plan breaks the build into individually executable, test-driven tasks. It follows the
dependency rule **core layer → repositories → services → MCP surface**, with one deliberate
exception: **B3 is a full vertical slice** (core → chains repository → `bw_list_chains` callable
from a real MCP client) so the end-to-end pattern is validated once before the remaining
repositories are built on it. Each task lists its upstream dependencies, the requirements it
satisfies (see `requirements.md`), and the mission build prompt it derives from (mission
Section 13, B0–B11).

**Connection policy.** Spec authoring performs no connection. Tasks marked **[LIVE]** are the only
ones that connect to a BW system; they run in their own sessions per the build-prompt cadence and
require explicit owner approval (system + profile named). Every other task runs fully offline
against mocked connections and synthetic fixtures.

**B2 is a hard gate.** No repository code and no tool/MCP-server code — including the FastMCP
bootstrap — is written before B2 (live capability discovery) completes and the owner has reviewed
the capability report. B2's report is produced as a direct artifact (local file/stdout), not via an
MCP tool, precisely so the gate is not crossed to produce it.

**Layering vs. build prompts.** Per owner correction: **B3 stays a vertical slice** (repository +
bootstrap + tools together). Build prompts **B4–B9 use the approved repository/tool split** — each
repository/service is built in the repository/service phases, and its MCP tools are registered in
the MCP-surface phase, which depends on the layers beneath. Cross-references note the originating
build prompt.

---

## Phase 0 — Scaffolding (B0)

- [ ] 1. Repository scaffold and toolchain
  - Create the `src/mcp_server_sapbw/` package tree (`core/`, `repositories/`, `services/`, `connectors/`, `models/`, `prompts/`, `server.py` placeholder) and `tests/` with `fixtures/`.
  - Write `pyproject.toml` with a **pinned, current-stable FastMCP 3.x** version plus `hdbcli`, `pydantic`, `pyyaml`; declare `pyrfc` as an optional extra. Configure `ruff`, `mypy`, `pytest`.
  - Ensure `.gitignore` carries the non-negotiable entries: `.env`, `profiles.yaml`, `output/`, `extracts/`, `cache/`, `*.sqlite`, `.kiro/settings/mcp.json` (already present; augment with Python caches).
  - Add `profiles.example.yaml` (synthetic values only), `.env.example`, README skeleton, `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`.
  - Add `.github/workflows/ci.yml`: `ruff`, `mypy`, `pytest` (offline), secret scan, and the customer-object pattern check (`/BIC/`, `/BI0/`, real prefixes) excluding `tests/fixtures/`.
  - **Verify the pinned FastMCP decorator/auth API against that version's own docs before writing any server code** (3.x changed the auth model from 2.x; confirm local stdio transport is unaffected).
  - _Depends on: none_
  - _Requirements: R29.1, R29.2_
  - _Build prompt: B0_

---

## Phase 1 — Core layer (B1, offline, mocked connections; no repositories, no tools)

- [ ] 2. Shared data models
  - Implement pydantic models: `Provenance`, `UnsupportedResult`, `CapabilityRecord` + `TableStatus`, `Description`, `LineageNode`, `LineageEdge`, `RoutineAnalysis` (+ `TableDependency`, `AntiPattern`, `ComplexitySignals`), and `Finding` + `Severity`.
  - Unit tests for validation and serialization (assert every fact-bearing model requires a `provenance` field).
  - _Depends on: 1_
  - _Requirements: R5, R7, R11, R12, R13_
  - _Build prompt: B1_

- [ ] 3. Profile manager
  - Implement `ProfileManager`: load `profiles.yaml` from `BW_PROFILES_PATH`, parse with pyyaml, interpolate `${VAR}` from the environment.
  - Reject inline literal secrets for secret fields; preserve `abap_schema: auto` as a sentinel.
  - Unknown-profile lookup returns a structured error listing configured profile names (no secrets).
  - Unit tests with fixture YAML + monkeypatched env vars.
  - _Depends on: 2_
  - _Requirements: R1.1, R1.2, R1.5, R1.6_
  - _Build prompt: B1_

- [ ] 4. Read-only connection pool and security enforcement
  - Implement `ReadOnlyConnectionPool` over `hdbcli`, one pool per profile, with a single `execute_select(sql, params)` entry point that asserts the statement is `SELECT`/`WITH … SELECT` and rejects everything else.
  - Implement the **fail-closed grant check**: when `read_only_user: true`, refuse the connection (`ReadOnlyViolation`) if the user holds any write/DDL/execute grant.
  - Implement `scrub()` for host/port/user/password/connection strings and install it on the logging formatter and all exceptions leaving this layer.
  - Set `encrypt` per profile and read-committed/read-only session flags where supported.
  - Unit tests against a mocked connection: SELECT-only guard rejects DML/DDL; grant check fails closed; scrubbing removes secrets from error text.
  - _Depends on: 3_
  - _Requirements: R2.1, R2.2, R2.3, R2.4, R1.4_
  - _Build prompt: B1_

- [ ] 5. SQL dialect builder
  - Implement parameterized SELECT construction (no value string-interpolation), `OBJVERS = 'A'` auto-injection for `RSD*`/`RSO*`/`RSTRAN*`/`RSZ*` (relaxable via `compare_versions`), schema qualification from the capability record, and `paginate()` + `count()` helpers.
  - Unit tests: active-version injection, version-compare relaxation, injection-safety, pagination/count SQL shape.
  - _Depends on: 2_
  - _Requirements: R6.1, R6.2, R2.1, R26.3_
  - _Build prompt: B1_

- [ ] 6. Capability resolver (offline logic)
  - Implement `CapabilityResolver.resolve(profile)`: resolve ABAP schema (never hardcode `SAPABAP1`), EXISTENCE-tier batch probe, DISCOVER-tier pattern discovery (`RSOADSO%`, `RSOHCPR%`, `RSDDSTAT%`, `RSTRAN%` text table, calc-view definition location, `ROOSOURCE`/`ROOSFIELD`), role classification via `DD03L` column signatures, population detection, and `RSPCPROCESSLOG` retention measurement.
  - Implement `Repository.require(logical_name)` gating returning `UnsupportedResult` when absent.
  - Unit tests against **three simulated fixture releases** (7.4 / 7.5 / BW4HANA): ADSO/CP present vs. absent, statistics-table name variance, HDI vs `_SYS_REPO`. Assert no SQL is built for absent tables.
  - _Depends on: 4, 5_
  - _Requirements: R3.1, R3.2, R3.3, R3.4, R7.1, R7.2, Appendix A_
  - _Build prompt: B1_

- [ ] 7. SQLite metadata cache
  - Implement `SqliteCache` (one file per profile under git-ignored `cache/`): key `(system, object_type, object_id, extraction_kind)` + `extracted_at` + capability fingerprint.
  - Implement the two-tier TTL: long TTL for structural metadata; **hard 1-hour cap** for runtime statistics. Implement `refresh(scope)` invalidation.
  - Unit tests: hit/miss, TTL expiry for both tiers, capability-refresh invalidation, scope-based purge.
  - _Depends on: 6_
  - _Requirements: R4.1, R4.2, R4.3, R4.4, R4.5_
  - _Build prompt: B1_

---

## Phase 2 — Live capability validation (B2) — HARD GATE, [LIVE]

- [ ] 8. [LIVE] First-connect capability discovery and report
  - **Requires explicit owner approval and a named system + profile before connecting.** Target: whichever non-production system (`dev`/`qa`) is provisioned with a read-only HANA user first.
  - Connect to the approved profile and run `CapabilityResolver` for real.
  - Produce a capability report **as a direct artifact** (local file / stdout, git-ignored): BW release, resolved ABAP schema, object-model variants present/populated, HANA repo style, `RSPCPROCESSLOG` retention window, and row estimates.
  - Validate every table in `requirements.md` Appendix A; list every table that does not exist for this release and the correct alternative.
  - **Gate:** do not start Phase 3 until this completes and the owner has reviewed the report. Fold any release-specific findings back into the resolver. Prefer QA for the first full extraction.
  - Note: runtime stats (`RSPCPROCESSLOG`) and query usage (`RSDDSTAT*`) are only meaningful in PRD; non-production values for those are not representative.
  - _Depends on: 6, 7_
  - _Requirements: R3.5, R3.6, R7.1, Appendix A_
  - _Build prompt: B2_

---

## Phase 3 — First vertical slice: chains (B3) — core → chains repo → callable `bw_list_chains`

> Owner correction: B3 is delivered as a complete working slice, not a repo/tool split. It proves
> the whole pattern — capability-gated SQL, provenance stamping, caching, FastMCP registration, and
> a tool reachable from a real MCP client — before six more repositories reuse it.

- [ ] 9. Repository base
  - Implement `Repository` base: capability gating via `require()`, `select()` through the read-only pool + dialect, and `stamp()` provenance attachment. Integrate the cache read/write path.
  - Unit tests: gating returns `UnsupportedResult`; every returned row carries provenance.
  - _Depends on: 6, 7 (Phase 2 gate cleared)_
  - _Requirements: R5.1, R5.2, R3.4_
  - _Build prompt: B3_

- [ ] 10. Chains repository
  - Read the `RSPCCHAIN` edge list; resolve nested meta-chains recursively.
  - Classify frequency strictly from `TBTCO`/`TBTCP`/`TBTCS` periodicity (never from names).
  - Compute runtime statistics (min/median/mean/p95/max, success rate, critical path) from `RSPCLOGCHAIN`/`RSPCPROCESSLOG` over the measured retention window; detect and report chain overlaps; report the actual window when it is shorter than requested.
  - Unit tests against synthetic chain/log fixtures incl. a meta-chain and an overlap case.
  - _Depends on: 9_
  - _Requirements: R8.1–R8.7_
  - _Build prompt: B3_

- [ ] 11. FastMCP server bootstrap, conventions, and core/system tools
  - Instantiate FastMCP (pinned-version API, **stdio transport**). Implement shared tool conventions: `system` param resolution, lazy capability load, pagination (`limit`/`offset` + `total_count`), oversized-result summary + `bw://` URI, and a **registration-time assertion** that every tool name matches `^[a-zA-Z][a-zA-Z0-9_]*$`, has no hyphens/dots, and is ≤40 chars.
  - Register the core/system tools that need no repository: `bw_list_systems`, `bw_system_profile` (re-exposes the B2 capability report), `bw_refresh_capabilities`, `bw_refresh_cache`.
  - Contract tests: naming assertion rejects a bad name; a response never contains host/credentials.
  - _Depends on: 4, 6, 7 (Phase 2 gate cleared)_
  - _Requirements: R26.1, R26.2, R26.3, R26.4, R26.5, R1.3, R2.5, R3.5, R4.4_
  - _Build prompt: B3_

- [ ] 12. Chain tools + vertical-slice verification
  - Register `bw_list_chains`, `bw_get_chain`, `bw_get_chain_runtimes`, `bw_get_schedule_matrix` on the bootstrap server.
  - **Verify the slice end to end:** `bw_list_chains` is callable from a real MCP client over stdio and returns provenance-stamped results (against a fixture-backed connection in tests; against the approved non-prod system in a [LIVE] smoke check only with owner approval).
  - Unit/contract tests for each chain tool.
  - _Depends on: 10, 11_
  - _Requirements: R8.1, R8.2, R8.4, R8.7, R26.3_
  - _Build prompt: B3_

---

## Phase 4 — Repository layer, remaining domains (B4, B5, B7, B8)

- [ ] 13. Texts repository (shared)
  - Read object text tables with logon-language + English fallback, active version; discover the transformation text table via `DD02L WHERE TABNAME LIKE 'RSTRAN%'`.
  - Unit tests: language fallback, discovered text-table name.
  - _Depends on: 9_
  - _Requirements: R13.1_
  - _Build prompt: B4_

- [ ] 14. Providers repository
  - Universal provider reader for classic DSO (`RSDODSO*`), ADSO (`RSOADSO*`, capability-gated), InfoCube + MultiProvider (`RSDCUBE*`/`RSDCUBEMULTI`, distinguished by cube type), CompositeProvider (discovered `RSOHCPR*`, capability-gated), and InfoObject (`RSDIOBJ*` with `RSDCHA`/`RSDKYF`/`RSDBCHATR`/`RSDATRNAV`). Return a common provider model.
  - Unit tests: each type; ADSO/CP absent → `UnsupportedResult`.
  - _Depends on: 9, 13_
  - _Requirements: R9.1, R9.2, R9.3_
  - _Build prompt: B4_

- [ ] 15. Transformations repository
  - Read `RSTRAN` header, `RSTRANFIELD` mappings, and rule types (`RSTRANRULE`/`RSTRANSTEP`/`RSTRANSTEPRULE`); assemble full ABAP source from `RSAABAP` joined on code ID ordered by `LINE_NO` for all four routine types.
  - Read `RSBKDTP` (`UPDMODE`), `RSBKREQUEST`, `RSLDPIO`/`RSLDPSEL`, `RSDS`/`RSDSSEGFD`, `RSSTATMANPART`.
  - Unit tests against synthetic transformation + routine-source fixtures.
  - _Depends on: 9_
  - _Requirements: R10.1, R10.2, R10.3, R10.5_
  - _Build prompt: B5_

- [ ] 16. Queries repository
  - Read `RSZCOMPDIR` (active), the `RSZELTTXT`-on-`COMPUID` description join, `RSZCOMPIC`, `RSZELTDIR`, recursive `RSZELTXREF`, `RSZSELECT`/`RSZRANGE`, `RSZCALC`, `RSZGLOBV`, `RSZELTPROP`, `RSRREPDIR`, and the discovered `RSDDSTAT*` usage tables.
  - Derive the `DEFTP` decode table empirically and return it with results.
  - Unit tests: description join, recursive element walk, empirical `DEFTP` decode.
  - _Depends on: 9, 13_
  - _Requirements: R14.2, R14.3, R14.8, R7.2_
  - _Build prompt: B7_

- [ ] 17. HANA repository
  - Read `SYS.OBJECT_DEPENDENCIES`; resolve `/BIC/` base tables to BW objects via `DD02L`; parse calc-view definitions from the capability-resolved location (`_SYS_REPO.ACTIVE_OBJECT` or HDI equivalent); read `SYS.VIEWS`/`SYS.VIEW_COLUMNS`/`SYS.COLUMNS` and `SYS.M_CS_TABLES`.
  - Unit tests against synthetic HANA-catalog fixtures for both repo styles.
  - _Depends on: 9_
  - _Requirements: R15.1, R15.2, R15.4_
  - _Build prompt: B8_

---

## Phase 5 — Service layer (B4, B5, B6, B7, B9)

- [ ] 18. Routine parser service
  - Implement the heuristic ABAP pipeline: normalize + capture leading comment; extract table reads (`SELECT`, `SELECT SINGLE`, `INTO TABLE`, `FOR ALL ENTRIES`); resolve `/BIC/`,`/BI0/` to BW objects; detect anti-patterns (`SELECT` in `LOOP`, missing `FOR ALL ENTRIES` guard, hardcoded values, record-set-altering `DELETE`); compute complexity; set `completeness = "lower_bound"` with caveats for dynamic SQL / FM / method calls.
  - Return `RoutineAnalysis`. Unit tests: one fixture per anti-pattern, `/BIC/` resolution, lower-bound caveat presence.
  - _Depends on: 15_
  - _Requirements: R11.1, R11.2, R11.3, R11.4, R11.5_
  - _Build prompt: B5_

- [ ] 19. Lineage graph service
  - Build the directed multigraph: declared edges from `RSTRAN`/`RSBKDTP`(with `UPDMODE`)/`RSDCUBEMULTI`/CompositeProvider parts/`SYS.OBJECT_DEPENDENCIES`/`RSZCOMPIC`; advisory `routine_lookup` edges merged from the routine parser (`derivation=routine`, `confidence=advisory`, routine ID in note). Annotate edges with executing chain ID + frequency.
  - Implement traversals: `get_lineage(direction, depth)` with cycle handling, `impact_analysis` (separates exact vs advisory, surfaces routine-embedded lookups), `trace_to_source` (terminates at DataSources).
  - Unit tests: declared + advisory merge, cycle handling, impact analysis surfacing a routine-only lookup.
  - _Depends on: 14, 15, 17, 18_
  - _Requirements: R12.1, R12.2, R12.3, R12.4, R12.5_
  - _Build prompt: B6_

- [ ] 20. Description service
  - Implement read → assess quality (empty / equals technical name / copy artifact / < 4 words / wrong language) → generate from held evidence (routines use leading `RSAABAP` comment first) → label (`origin`, `quality_flag`, `evidence`). Never write back to BW.
  - Unit tests: each `quality_flag`; each `origin`; generated content carries the marker.
  - _Depends on: 13, 14, 15, 18_
  - _Requirements: R13.2, R13.3, R13.4, R13.5, R13.6, R2.6_
  - _Build prompt: B4_

- [ ] 21. BEx query lineage service (Section 6)
  - Compose the queries repository + lineage + routine parser into per-InfoObject field-level paths: InfoObject → provider field → (CP) part-provider + calc-view column → inbound transformation target → rule type → source field(s)/routine deps → up to a DataSource field; mark routine hops advisory.
  - Implement shared-element detection (`RSZELTXREF` spanning multiple `COMPUID`s), customer-exit variable dead-end flagging, usage ranking (zero-in-12-months → decommission candidate), and chain annotation (id/frequency/p95) per query.
  - Unit tests: field path to DataSource, routine hop marked advisory, customer-exit flagged, shared element detected.
  - _Depends on: 16, 19, 18, 10_
  - _Requirements: R14.1, R14.4, R14.5, R14.6, R14.7, R14.9_
  - _Build prompt: B7_

- [ ] 22. External-system connector interface
  - Define the pluggable connector interface (`connectors/base.py`) separate from the BW core, with a `NullConnector` returning "connector not configured", covering: **ECC** (`connectors/ecc.py`, extractor-enhancement source / `ROOSOURCE`/`ROOSFIELD`, for 9.6); **Tableau** (Metadata API / `workgroup`, for 9.7/9.8); **BOBJ** (Query Builder / RESTful RaaS, for 9.7).
  - The BW HANA connection is never assumed to reach ECC/Tableau/BOBJ — those are distinct systems with distinct credentials.
  - Unit tests: null connector present; analyzers detect "no connector configured" for each of 9.6/9.7/9.8.
  - _Depends on: 2_
  - _Requirements: R30.1, R30.2, R30.3_
  - _Build prompt: B9_

- [ ] 23. Latency service
  - Implement shared timing math: consumer start (chain `TBTC*` schedule or connector report schedule) vs. feeding-chain p95 completion → safety margin. Support the 9.1 routine-lookup → looked-up-DSO frequency walk and the 9.7 margin flagging.
  - Unit tests: margin computation, sub-30-minute and negative flags.
  - _Depends on: 10, 19_
  - _Requirements: R16.2, R22.1, R22.2_
  - _Build prompt: B9_

- [ ] 24. Risk analyzers (the eight scenarios + layer violations)
  - Implement each analyzer returning `Finding` (severity, affected_objects, evidence, recommendation; `unpopulated_reason` when a connector is absent):
    - 24.1 — 9.1 full-update DSO lookups on once-daily ADM DSOs; latency-contract table; extractor constraints from `RSDS`/`ROOSOURCE`. → `bw_check_load_latency` data.
    - 24.2 — 9.2 deep EDW→ADM→calc view→CP→BEx chains; layer count, cumulative latency, per-layer runbook.
    - 24.3 — 9.3 CompositeProvider→DSO transformations; calc-view vs BW calc split; activation-order warning.
    - 24.4 — 9.4 InfoObjects loaded from CompositeProviders; sequencing requirement; violating chains.
    - 24.5 — 9.5 merged Orders/Billing/Shipments/Deliveries DSO; per-stream keys/collision; per-layer field-lineage matrix.
    - 24.6 — 9.6 ECC extractor enhancements (`ROOSFIELD` vs `DD03L`); cross-team + per-record `SELECT` flags. **Connector-dependent** (needs the ECC connector from task 22; the enhancement source is in ECC, unreachable from BW HANA). Without it, emit the template + `unpopulated_reason`, populating only the BW-side DataSource replica.
    - 24.7 — 9.7 report schedule vs chain p95 (uses latency service + connector). → `bw_check_schedule_risk` data.
    - 24.8 — 9.8 Tableau dashboards directly on calc views; shared vs separate view finding.
    - 24.9 — layer-violation finder (CP→DSO, CP→InfoObject, deep DSO stacks). → `bw_find_layer_violations` data.
  - Unit tests per analyzer against synthetic fixtures, incl. connector-absent path for 24.7/24.8.
  - _Depends on: 19, 20, 21, 22, 23_
  - _Requirements: R16, R17, R18, R19, R20, R21, R22, R23, R24_
  - _Build prompt: B9_

---

## Phase 6 — MCP surface for B4–B9 (approved repository/tool split)

- [ ] 25. Search and describe tools
  - Register `bw_search_objects`, `bw_describe_object`.
  - _Depends on: 11, 14, 20_
  - _Requirements: R9.4, R9.5, R26.1 (originally bundled in B4)_
  - _Build prompt: B4_

- [ ] 26. Transformation and routine tools
  - Register `bw_list_transformations`, `bw_get_transformation`, `bw_get_routine_code`, `bw_analyze_routine`.
  - _Depends on: 11, 15, 18_
  - _Requirements: R10.1, R10.2, R10.3, R10.4 (originally bundled in B5)_
  - _Build prompt: B5_

- [ ] 27. Lineage tools
  - Register `bw_get_lineage`, `bw_impact_analysis`, `bw_trace_to_source`.
  - _Depends on: 11, 19_
  - _Requirements: R12.3, R12.4, R12.5 (originally bundled in B6)_
  - _Build prompt: B6_

- [ ] 28. Query tools
  - Register `bw_list_queries`, `bw_get_query`, `bw_get_query_lineage`, `bw_get_query_usage`.
  - _Depends on: 11, 16, 21_
  - _Requirements: R14.1, R14.2, R14.6, R14.8 (originally bundled in B7)_
  - _Build prompt: B7_

- [ ] 29. HANA tools
  - Register `bw_list_calc_views`, `bw_get_calc_view_lineage`, `bw_get_hana_crossings`.
  - _Depends on: 11, 17, 19_
  - _Requirements: R15.1, R15.2, R15.3 (originally bundled in B8)_
  - _Build prompt: B8_

- [ ] 30. Risk and layer-violation tools
  - Register `bw_check_load_latency`, `bw_check_schedule_risk`, `bw_find_layer_violations`.
  - _Depends on: 11, 24_
  - _Requirements: R16.5, R22.4, R24.1_
  - _Build prompt: B9_

- [ ] 31. Resources
  - Register read-only resources: `bw://{system}/profile`, `/catalog`, `/chain/{id}`, `/provider/{name}`, `/transformation/{id}`, `/query/{id}`, `/calcview/{name}`. Carry provenance; absent object → `UnsupportedResult`.
  - _Depends on: 11, 10, 14, 15, 16, 17_
  - _Requirements: R27.1, R27.2, R27.3_
  - _Build prompt: B3–B8_

- [ ] 32. Prompts
  - Register `analyze_impact`, `troubleshoot_missing_data`, `document_dataflow`, `review_scenario`, `onboard_analyst`, `pre_change_checklist`; each takes `system` and composes read-only tools; `review_scenario` drives the matching analyzer.
  - _Depends on: 12, 25, 26, 27, 28, 29, 30_
  - _Requirements: R28.1, R28.2, R28.3_
  - _Build prompt: B9_

---

## Phase 7 — Documentation generation (B10)

- [ ] 33. Documentation generator service + `bw_generate_docs`
  - Render the Section 8 tree to a git-ignored output dir: `index.md`, `01-inventory/` … `08-scenarios/`, `99-gaps-and-risks.md`. Provider pages list sources/targets/frequency/update-mode/transformations/routine-lookups/calc-views/reports. Include Mermaid + graph JSON on lineage pages, backlinks, source-table citations, visible markers on generated descriptions, and a **non-empty** gaps register. Register `bw_generate_docs`.
  - Note: generated KB output goes to a git-ignored directory (`output/`), NOT the repo's `docs/` (which holds server documentation) — see open item on the Section 8 vs Section 10 `docs/` naming overlap.
  - Unit tests: structure created, gaps register non-empty, generated-description marker present, output path outside repo.
  - _Depends on: 19, 20, 21, 24, 11_
  - _Requirements: R25.1, R25.2, R25.3, R25.4, R25.5_
  - _Build prompt: B10_

---

## Phase 8 — Harden and publish (B11)

- [ ] 34. Test coverage and secret-path hardening
  - Complete offline coverage against anonymized synthetic fixtures across all layers; add contract tests asserting provenance on every tool/resource response and absence of host/credentials in any serialized output. Verify `scrub()` covers every error path.
  - _Depends on: 12, 25–33_
  - _Requirements: R29.3, R29.4, R2.4, R5.1_
  - _Build prompt: B11_

- [ ] 35. Docs, security model, and README tool catalog
  - Write the README: what it does, supported-release matrix, quickstart, full tool catalog with parameters, MCP client config example, security model (read-only, no data retention), and the explicit statement that no customer metadata ships with the package.
  - _Depends on: 34_
  - _Requirements: R2 (security model), R29.1_
  - _Build prompt: B11_

- [ ] 36. Customer-data history scan and repository publish
  - Run the customer-object CI check across **full git history** (not just the diff); if anything is found, remediate with `git filter-repo` (a squashed commit does not remove history).
  - Create the private GitHub repository and push per the mission commands. Tag with semantic versioning; defer PyPI publish until the repo is confirmed clean.
  - Report which acceptance criteria in mission Section 11 are met and which are unmet.
  - _Depends on: 35_
  - _Requirements: R29.1, R29.2_
  - _Build prompt: B11_

---

## Dependency summary

```
1 (scaffold, B0)
└─ 2 (models) ─ 3 (profiles) ─ 4 (conn/security)
   ├─ 5 (dialect) ─ 6 (capability) ─ 7 (cache)          [Phase 1 core, B1]
   │                 └─ 8 [LIVE][GATE] capability validation (B2)
   │                     │  ── hard gate: nothing below starts until reviewed ──
   │                     └─ Phase 3 vertical slice (B3):
   │                        9 (repo base) ─ 10 (chains repo)
   │                        11 (FastMCP bootstrap + system tools) ─ 12 (chain tools; bw_list_chains callable) 
   │                           └─ Phase 4 repos (B4/B5/B7/B8): 13 texts ─ 14 providers ─ 15 transformations ─ 16 queries ─ 17 hana
   │                              └─ Phase 5 services: 18 routine parser ─ 19 lineage ─ 20 descriptions ─ 21 query lineage ─ 22 connectors ─ 23 latency ─ 24 analyzers
   │                                 └─ Phase 6 MCP surface: 25 search/describe ─ 26 transform ─ 27 lineage ─ 28 query ─ 29 hana ─ 30 risk ─ 31 resources ─ 32 prompts
   │                                    └─ Phase 7 docs (B10): 33 docgen + bw_generate_docs
   │                                       └─ Phase 8 (B11): 34 harden ─ 35 README/security ─ 36 history scan + publish
```

**Ordering guarantees:**
- Core (2–7) depends only on scaffold; no repositories or tools exist in Phase 1.
- **B2 (8) is a hard gate**: Phase 3 and everything after is blocked until the capability report is reviewed. B2 emits its report as a direct artifact, not an MCP tool.
- **B3 (9–12) is a vertical slice**: repository base + chains repository + FastMCP bootstrap + chain tools, ending with `bw_list_chains` callable from a real MCP client — validating the full pattern before B4–B9.
- **B4–B9 use the repository/tool split**: repositories (13–17) and services (18–24) precede their MCP-surface registration (25–32); each surface task depends on the layers beneath it.
- Docs (33) and hardening/publish (34–36) come last.
