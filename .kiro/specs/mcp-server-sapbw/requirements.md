# Requirements Document — mcp-server-sapbw

## Introduction

`mcp-server-sapbw` is a read-only, system-agnostic MCP (Model Context Protocol) server that
exposes the metadata of any SAP BW-on-HANA system as model-callable tools, resources, and
prompts. A single deployment points at DEV, QA, PRD, or a different client's landscape through
a named connection profile with no code changes. The server auto-detects BW release and
object-model variant at connect time, answers impact-analysis and incident-triage questions in
one call (including dependencies invisible to BW's own where-used lists), and can render a full
markdown knowledge base on demand.

The authoritative scope for these requirements is `.kiro/steering/mission.md`. Section numbers
below refer to that document. Every requirement traces to it.

### Guiding constraints (from mission Section 2 — Non-Negotiable Rules)

These constraints are foundational and apply to every requirement in this document:

1. **Read-only, permanently** — `SELECT` only; no code path may write to a BW system.
2. **Never invent metadata** — a missing table/column yields a structured "unsupported on this release" result, never a guess.
3. **Provenance on every fact** — every returned record carries `source_table` and `source_key`.
4. **No customer metadata in the Git repository, ever** — enforced by `.gitignore` and CI.
5. **Secrets by environment variable only** — no credentials in code, tests, fixtures, logs, or error messages.
6. **Active version only** (`OBJVERS = 'A'`) unless explicitly comparing versions.
7. **Generated content is labelled as generated** — never indistinguishable from stored BW text.

### EARS notation used

- **Ubiquitous:** "The system SHALL …"
- **Event-driven:** "WHEN <trigger> THEN the system SHALL …"
- **State-driven:** "WHILE <state> the system SHALL …"
- **Optional/feature:** "WHERE <feature is present> the system SHALL …"
- **Unwanted behavior:** "IF <condition> THEN the system SHALL …"

### Glossary

- **Profile** — a named connection configuration for one BW system (e.g. `dev`, `qa`, `prd`).
- **Capability record** — the cached result of runtime discovery for one profile: release, ABAP schema, present object-model variants, HANA repository style, log-retention window.
- **Provenance** — `{source_table, source_key}` attached to every returned record.
- **ADSO** — Advanced DSO. **CP** — CompositeProvider. **CKF/RKF** — Calculated / Restricted Key Figure.
- **Advisory hop** — a lineage edge derived from heuristic routine parsing rather than declarative metadata.

---

## Requirements

### Requirement 1 — System-agnostic connection profiles (Core layer)

**User Story:** As a BW consultant working across multiple landscapes, I want to point the server
at any BW system through a named profile, so that I can analyze DEV, QA, PRD, or a client system
without changing code.

#### Acceptance Criteria

1. The system SHALL load connection profiles from a git-ignored `profiles.yaml` (path configurable via `BW_PROFILES_PATH`), each profile named (e.g. `prd`) and carrying `host`, `port`, `user`, `password`, `abap_schema`, `encrypt`, and `read_only_user`.
2. WHERE a profile field is written as `${VAR}` THEN the system SHALL resolve it from an environment variable at load time and SHALL NOT accept an inline literal secret.
3. Every tool, resource, and prompt SHALL accept a `system: str` parameter naming a profile, and the server SHALL be stateless across calls.
4. The system SHALL pool connections per profile and reuse pooled connections across calls to the same profile.
5. WHEN a profile sets `abap_schema: auto` THEN the system SHALL resolve the ABAP schema name at connect time and SHALL NOT hardcode `SAPABAP1` or any other schema name.
6. IF a named profile does not exist THEN the system SHALL return a structured error identifying the unknown profile and listing the configured profile names.
7. The system SHALL connect to at least two different BW systems via profiles with zero code changes (acceptance criterion, mission Section 11).

### Requirement 2 — Read-only enforcement and fail-closed security (Core layer)

**User Story:** As a security-conscious platform owner, I want the server to be structurally
incapable of writing to a BW system, so that pointing it at production carries no write risk.

#### Acceptance Criteria

1. The system SHALL issue `SELECT` statements only and SHALL contain no code path capable of DDL, DML, activation, chain triggering, or writing RFC/BAPI calls.
2. The system SHALL enforce read-only behavior in the connection layer rather than by convention in higher layers.
3. WHEN a connection is established AND the profile sets `read_only_user: true` THEN the system SHALL assert that the connecting user holds no write grants and SHALL fail closed (refuse the connection) if write grants are detected.
4. IF an exception message would contain a connection string, host name, user, or password THEN the system SHALL scrub those values before the message leaves the process (logs, tool responses, errors).
5. The system SHALL NOT write credentials or host names into any tool response, log line, test fixture, or generated artifact.
6. The system SHALL never write generated descriptions or any other synthesized content back to the BW system.

### Requirement 3 — Capability resolution (Core layer)

**User Story:** As an operator connecting to an unknown release, I want the server to discover
what actually exists before running any tool, so that no tool ever queries a non-existent table.

#### Acceptance Criteria

1. WHEN a profile is first connected THEN the system SHALL run capability discovery once and cache the result with a configurable TTL (default 24h).
2. The capability record SHALL contain: BW release, resolved ABAP schema name, which of {classic DSO, ADSO, CompositeProvider, MultiProvider, Open ODS View} are present and populated, whether HANA repository (`_SYS_REPO`) or HDI containers are in use, and the `RSPCPROCESSLOG` retention window.
3. The system SHALL validate table existence via `<ABAP_SCHEMA>.DD02L` (and `SYS.TABLES`/`SYS.VIEWS` for HANA-side objects) and SHALL discover pattern-named tables (e.g. `RSOHCPR%`, `RSTRAN%` text tables) rather than assuming their names.
4. Every repository method SHALL check the capability record before building SQL, and IF a required table is absent for the release THEN the method SHALL return a structured "unsupported on this release" result naming the missing table and, where known, the correct alternative.
5. WHEN `bw_refresh_capabilities(system)` is invoked THEN the system SHALL re-run discovery and replace the cached capability record.
6. The system SHALL correctly report release and object-model variant such that no tool queries a non-existent table (acceptance criterion, mission Section 11).

### Requirement 4 — Metadata caching (Core layer)

**User Story:** As an analyst querying a large production system, I want expensive metadata
extracts cached locally, so that repeated analysis is fast without re-hitting the source.

#### Acceptance Criteria

1. The system SHALL cache extracted structures in a local SQLite file per profile, keyed by object identity plus extraction timestamp.
2. The cache file SHALL live outside the Git repository and match a git-ignored path (`cache/`, `*.sqlite`).
3. WHILE serving runtime statistics (chain/query runtimes, request durations) the system SHALL NOT cache them beyond one hour.
4. WHEN `bw_refresh_cache(system, scope)` is invoked THEN the system SHALL invalidate cached extracts matching the given scope.
5. WHERE a cached extract exists and is within TTL THEN the system SHALL serve from cache rather than re-querying the source.

### Requirement 5 — Provenance on every fact (cross-cutting)

**User Story:** As an analyst who must trust the output, I want every fact traceable to the
metadata row it came from, so that a plausible-but-wrong dependency can never masquerade as fact.

#### Acceptance Criteria

1. Every returned record SHALL carry a `provenance` object of the form `{"source_table": "RSTRAN", "source_key": {"TRANID": "0ABC123", "OBJVERS": "A"}}`.
2. WHERE a fact is aggregated from multiple rows THEN the system SHALL carry provenance for each contributing source table and key.
3. IF a requested fact cannot be traced to a metadata row actually read THEN the system SHALL omit it and record the gap rather than emit an untraceable value.

### Requirement 6 — Active version filtering (cross-cutting)

**User Story:** As an analyst, I want only active object versions returned by default, so that row
counts are not silently multiplied by modified and delivered versions.

#### Acceptance Criteria

1. The system SHALL apply `OBJVERS = 'A'` on all `RSD*`, `RSO*`, `RSTRAN*`, and `RSZ*` table reads by default.
2. WHERE the caller explicitly requests a version comparison THEN the system SHALL relax the active-only filter and SHALL label each record with its `OBJVERS`.

### Requirement 7 — Never invent metadata (cross-cutting)

**User Story:** As a consultant relying on the output, I want the server to refuse to guess, so
that documented gaps replace confident fabrication.

#### Acceptance Criteria

1. IF a table or column does not exist in the connected release THEN the system SHALL return a structured "unsupported on this release" result and SHALL NOT substitute a guessed table or column name.
2. The system SHALL derive element-type decode tables (e.g. `DEFTP` codes) empirically from the connected system and SHALL record the derived decode table in the output rather than assuming a fixed list.
3. WHERE automated lineage necessarily becomes inexact (routine hops, customer-exit variables, dynamic SQL) THEN the system SHALL mark the affected edge/element as advisory rather than presenting it as exact.

### Requirement 8 — Process chains and scheduling repository

**User Story:** As an operations analyst, I want chain structure, schedule, and runtime
statistics, so that I can understand when data is loaded and how reliably.

#### Acceptance Criteria

1. WHEN `bw_list_chains(system, ...)` is invoked THEN the system SHALL return chains filterable by frequency, status, or name pattern, paginated with `limit`/`offset` and a `total_count`.
2. WHEN `bw_get_chain(system, chain_id)` is invoked THEN the system SHALL return the chain structure and steps from `RSPCCHAIN` and SHALL resolve nested (meta) chains recursively.
3. The system SHALL classify chain frequency from `TBTCO`/`TBTCP`/`TBTCS` periodicity and SHALL NOT infer frequency from chain names.
4. WHEN `bw_get_chain_runtimes(system, chain_id, days)` is invoked THEN the system SHALL compute min, median, mean, p95, max, success rate, and critical path over the requested window from `RSPCLOGCHAIN`/`RSPCPROCESSLOG`.
5. IF the requested window exceeds the actual `RSPCPROCESSLOG` retention THEN the system SHALL report the actual window used in the response rather than promising unavailable history.
6. WHERE chains overlap in execution THEN the system SHALL detect and report the observed overlap rather than presenting durations as fixed intrinsic costs.
7. WHEN `bw_get_schedule_matrix(system)` is invoked THEN the system SHALL return all chains × frequency × start time × p95 completion.

### Requirement 9 — InfoProviders repository

**User Story:** As an analyst, I want a universal view of any provider (DSO, ADSO, cube,
MultiProvider, CompositeProvider, InfoObject), so that I can inspect definition, fields, and
consumers regardless of object type.

#### Acceptance Criteria

1. The system SHALL read classic DSOs (`RSDODSO`/`RSDODSOT`/`RSDODSOIOBJ`), InfoCubes and MultiProviders (`RSDCUBE`/`RSDCUBET`/`RSDCUBEIOBJ`/`RSDCUBEMULTI`, distinguishing MultiProviders by cube type), and InfoObjects (`RSDIOBJ`/`RSDIOBJT`, with `RSDCHA`/`RSDKYF`/`RSDBCHATR`/`RSDATRNAV`).
2. WHERE the release exposes Advanced DSOs THEN the system SHALL read them via the runtime-validated `RSOADSO*` tables; IF those tables are absent THEN the system SHALL report ADSO support as unavailable for the release.
3. WHERE the release exposes CompositeProviders THEN the system SHALL discover the `RSOHCPR%` header/texts/part-provider tables at runtime and read part-provider assignments; IF absent THEN the system SHALL report CompositeProvider support as unavailable.
4. WHEN `bw_describe_object(system, name)` is invoked THEN the system SHALL return a universal deep-dive: definition, fields, descriptions, a lineage summary, and consumers, regardless of the object's type.
5. WHEN `bw_search_objects(system, query, ...)` is invoked THEN the system SHALL fuzzy-search by technical name or description across all object types and paginate results.

### Requirement 10 — Transformations and routine source repository

**User Story:** As a data engineer, I want full transformation logic including ABAP routine
source, so that I can see exactly how each target field is derived.

#### Acceptance Criteria

1. WHEN `bw_list_transformations(system, ...)` is invoked THEN the system SHALL return transformations filterable by source, target, or routine presence, paginated.
2. WHEN `bw_get_transformation(system, tran_id)` is invoked THEN the system SHALL return the header (`RSTRAN`), field mappings (`RSTRANFIELD`), and the rule type per target field (`RSTRANRULE`/`RSTRANSTEP`/`RSTRANSTEPRULE`).
3. WHEN `bw_get_routine_code(system, code_id)` is invoked THEN the system SHALL return full ABAP source for start, end, expert, and field routines by joining `RSAABAP` on the code ID ordered by `LINE_NO`.
4. WHEN `bw_analyze_routine(system, code_id)` is invoked THEN the system SHALL return parsed table dependencies, detected anti-patterns, and complexity signals.
5. The system SHALL read DTP headers including `UPDMODE` from `RSBKDTP`, DTP request history from `RSBKREQUEST`, InfoPackages/selections from `RSLDPIO`/`RSLDPSEL`, DataSources and fields from `RSDS`/`RSDSSEGFD`, and request status per provider from `RSSTATMANPART`.

### Requirement 11 — Routine parser (Service layer)

**User Story:** As a data engineer, I want routine ABAP parsed for its real table dependencies
and anti-patterns, so that lineage sees dependencies BW's where-used misses.

#### Acceptance Criteria

1. The routine parser SHALL resolve `SELECT` statements against `/BIC/` and `/BI0/` tables back to the corresponding BW objects.
2. The parser SHALL detect anti-patterns: `SELECT` inside `LOOP`, missing `FOR ALL ENTRIES` guards, hardcoded values, and record-set-altering `DELETE` logic.
3. The parser SHALL extract the leading comment block from `RSAABAP` as candidate documentation.
4. Every routine dependency result SHALL state, in its response payload, that parsing is heuristic and produces a lower bound (dynamic SQL, function-module calls, and class methods are not captured).
5. WHERE a routine dependency is emitted as a lineage edge THEN the system SHALL mark the edge `derivation: routine` and attach the routine ID.
6. WHERE the parser encounters a custom class, function module, method, external `PERFORM`, or dynamic call it cannot resolve THEN the system SHALL emit a structured unresolved-dependency naming the called object (never silently dropping it), so ABAP-layer gaps are countable and visible.
7. The parser SHALL be able to consume ABAP source from an offline source bundle (a local directory of exported files) using the same logic as `RSAABAP`, so unresolved dependencies can be resolved later without a live connection (see Requirement 31).

### Requirement 12 — Lineage graph (Service layer)

**User Story:** As an analyst planning a change, I want a directed lineage graph across all
layers including routine-derived edges, so that I can see the complete blast radius in one call.

#### Acceptance Criteria

1. The system SHALL build a directed lineage graph from transformations and DTPs, with each edge annotated with update mode and the frequency of the chain that executes it.
2. The system SHALL merge routine-derived edges (Requirement 11) into the graph, marked as advisory.
3. WHEN `bw_get_lineage(system, object, direction, depth)` is invoked THEN the system SHALL return upstream, downstream, or both to the requested depth as graph JSON (nodes + edges).
4. WHEN `bw_impact_analysis(system, object)` is invoked THEN the system SHALL return the complete downstream blast radius including routine lookups, calc views, queries, and reports, and SHALL include at least one routine-embedded lookup invisible to BW's own where-used list where such a lookup exists.
5. WHEN `bw_trace_to_source(system, object)` is invoked THEN the system SHALL trace the object back to its originating DataSources hop by hop.
6. The system SHALL model the DataSource as an explicit boundary node carrying an `upstream_resolved` flag and a slot for source-system detail. The current build resolves DataSource-to-report lineage and SHALL set `upstream_resolved = false` on DataSource nodes (source-system parents not yet resolved).
7. An external connector or offline source bundle SHALL be able to attach source-system parent nodes to a DataSource boundary node WITHOUT altering existing node or edge types (extension point).
8. WHERE a routine has an ABAP-layer call the parser cannot resolve (Requirement 11.6) THEN the graph SHALL contain a named `unresolved_dependency` node for it, so the gap is countable and visible in generated documentation.

### Requirement 13 — Descriptions for all objects (Service layer, Section 7)

**User Story:** As a documentation consumer, I want a useful description for every object with
clear provenance, so that I can distinguish stored BW text from server-synthesized text.

#### Acceptance Criteria

1. The system SHALL read stored short and long text from each object's text table in the logon language with English fallback, active version, and SHALL discover the transformation text table via `DD02L WHERE TABNAME LIKE 'RSTRAN%'` rather than assuming its name.
2. The system SHALL assess stored-description quality and flag a description as low-value when it is empty, identical to the technical name, a copy artifact (e.g. `Copy of …`, `ZZ_TEST`, `tmp`), shorter than four words, or in an unexpected language.
3. WHERE a stored description is missing or low-value THEN the system SHALL synthesize one from evidence it already holds (source objects, key fields, semantic key, update mode, load frequency, routine logic summary, known consumers), and for routines SHALL first use the leading `RSAABAP` comment block.
4. Every description result SHALL return `description_short`, `description_long`, `origin` (`stored | generated | stored_augmented`), `quality_flag` (`ok | missing | generic | copy_artifact`), and an `evidence` list of source-table/key citations.
5. WHEN generated descriptions are rendered in documentation THEN the system SHALL show a visible marker identifying them as generated, and SHALL never write them back to BW.
6. The system SHALL produce a description with populated `origin` and `quality_flag` for every supported object type: BEx queries, DSOs/ADSOs, InfoCubes, MultiProviders, CompositeProviders, InfoObjects, transformations, and routines (acceptance criterion, mission Section 11).

### Requirement 14 — BEx query subsystem (Service layer, Section 6)

**User Story:** As a report analyst, I want complete BEx query definitions with field-level
lineage to DataSource, so that I can understand and trace any report end to end.

#### Acceptance Criteria

1. WHEN `bw_list_queries(system, ...)` is invoked THEN the system SHALL return queries filterable by provider, owner, or usage rank, paginated.
2. WHEN `bw_get_query(system, query_id)` is invoked THEN the system SHALL return the query header from `RSZCOMPDIR` (active version) with its description obtained by joining `RSZELTTXT` on `ELTUID = COMPUID`, and SHALL return the complete definition: element tree, RKFs, CKFs, filters, variables, and layout.
3. The system SHALL build the element tree by walking `RSZELTXREF` recursively from the query root `ELTUID`, resolving each node through `RSZELTDIR`, classifying by `DEFTP` using an empirically derived decode table that is recorded in the output.
4. For each element the system SHALL capture: description (`RSZELTTXT`), restrictions (`RSZSELECT`/`RSZRANGE`, including InfoObject, operator, sign, low/high, and variable-reference flag), formula/CKF definitions (`RSZCALC`, operands resolved to readable names), variables (`RSZGLOBV`: technical name, InfoObject, type, processing type, mandatory flag), and the element's placement (rows, columns, free characteristics, default filter, global filter).
5. IF a variable uses customer-exit processing THEN the system SHALL flag it as a lineage dead end requiring manual review, name the exit where derivable, and SHALL NOT present its resolved values.
6. WHEN `bw_get_query_lineage(system, query_id)` is invoked THEN the system SHALL emit, per InfoObject referenced in the query, a field-level path: InfoObject → provider field → (for CompositeProviders) part-provider and calc-view column → inbound transformation target field → rule type → source field(s) or routine dependencies, repeated upward until a DataSource field is reached; and SHALL mark routine hops `derivation: routine` with the routine ID.
7. The system SHALL detect queries reused as InfoProviders and elements shared across queries (`RSZELTXREF` spanning multiple `COMPUID`s), reporting shared elements as change-impact multipliers.
8. WHEN `bw_get_query_usage(system, query_id)` is invoked THEN the system SHALL return execution counts, runtimes, and last-used from runtime-validated BW statistics tables, rank queries by real usage, and label a query with zero executions in 12 months as a decommissioning candidate.
9. The system SHALL annotate every query with the chain ID, frequency, and p95 completion time of the chain(s) loading its provider(s), so that data currency is answerable in one call.

### Requirement 15 — HANA layer repository

**User Story:** As an architect, I want calc-view dependencies and every BW↔HANA boundary
crossing, so that I can see the parts of lineage that live outside BW.

#### Acceptance Criteria

1. WHEN `bw_list_calc_views(system, ...)` is invoked THEN the system SHALL return calculation views, optionally filtered to those consumed by BW, paginated.
2. WHEN `bw_get_calc_view_lineage(system, view_name)` is invoked THEN the system SHALL extract dependencies from `SYS.OBJECT_DEPENDENCIES`, resolve `/BIC/` base tables to BW objects, and parse the view definition from `_SYS_REPO.ACTIVE_OBJECT` (or the HDI-container equivalent detected by the capability resolver) for joins, calculated columns, and input parameters.
3. WHEN `bw_get_hana_crossings(system)` is invoked THEN the system SHALL return every BW↔HANA boundary crossing in both directions.
4. WHERE `SYS.M_CS_TABLES` is available THEN the system SHALL surface table size/memory to flag large full-load targets.

### Requirement 16 — Risk analyzer 9.1: full-update DSOs with lookups on once-daily ADM DSOs

**User Story:** As a data quality owner, I want to find loads that enrich new data against stale
master data, so that I can prevent silent data-currency defects.

#### Acceptance Criteria

1. The system SHALL cross-reference DTPs with `UPDMODE = 'F'` → their transformations → routine lookups parsed from `RSAABAP` → the load frequency of each looked-up DSO.
2. WHEN the analyzer runs THEN the system SHALL produce a latency-contract table stating, for each lookup, whether the looked-up target completes before the consuming DTP starts on every scheduled run.
3. IF a chain running more than once daily reads an ADM DSO refreshed once daily THEN the system SHALL flag the case as a stale-master-data risk.
4. The system SHALL include extractor-side constraints (e.g. `0FI_AR_4` class: delta method, safety interval, re-init conditions) from `RSDS`/`ROOSOURCE` as named cases.
5. WHEN `bw_check_load_latency(system)` is invoked THEN the system SHALL return findings with severity, affected objects, evidence, and recommended action.

### Requirement 17 — Risk analyzer 9.2: deep EDW→ADM→calc view→CP→BEx chains

**User Story:** As a support engineer, I want the full shape and latency of deep layer stacks
with a per-layer runbook, so that incident triage names the exact object to inspect at each hop.

#### Acceptance Criteria

1. The system SHALL trace every complete chain of shape EDW DSO → ADM DSO → calc view → CompositeProvider → BEx query.
2. For each such chain the system SHALL report layer count and cumulative latency.
3. The system SHALL emit a troubleshooting runbook naming the exact table or view to inspect at each layer, in order.

### Requirement 18 — Risk analyzer 9.3: CompositeProviders feeding EDW DSOs

**User Story:** As an architect, I want every CompositeProvider→DSO transformation documented,
so that I understand the silent activation-order dependency a calc-view change creates.

#### Acceptance Criteria

1. The system SHALL identify every transformation whose source is a CompositeProvider and whose target is a DSO.
2. For each the system SHALL document which calculations occur in the calc view versus the BW transformation.
3. The system SHALL document the activation-order dependency and warn that a calc-view change silently changes DSO content on the next load with no BW where-used warning.

### Requirement 19 — Risk analyzer 9.4: InfoObjects loaded from CompositeProviders

**User Story:** As a load-scheduling owner, I want every InfoObject loaded from a CompositeProvider
and the chains that violate master-before-transaction sequencing, so that I can fix load order.

#### Acceptance Criteria

1. The system SHALL identify every transformation targeting an InfoObject with a CompositeProvider source.
2. The system SHALL document the sequencing requirement that master data must load before any transaction load reading it.
3. The system SHALL identify which chains currently violate that sequencing.

### Requirement 20 — Risk analyzer 9.5: merged Orders/Billing/Shipments/Deliveries DSO

**User Story:** As a data modeler, I want a full decomposition of a merged-stream DSO with a
per-field lineage matrix, so that I can see collision risk and hidden semantic differences.

#### Acceptance Criteria

1. The system SHALL produce a 3–4 layer decomposition of the merged DSO.
2. Per contributing stream the system SHALL report source DataSource, key mapping, uniqueness keys, collision risk on the merged key, and the semantic differences the merge hides.
3. The system SHALL produce a per-layer field-lineage matrix stating, for every field in the final DSO, which stream(s) populate it, through which rule, at which layer.

### Requirement 21 — Risk analyzer 9.6: ECC extractor enhancements (connector-dependent)

**User Story:** As an integration owner, I want each enhanced DataSource's appended fields and
the tables its exit reads, so that I can spot cross-team coordination and performance risks.

**Access note (reclassified):** The ECC extractor enhancement source (CMOD/BAdI ABAP) and the
extractor definition tables `ROOSOURCE`/`ROOSFIELD` live in the **ECC source system** — a different
database that is not reachable over the BW HANA connection. Scenario 9.6 is therefore **not a core
BW capability**; like 9.7/9.8 it depends on a separate pluggable connector (see Requirement 30).
The BW side can only see the replicated DataSource (`RSDS`/`RSDSSEGFD`); the enhancement logic
itself is invisible from BW.

#### Acceptance Criteria

1. WHERE an ECC connector is configured THEN, per enhanced DataSource, the system SHALL report appended fields (`ROOSFIELD` vs. the standard extract structure in `DD03L`), the enhancement technique, and the tables the enhancement reads with their owning functional area.
2. WHERE an ECC connector is configured AND an enhancement reads another team's data THEN the system SHALL flag it as a coordination-risk item.
3. WHERE an ECC connector is configured AND an exit performs per-record `SELECT`s THEN the system SHALL flag it as a performance risk.
4. WHERE no ECC connector is configured THEN the system SHALL produce the 9.6 template, populate whatever is derivable from the BW-side DataSource replica alone, and explicitly report the enhancement-side data as unpopulated, naming the required connector.
5. WHERE no ECC connector is configured THEN the system SHALL nonetheless flag *likely* enhancements from BW alone by detecting customer-namespace fields (`Z*` / `Y*` / `ZZ*`) in `RSDSSEGFD` against the DataSource's standard extract-structure field set, and SHALL label this result `heuristic` — it identifies that an enhancement exists, never what it does.

### Requirement 22 — Risk analyzer 9.7: report schedules vs. chain completion

**User Story:** As a BI operations owner, I want downstream report schedules compared against the
p95 completion of feeding chains, so that I can eliminate reports that run before their data is ready.

#### Acceptance Criteria

1. The system SHALL produce a timeline table of each report/extract's scheduled start versus the p95 completion of the chain feeding its provider.
2. The system SHALL compute the safety margin and flag negative or sub-30-minute margins.
3. The system SHALL recommend event-based triggering over clock-based scheduling for the flagged set.
4. WHEN `bw_check_schedule_risk(system)` is invoked THEN the system SHALL return findings with severity, affected objects, evidence, and recommended action.
5. WHERE Tableau/BOBJ schedule metadata is not available (no connector configured) THEN the system SHALL produce the timeline template and SHALL explicitly report that the report-side rows could not be populated.

### Requirement 23 — Risk analyzer 9.8: Tableau dashboards directly on calc views

**User Story:** As a governance owner, I want dashboards that bypass BW identified with their
calc-view relationship to the CompositeProvider path, so that I know when one change breaks two paths.

#### Acceptance Criteria

1. The system SHALL identify dashboards reading calc views directly, bypassing BW.
2. Per dashboard the system SHALL document the parallel path and state whether its calc view is the same one the CompositeProvider uses.
3. IF the calc view is shared THEN the system SHALL report that one change breaks both paths at once; IF the views are separate THEN the system SHALL report that the two paths can silently diverge in numbers.
4. WHERE Tableau metadata is not available (no connector configured) THEN the system SHALL report the finding as unpopulated and name the connector required.

### Requirement 24 — Layer-violation finder (Service layer)

**User Story:** As an architect, I want structural anti-patterns surfaced, so that I can catch
CompositeProvider→DSO, CompositeProvider→InfoObject, and over-deep DSO stacks.

#### Acceptance Criteria

1. WHEN `bw_find_layer_violations(system)` is invoked THEN the system SHALL return CompositeProvider→DSO edges, CompositeProvider→InfoObject edges, and DSO stacks exceeding a configurable depth, each with evidence and provenance.

### Requirement 25 — Knowledge base generation (Service layer, Section 8)

**User Story:** As a documentation consumer, I want a full markdown knowledge base rendered on
demand to a local directory, so that the analysis is browsable and citable outside the tool.

#### Acceptance Criteria

1. WHEN `bw_generate_docs(system, output_dir)` is invoked THEN the system SHALL render markdown to a git-ignored local directory using the structure `index.md`, `01-inventory/`, `02-process-chains/`, `03-lineage/`, `04-providers/`, `05-transformations/`, `06-queries/`, `07-hana/`, `08-scenarios/`, and `99-gaps-and-risks.md`.
2. Each provider page SHALL list sources, targets, load frequency, update mode, inbound/outbound transformations, routine-embedded lookups, calc views reading it, and reports depending on it.
3. Every page SHALL carry backlinks and source-table citations, lineage pages SHALL include Mermaid diagrams plus graph JSON, and generated descriptions SHALL render with a visible marker.
4. The system SHALL always produce a non-empty `99-gaps-and-risks.md` capturing everything unverified or inaccessible (acceptance criterion, mission Section 11).
5. The system SHALL render docs only to a location outside the Git repository.

### Requirement 26 — MCP tool surface

**User Story:** As an MCP client, I want the full catalog of tools with consistent pagination,
so that I can drive every capability of the server programmatically.

#### Acceptance Criteria

1. The system SHALL register all tools listed in mission Section 4: `bw_list_systems`, `bw_system_profile`, `bw_search_objects`, `bw_describe_object`, `bw_list_chains`, `bw_get_chain`, `bw_get_chain_runtimes`, `bw_get_schedule_matrix`, `bw_get_lineage`, `bw_impact_analysis`, `bw_trace_to_source`, `bw_list_transformations`, `bw_get_transformation`, `bw_get_routine_code`, `bw_analyze_routine`, `bw_list_queries`, `bw_get_query`, `bw_get_query_lineage`, `bw_get_query_usage`, `bw_list_calc_views`, `bw_get_calc_view_lineage`, `bw_get_hana_crossings`, `bw_check_load_latency`, `bw_check_schedule_risk`, `bw_find_layer_violations`, `bw_generate_docs`, `bw_refresh_capabilities`, and `bw_refresh_cache`.
2. Every tool name SHALL match `^[a-zA-Z][a-zA-Z0-9_]*$`, contain no hyphens or dots, and stay ≤40 characters (to remain under 64 including the Kiro server prefix).
3. Every list tool SHALL accept `limit` and `offset` and return a `total_count`.
4. IF a result set is large THEN the tool SHALL return a summary plus a resource URI rather than dumping thousands of rows into the response.
5. WHEN `bw_list_systems(...)` is invoked THEN the system SHALL return configured profiles and their connection status, and WHEN `bw_system_profile(system)` is invoked THEN the system SHALL return release, capabilities, object counts, and log-retention window.

### Requirement 27 — MCP resource surface

**User Story:** As an MCP client, I want URI-addressable read-only context, so that I can pull an
object into context without a tool round-trip.

#### Acceptance Criteria

1. The system SHALL expose read-only resources at `bw://{system}/profile`, `bw://{system}/catalog`, `bw://{system}/chain/{chain_id}`, `bw://{system}/provider/{name}`, `bw://{system}/transformation/{tran_id}`, `bw://{system}/query/{query_id}`, and `bw://{system}/calcview/{view_name}`.
2. Resource responses SHALL carry provenance identically to tool responses.
3. IF a resource references an object absent on the connected release THEN the system SHALL return a structured "unsupported on this release" result.

### Requirement 28 — MCP prompt surface

**User Story:** As an analyst, I want reusable prompt templates that compose several tools, so
that common workflows run in one step.

#### Acceptance Criteria

1. The system SHALL register the prompts `analyze_impact`, `troubleshoot_missing_data`, `document_dataflow`, `review_scenario`, `onboard_analyst`, and `pre_change_checklist`.
2. WHEN `review_scenario` is invoked with a scenario identifier THEN the prompt SHALL drive the corresponding analyzer from Requirements 16–23.
3. Each prompt SHALL take a `system` parameter and compose only read-only tools.

### Requirement 29 — No customer metadata in the repository; offline tests

**User Story:** As an open-source maintainer, I want the repository to contain zero customer
metadata and a full offline test suite, so that publishing carries no IP-leak risk.

#### Acceptance Criteria

1. The repository SHALL contain no customer metadata; `.gitignore` SHALL exclude `.env`, `profiles.yaml`, `output/`, `extracts/`, `cache/`, `*.sqlite`, and `.kiro/settings/mcp.json`.
2. CI SHALL fail the build if any commit outside `tests/fixtures/` contains strings matching customer object-naming patterns (`/BIC/`, `/BI0/`, real chain or DSO prefixes).
3. The full test suite SHALL run entirely offline against synthetic, anonymized fixtures with no live BW system.
4. Test fixtures SHALL use synthetic names only and SHALL contain no real credentials, host names, or customer object names.

### Requirement 30 — Pluggable external-system connectors (design constraint)

**User Story:** As a maintainer, I want metadata that lives outside BW-on-HANA sourced through
pluggable connectors, so that scenarios 9.6, 9.7, and 9.8 can be completed later without touching
the BW core.

#### Acceptance Criteria

1. The system SHALL define external-system metadata access behind a connector interface separate from the BW core, covering at least: the **ECC source system** (extractor enhancement source / `ROOSOURCE`/`ROOSFIELD`, for 9.6), **Tableau** (Metadata API / `workgroup` repository, for 9.7/9.8), and **BOBJ** (BOE Query Builder / RESTful RaaS, for 9.7).
2. WHERE a required connector is not configured THEN the system SHALL still produce the corresponding scenario template (9.6/9.7/9.8) and SHALL report the externally-sourced data as unpopulated, naming the required connector.
3. The BW HANA connection SHALL never be assumed to reach ECC, Tableau, or BOBJ; those are distinct systems with distinct credentials.
4. WHERE the ECC connector is implemented THEN it SHALL use a SQL Server driver (`pyodbc` with ODBC Driver 18 for SQL Server), target the `SAP<SID>` schema, validate every object name at connect time (as the BW capability resolver does), and be scoped to the enhancement **inventory** only (`ROOSOURCE`, `ROOSFIELD`, `DD02L`/`DD03L` append structures, `MODSAP`/`MODACT`, `SXS_ATTR`/`SXC_EXIT`, `ENHHEADER`/`ENHOBJ`); it SHALL NOT attempt to read ABAP source (`REPOSRC.DATA` is compressed on every platform).

### Requirement 31 — Offline ABAP source-bundle ingestion (extension point)

**User Story:** As a maintainer, I want the routine parser to be able to consume exported ABAP
source files from a local directory, so that unresolved ABAP-layer dependencies can be resolved
offline — without a live connection or RFC — from both the BW and ECC sides.

#### Acceptance Criteria

1. The system SHALL define a filesystem "source bundle" (a local directory of exported ABAP source files, e.g. the `ZXRSAU0x` extractor user-exit include family and custom BW classes/function modules) that the existing routine parser can consume with the same logic it applies to `RSAABAP`.
2. WHERE a source bundle is provided THEN the system SHALL match its files to `unresolved_dependency` nodes by the called object name and resolve them, updating the affected lineage edges/nodes.
3. The source-bundle path SHALL require no live connection and no RFC, and SHALL be git-ignored (customer source is customer IP).
4. This is an extension point: the interface is designed now; the implementation is deferred.

---

## Appendix A — Section 5 table runtime-validation matrix

Per mission Section 5 and Non-Negotiable Rule 2, the capability resolver validates **every** table
below at runtime before any repository builds SQL against it. Two validation tiers apply:

- **EXISTENCE** — the canonical name is known; the resolver confirms it exists for the release/schema via `<ABAP_SCHEMA>.DD02L` (ABAP tables) or `SYS.TABLES`/`SYS.VIEWS` (HANA objects) and records presence + row-count sanity.
- **DISCOVER** — the name itself varies by release; the resolver must discover it by pattern (e.g. `DD02L WHERE TABNAME LIKE '…%'`) rather than assume. All ⚠️ entries in Section 5 fall here.

Every table is flagged; the DISCOVER tier is the set that **must not be assumed from memory**.

### Process chains and scheduling

| Table | Tier | Note |
|---|---|---|
| `RSPCCHAIN` | EXISTENCE | Chain edge list |
| `RSPCCHAINATTR` | EXISTENCE | Chain header |
| `RSPCCHAINT` | EXISTENCE | Chain descriptions |
| `RSPCVARIANT` | EXISTENCE | Variant parameters |
| `RSPCVARIANTT` | EXISTENCE | Variant texts |
| `RSPCLOGCHAIN` | EXISTENCE | Run headers |
| `RSPCPROCESSLOG` | EXISTENCE | Per-step runtimes; **also probe earliest entry for retention window** |
| `RSPCLOGS` | EXISTENCE | Step messages |
| `TBTCO` | EXISTENCE | Job header (periodicity source) |
| `TBTCP` | EXISTENCE | Job steps |
| `TBTCS` | EXISTENCE | Job schedule |

### Data flow and transformations

| Table | Tier | Note |
|---|---|---|
| `RSTRAN` | EXISTENCE | Transformation header (`OBJVERS='A'`) |
| `RSTRANFIELD` | EXISTENCE | Field mapping |
| `RSTRANRULE` | EXISTENCE | Rule definitions |
| `RSTRANSTEP` | EXISTENCE | Rule-to-step |
| `RSTRANSTEPRULE` | EXISTENCE | Rule-to-step |
| `RSAABAP` | EXISTENCE | ABAP source lines (large; cache before use) |
| `RSBKDTP` | EXISTENCE | DTP header incl. `UPDMODE` |
| `RSBKREQUEST` | EXISTENCE | DTP request history |
| `RSLDPIO` | EXISTENCE | InfoPackages |
| `RSLDPSEL` | EXISTENCE | InfoPackage selections |
| `RSDS` | EXISTENCE | DataSources |
| `RSDSSEGFD` | EXISTENCE | DataSource fields |
| `RSSTATMANPART` | EXISTENCE | Request status per provider |
| ⚠️ transformation **text** table (`RSTRAN%`) | **DISCOVER** | Section 7 Step 1: discover via `DD02L WHERE TABNAME LIKE 'RSTRAN%'`; do not assume the text-table name |

### InfoProviders and their descriptions

| Table | Tier | Note |
|---|---|---|
| `RSDODSO` | EXISTENCE | Classic DSO header |
| `RSDODSOT` | EXISTENCE | Classic DSO texts |
| `RSDODSOIOBJ` | EXISTENCE | Classic DSO fields |
| ⚠️ `RSOADSO` | **DISCOVER** | Advanced DSO header — verify/discover per release |
| ⚠️ `RSOADSOT` | **DISCOVER** | Advanced DSO texts — verify/discover per release |
| ⚠️ `RSOADSOIOBJ` | **DISCOVER** | Advanced DSO fields — verify/discover per release |
| `RSDCUBE` | EXISTENCE | InfoCubes + MultiProviders (filter by cube type) |
| `RSDCUBET` | EXISTENCE | Cube texts |
| `RSDCUBEIOBJ` | EXISTENCE | Cube fields |
| `RSDCUBEMULTI` | EXISTENCE | MultiProvider → part-provider |
| ⚠️ `RSOHCPR*` | **DISCOVER** | CompositeProvider — discover header/texts/part-provider tables via `DD02L WHERE TABNAME LIKE 'RSOHCPR%'` |
| `RSDIOBJ` | EXISTENCE | InfoObjects |
| `RSDIOBJT` | EXISTENCE | InfoObject texts |
| `RSDCHA` | EXISTENCE | Characteristics |
| `RSDKYF` | EXISTENCE | Key figures |
| `RSDBCHATR` | EXISTENCE | Attributes |
| `RSDATRNAV` | EXISTENCE | Navigation attributes |

### BEx queries

| Table | Tier | Note |
|---|---|---|
| `RSZCOMPDIR` | EXISTENCE | Query directory (`OBJVERS='A'`) |
| `RSZCOMPIC` | EXISTENCE | Query → InfoProvider |
| `RSZELTDIR` | EXISTENCE | Element directory |
| `RSZELTXREF` | EXISTENCE | Element hierarchy (walk recursively) |
| `RSZELTTXT` | EXISTENCE | Element texts incl. query description via `ELTUID=COMPUID` |
| `RSZSELECT` | EXISTENCE | Restrictions |
| `RSZRANGE` | EXISTENCE | Ranges |
| `RSZCALC` | EXISTENCE | CKF / formula definitions |
| `RSZGLOBV` | EXISTENCE | Variables |
| `RSZELTPROP` | EXISTENCE | Element properties |
| `RSRREPDIR` | EXISTENCE | Report directory / generation status |
| ⚠️ `RSDDSTAT_OLAP` | **DISCOVER** | Query usage statistics — verify/discover name (`RSDDSTAT%`) per release |
| ⚠️ `RSDDSTATHEADER` | **DISCOVER** | Query usage statistics header — verify/discover name (`RSDDSTAT%`) per release |

### HANA layer and dictionary

| Object | Tier | Note |
|---|---|---|
| `SYS.OBJECT_DEPENDENCIES` | EXISTENCE | Calc view → base table lineage (HANA catalog) |
| `_SYS_REPO.ACTIVE_OBJECT` | **DISCOVER** | Present only with repository style; HDI containers use a different location — resolver decides |
| `SYS.VIEWS` | EXISTENCE | Runtime catalog (`_SYS_BIC`) |
| `SYS.VIEW_COLUMNS` | EXISTENCE | Runtime catalog |
| `SYS.COLUMNS` | EXISTENCE | Runtime catalog |
| `SYS.M_CS_TABLES` | EXISTENCE | Table size/memory (monitoring view) |
| `DD02L` | EXISTENCE | Table existence dictionary (in resolved ABAP schema) |
| `DD02T` | EXISTENCE | Table texts |
| `DD03L` | EXISTENCE | Columns |
| `DD04T` | EXISTENCE | Data-element texts |

### Referenced by scenarios (validate before use)

| Object | Tier | Note |
|---|---|---|
| ⚠️ `ROOSOURCE` | **DISCOVER** | Extractor metadata (scenarios 9.1, 9.6) — validate presence/schema; not in the Section 5 core map |
| ⚠️ `ROOSFIELD` | **DISCOVER** | Extractor appended-field metadata (scenario 9.6) — validate presence/schema |

**Summary of DISCOVER-tier (must never be assumed from memory):** the `RSOADSO*` family,
the `RSOHCPR*` family, the `RSDDSTAT*` statistics family, the `RSTRAN%` transformation text
table, the HANA calc-view definition location (`_SYS_REPO.ACTIVE_OBJECT` vs. HDI container),
and the extractor tables `ROOSOURCE`/`ROOSFIELD`. All other tables are EXISTENCE-tier: the
canonical name is known but presence is still confirmed at runtime before any SQL is built.
