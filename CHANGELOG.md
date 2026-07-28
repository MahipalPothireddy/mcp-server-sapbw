# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — capability merge from two script-based BW analysis projects

Eight vertical slices, each with offline tests. Where a source project's claim disagreed with the
system, the system won and the correction is noted.

- **CompositeProvider part providers, via the generated calc view.** `RSOHCPR.XML_DEF` is empty on
  every CompositeProvider on the reference system, so composition was previously undiscoverable.
  Parts are now resolved from the base tables of the CompositeProvider's generated HANA calc view,
  through a shared physical-table ↔ BW-object resolver (classic DSO `/BIC/A<n>00` active and `…40`
  activation queue, ADSO `…1|2|3`, cube `F`/`E`, InfoObject `P`, namespaced variants), with
  master-data side tables and hierarchy views excluded. Lineage emits `composite_part` edges where
  CompositeProviders previously dead-ended. **Correction:** these require
  `SYS.OBJECT_DEPENDENCIES.DEPENDENCY_TYPE = 2` (transitive), not 1 — BW layers the calc view over
  intermediate views, so type-1 dependencies are master-data side tables only. Type 1 remains
  correct for direct-read crossings. The resolver was reverse-validated against all 864 `/BIC/A*`
  tables on the reference system.
- **`bw_render_lineage`** — data-flow diagrams as images. Deterministic layered-DAG layout
  (longest-path layering with bounded relaxation so a cycle cannot hang it, barycentre ordering),
  emitted as self-contained SVG or PNG with a legend, per-type node colour and shape, dashed grey
  advisory edges, collision-avoiding edge labels, and an on-canvas truncation warning. Rendering is
  entirely local: SVG uses only the standard library, PNG uses the optional `viz` extra. A hosted
  renderer was rejected outright — it would exfiltrate customer object names.
- **Transformation rule depth.** Aggregation behaviour, rule group type, no-conversion flag, key
  fields, constant values, and **declared** lookups from `RSTRANSTEPMASTER`/`ODSO`/`ADSO` — exact
  dependencies, unlike routine-parsed ones. Where a lookup miss substitutes a constant rather than
  failing, scenario 9.1 flags it: the load changes data silently. **Correction:**
  `RSTRANRULE.AGGR` is not a boolean overwrite-vs-summation flag as the source projects assumed; it
  is a five-value domain (`MOV`/`SUM`/`MIN`/`MAX`/`NOP`) decoded from the ABAP dictionary.
  `FIELD_USAGE` is entirely empty on the reference system and is deliberately not surfaced.
- **Observed cadence and load closure (`bw_get_load_closure`).** Cadence is banded from the median
  gap between runs, with runs-per-day only promoting a daily chain to intraday; a single recorded run
  yields `unknown` at low confidence rather than a force-fitted band; the reference date is the
  latest run in the system, never today. Load closure resolves chain → providers recursively through
  nested sub-chains (where most loads actually live) and provider → loading chains including parent
  chains, whose schedule governs. Step scope is categorised from process type codes, never names.
  **Correction of an earlier claim in this repo:** "object → loading chain is not derivable" was
  wrong; it came from probing `RSPCVARIANT` (parameters) instead of `RSPCCHAIN` (steps). 99% of
  `DTP_LOAD` steps join cleanly. Every "not derivable" caveat has been removed.
- **`bw_get_provider_health`** — volume from the HANA monitoring view with active, inbound and
  changelog rows reported separately (summing them hides changelog bloat), plus request-level
  currency from BW's own request ledger. Data age is measured against the latest request in the
  system, so a restored copy is not read as stale. "Could not locate the tables" and "the tables
  exist and are empty" are separate facts.
- **`bw_get_source_systems` and `bw_list_extractor_enhancements`.** Topology from the logical systems
  DataSources actually extract from, compared against the registry, so a logical system referenced
  but unregistered is surfaced — the signature of a system copy without BDLS. `SRCTYPE` decoding
  carries dual confidence: the dictionary domain documents three codes while the live data holds
  eight, so the undocumented ones get a conventional reading labelled advisory. The source projects'
  practice of inferring risk from a DataSource *name* prefix is deliberately not carried over.
- **`bw_get_extractor_exit_code`** — extractor-exit ABAP read from the source system over ADT.
  GET-only with no CSRF token and a stateless session, so it can neither write nor take locks;
  credentials come through an optional `ecc_systems` profile with the same `${VAR}`-only rule, TLS by
  default, and plain HTTP requiring a separate explicit opt-in. Risk is attributed per `CASE` branch
  rather than per include, because one include serves every enhanced DataSource and crediting the
  whole include to one of them would manufacture false high-severity findings. **Correction of an
  earlier claim in this repo:** ABAP source is *not* unreachable because `REPOSRC.DATA` is
  compressed — that reasoning was about reading the table directly, and ADT renders source
  server-side as `text/plain`.
- **`bw_get_routine_register`** — every transformation routine in the system, ranked. Three bulk
  reads rather than a per-transformation loop. Size and portfolio totals are complete; pattern
  detection is limited to the largest `parse_budget` routines, and an unparsed entry reports **no**
  pattern counts rather than zeroes, which would read as "clean". Ranking is by measured pattern
  count then measured line count — no composite score with invented weights.
- **Query origin, and `bw_find_unused_providers`.** A technical name prefixed `!!` marks a query
  created ad hoc in the BEx Analyzer rather than Query Designer — a navigation artefact, not a
  maintained report. `bw_list_queries` gains an `origin` filter (default `all`, so existing behaviour
  is unchanged) and every summary carries `origin` with its basis stated, since BW stores no flag.
  `bw_find_unused_providers` reports a provider only when it feeds no transformation, has no
  Query-Designer query, and is no CompositeProvider part — that third route is essential, since a
  CompositeProvider consumes its parts through a calc view and ignoring it would flag every DSO
  beneath one.
- **Write-back loop detection** in the layer-violation analyzer: a transformation whose source and
  target are the same object (its load is not repeatable) and two objects that each feed the other
  (no load order is correct, so scheduling cannot fix it). Both high severity. Longer cycles are not
  searched and the report says so.

### Changed
- `scripts/customer_metadata_scan.py` now strips backslashes before matching. A real customer object
  name had evaded the check because a SQL-`LIKE` escape split the token mid-name.
- `ExternalConnector.kind` is a read-only property, so a connector can narrow it to its own literal.

### Fixed
- **Name filters silently matched nothing.** `bw_search_objects` treated any pattern containing `_`
  as pre-authored and skipped `%`-wrapping, so a partial BW name (nearly all of them contain an
  underscore) matched only an exact full name; `bw_list_chains.name_pattern` was passed into
  `CHAIN_ID LIKE ?` verbatim, so a bare term matched nothing at all; and
  `bw_list_transformations.source_name` / `target_name` used equality, which can never match a
  DataSource endpoint (stored space-padded as `<DATASOURCE><padding><LOGSYS>`). All four now share
  one rule via `core.dialect.like_term`: a bare term is a case-insensitive substring match with
  `LIKE` metacharacters escaped (emitting `ESCAPE` only when needed), while a term containing `%`
  stays under the caller's control. An empty result is the worst failure mode for a model-facing
  tool, so this is documented in the tool docstrings and the README.
- **Scenario 9.1 reported findings it could not evaluate.** A full-update load whose routines
  resolved no lookup has no latency contract to check, but was emitted as a `low` finding — and
  because candidates were truncated in alphabetical order, those empty findings could crowd the real
  risks out of the page entirely. Such candidates are now excluded from the findings and counted in
  the caveats (a parser lower bound, not proof of no lookup), scanning continues past them within a
  parse budget, and severity scales with the number of looked-up objects.

### Added
- **Calc view → InfoProvider resolution (`0BW:BIA:` views).** BW generates a per-InfoProvider HANA
  view named `0BW:BIA:<PROVIDER>` (with `:J1.CALC.n` / `.CONV*` internal nodes), and that is what
  sits on the BW side of a `bw_reads_hana` crossing — previously left unresolved. The provider is
  now parsed from the view name and its **type confirmed** against the provider header tables, so
  `bw_get_hana_crossings` resolves the calc-view → CompositeProvider hop that BW's own where-used
  lists do not report, and `bw_get_calc_view_lineage` gains `consuming_bw_providers` (deduplicated
  per provider). `HanaCrossing.resolution` distinguishes `bic_table` / `bw_provider_view` /
  `unresolved`, and an unconfirmed provider is named without asserting it exists (`verified=false`).
  This closes the deferred calc-view → CompositeProvider mapping without parsing `RSOHCPR.XML_DEF`.
  Generated docs render the hop as a "Calc view -> consuming InfoProvider" table in `07-hana`,
  derived from crossing rows already fetched (no extra queries).
- `tests/sqllike.py`: a minimal SQL `LIKE` evaluator so fixture connections honour built patterns
  the way HANA would — a substring-checking fixture is how the underscore defect survived the
  original suite.
- Shared `models.providers.classify_cube_type` (the `RSDCUBE.CUBETYPE` mapping was duplicated in the
  search and providers repositories).
- Project scaffold (build prompt B0): `src/mcp_server_sapbw` package tree (core, repositories,
  services, connectors, models, prompts), `server.py` entry-point stub, and offline test scaffold.
- `pyproject.toml` with pinned `fastmcp==3.4.4`, `hdbcli`, `pydantic`, `pyyaml`, optional `rfc`
  extra, and ruff / mypy (strict) / pytest / coverage configuration.
- `.gitignore` hardening, `profiles.example.yaml`, `.env.example`.
- Documentation: README skeleton, `SECURITY.md`, `CONTRIBUTING.md`, this changelog.
- Licensed under Apache-2.0.
- GitHub Actions CI: ruff, mypy, pytest (offline), secret scanning, and a customer-object
  leak check.

- Core layer (build prompt B1), offline and mocked-connection tested:
  - Models: `Provenance`, `UnsupportedResult`, `CapabilityRecord` + `TableStatus`.
  - `ProfileManager` with `${VAR}` interpolation and inline-secret rejection.
  - `ReadOnlyConnectionPool` with a statement-level SELECT/WITH guard (enforced before the driver),
    a fail-closed effective-privileges grant check, and secret scrubbing.
  - `SqlDialect` (parameterized SELECTs, `OBJVERS='A'` auto-injection, pagination/count).
  - `CapabilityResolver` (ABAP schema resolution, existence + discover tiers, object-model
    detection, log-retention measurement) with a re-added `hdbcli` mypy override.
  - `SqliteCache` (per-profile, two-tier TTL with a 1h runtime cap, fingerprint + scope invalidation).
- Apache-2.0 license (was MIT); CI customer-metadata check extended with the Z/Y namespace pattern
  and a full-git-history scan.

- Live capability discovery (build prompt B2), verified against QA (BW 7.50):
  - `scripts/capability_report.py`: connects read-only through the pool (running the fail-closed
    grant check), runs the resolver, and writes a no-secrets capability report to `output/`.
  - TLS connection options (`encrypt`, `ssl_validate_certificate`) plumbed through profiles and the
    connection pool.
  - Resolver corrected against the live release: RSPCPROCESSLOG timing columns, and log-retention
    measurement bounded by `RSPCLOGCHAIN.DATUM` and capped at `MAX_RUNTIME_WINDOW_DAYS` (1 year).

- Chains vertical slice (build prompt B3), offline- and live-smoke-verified:
  - Chain domain models (`models/chains.py`): `Chain`, `ChainProcess`, `ChainEdge`, `ChainSummary`,
    `ChainRuntimes`, `DurationStats`, `StepRuntime`, `ScheduleMatrixEntry`, and an observed-cadence
    `FrequencyClass`. Every fact carries provenance.
  - Repository base (`repositories/base.py`): capability gating (`require()` -> `UnsupportedResult`),
    read-only `select()` through the pool + dialect, provenance stamping, and cache passthrough.
  - Chains repository (`repositories/chains.py`): event-parameter edge linkage (EVENTP_GREEN/RED),
    recursive meta-chain resolution (cycle-guarded), frequency from observed RSPCLOGCHAIN cadence
    (never from names), runtime stats (min/median/mean/p95/max, success rate, bottleneck steps,
    observed overlaps) over a measured window capped at 1 year, and a schedule matrix.
  - FastMCP server (`server.py`): stdio instance with `mask_error_details`, per-call profile
    resolution, registration-time tool-name validation (`^[a-zA-Z][a-zA-Z0-9_]*$`, <=40 chars),
    pagination with `total_count`, and an injectable runtime for tests.
  - Tools: `bw_list_systems`, `bw_system_profile`, `bw_refresh_capabilities`, `bw_refresh_cache`,
    `bw_list_chains`, `bw_get_chain`, `bw_get_chain_runtimes`, `bw_get_schedule_matrix`.
  - `SqlDialect` gained a `group_by` clause; CI customer-metadata allow-list extended with the
    `YYYYMMDD` / `YYYYMMDDHHMMSS` date-format placeholders.

- Providers, texts, and descriptions (build prompt B4), offline- and live-smoke-verified:
  - Domain models: a unified `Provider` spanning classic DSO / advanced DSO / InfoCube /
    MultiProvider / virtual provider / CompositeProvider / InfoObject (`models/providers.py`), and
    a `Description` labelled stored / generated / stored_augmented with a quality flag
    (`models/description.py`).
  - `TextsRepository` reads BW's two text-table shapes — classic `RSD*T` (TXTSH/TXTLG) and
    HANA-object `RSO*T` (DESCRIPTION/QUICK_INFO keyed by COLNAME) — with language fallback.
  - `DescriptionService`: quality assessment (missing / copy_artifact / generic / ok) and
    evidence-based generation, always labelled by origin so a synthesized description is never
    mistaken for a stored one (mission Rule 7).
  - `ProvidersRepository`: universal deep-dive resolving any object by name (auto-detecting the
    type), listing fields, resolving MultiProvider parts (RSDCUBEMULTI), and attaching a
    description; CompositeProvider composition (RSOHCPR.XML_DEF) is flagged deferred, never guessed.
  - `SearchRepository`: cross-object fuzzy search by technical name or description across chains,
    providers, and InfoObjects, object-type-tagged with provenance.
  - Tools: `bw_describe_object`, `bw_search_objects`.
  - Capability catalog extended with the ADSO (RSOADSO*) and CompositeProvider (RSOHCPR*) member
    tables (runtime-confirmed); the SQL dialect skips OBJVERS injection for member tables that carry
    no OBJVERS column (RSOADSOKEYFIELDS, RSOADSOPART, RSOADSO_DTELNM).

- Transformations and routine parser (build prompt B5), offline- and live-smoke-verified:
  - Domain models (`models/transformations.py`): `Transformation` (source/target endpoints,
    field-level rule mappings, routine references), `FieldMapping`, `RoutineCode`, and a heuristic
    `RoutineAnalysis` fixed to `completeness='lower_bound'`.
  - `TransformationsRepository`: header + field-level rule mappings (RSTRANRULE + RSTRANFIELD, with
    PARAMTYPE 1=target / 0=source verified live), routine references (RSTRAN header code-ids +
    RSTRANSTEPROUT), and full ABAP source from RSAABAP (join on CODEID, ordered by LINE_NO).
  - `RoutineParser` (`services/routine_parser.py`): static regex analysis — table dependencies
    (resolving `/BIC/` and `/BI0/` generated tables back to BW objects, advisory), anti-patterns
    (SELECT-in-LOOP, missing FOR ALL ENTRIES guard, hardcoded values, record-set DELETE, DB
    modification, nested loops), and named unresolved calls. Always a lower bound.
  - Tools: `bw_list_transformations`, `bw_get_transformation`, `bw_get_routine_code`,
    `bw_analyze_routine`.
  - Capability catalog gained `RSTRANSTEPROUT` (field-routine code-ids) and `RSTRANSEG`.

- Lineage service (build prompt B6), offline- and live-smoke-verified:
  - Models (`models/lineage.py`): `LineageNode` (incl. the DataSource boundary with
    `upstream_resolved`), `LineageEdge` (declared vs advisory routine edges, update mode),
    `LineageGraph`, `ImpactAnalysis`, `TraceToSource` — aligned with the design.md sketch.
  - `LineageService` (`services/lineage.py`): BFS over declared edges from transformations (RSTRAN)
    and DTPs (RSBKDTP, with update mode), merged, plus advisory routine-derived edges parsed from a
    target's routines (B5 parser). Cycle-guarded, depth- and node-capped.
  - `bw_impact_analysis` additionally runs a reverse RSAABAP scan to find objects whose *routines*
    read the target — dependencies invisible to BW's own where-used lists (advisory).
  - `bw_trace_to_source` walks upstream to the DataSource boundary.
  - Tools: `bw_get_lineage`, `bw_impact_analysis`, `bw_trace_to_source`.

- BEx query subsystem (build prompt B7, mission Section 6), offline- and live-smoke-verified:
  - Models (`models/queries.py`): `Query` (header + element tree + variables), `QueryElement` /
    `QueryElementEdge`, `Restriction`, `QueryVariable`, `QueryLineage` / `FieldLineagePath`,
    `QueryUsage`, `QuerySummary`.
  - `QueriesRepository`: query description via the RSZELTTXT/COMPUID join, recursive element tree
    (RSZELTXREF, cycle-guarded), restrictions (RSZRANGE, variable-reference detection),
    variables (RSZGLOBV) with processing types decoded from DD07T (customer-exit flagged as a
    lineage dead end), provider assignment (RSZCOMPIC), field-level lineage reusing the lineage
    service, and usage from RSZCOMPDIR.LASTUSED (decommission-candidate flag). `bw_list_queries`
    filters RSZCOMPDIR to actual queries (root element DEFTP='REP', not reusable RKFs/structures).
  - Tools: `bw_list_queries`, `bw_get_query`, `bw_get_query_lineage`, `bw_get_query_usage`.

- HANA layer (build prompt B8), offline- and live-smoke-verified:
  - Models (`models/hana.py`): `CalcView`, `BaseTableRef`, `CalcViewLineage`, `HanaCrossing`,
    `HanaCrossingReport`, with `CalcViewType` / `CrossingDirection` literals and provenance on every
    fact.
  - `HanaRepository`: lists `_SYS_BIC` calc views (VIEW_TYPE in CALC/JOIN/OLAP; HIERARCHY excluded)
    with an optional BW-consuming-only filter; resolves calc-view -> direct base-table dependencies
    from `SYS.OBJECT_DEPENDENCIES` (DEPENDENCY_TYPE=1), mapping `/BIC/` and `/BI0/` base tables back
    to BW objects (advisory, reusing the routine parser's resolver); and builds the bidirectional
    BW<->HANA crossing report (calc-view-reads-BW-table and BW-object-reads-calc-view) with exact
    per-direction totals.
  - Tools: `bw_list_calc_views`, `bw_get_calc_view_lineage`, `bw_get_hana_crossings`.
  - Capability catalog HANA entries (OBJECT_DEPENDENCIES, VIEWS, ...) are schema-qualified as `SYS`
    and OBJVERS-free; the layer filters on the `_SYS_BIC` schema value rather than treating it as a
    table schema.

- Risk-scenario analyzers (build prompt B9, mission Section 9), offline- and live-smoke-verified:
  - Models (`models/findings.py`): `Finding` (scenario, severity, affected objects, evidence,
    recommendation, metrics, `unpopulated_reason`) and `ScenarioReport` (severity-ordered findings
    with scope/gap metadata).
  - External-system connector interface (`connectors/`): `ExternalConnector` protocol, a
    `NullConnector`, and a `ConnectorRegistry` that names the connector required when a kind is
    absent; deferred `EccConnector` / `TableauConnector` / `BobjConnector` shapes. The BW HANA
    connection never reaches ECC/Tableau/BOBJ.
  - Latency math (`services/latency.py`): safety-margin computation (negative or sub-30-minute
    margins flagged) and a refresh-frequency comparison for stale-master-data risk.
  - `Analyzers` service (`services/analyzers.py`): the eight scenarios plus a layer-violation
    finder — full-update routine lookups (9.1), deep DSO stacks (9.2), CompositeProvider->DSO (9.3),
    InfoObject<-CompositeProvider (9.4), merged multi-stream DSOs with a field-collision matrix
    (9.5), extractor-enhancement heuristic + ECC-gated logic (9.6), report-schedule risk (9.7) and
    dashboards-on-calc-views (9.8) gated on Tableau/BOBJ connectors. Each is bounded and returns
    `Finding`s with provenance; patterns are detected structurally, never from technical names.
  - Tools: `bw_check_load_latency`, `bw_check_schedule_risk`, `bw_find_layer_violations`, and a
    general `bw_review_scenario(system, scenario)` dispatcher.
  - Prompts (`prompts/workflows.py`): `analyze_impact`, `troubleshoot_missing_data`,
    `document_dataflow`, `review_scenario`, `onboard_analyst`, `pre_change_checklist`.

- Documentation generator (build prompt B10, mission Section 8), offline- and live-smoke-verified:
  - `DocGenerator` (`services/docgen.py`) composes every repository and service to render the full
    markdown knowledge base: `index.md`, `01-inventory/` through `08-scenarios/`, and a non-empty
    `99-gaps-and-risks.md`. Lineage pages carry Mermaid flow diagrams plus graph JSON; every page
    has backlinks and a source-table citation footer; generated descriptions render with a visible
    marker; the gaps register aggregates unsupported/truncated/connector-gated/advisory items.
  - Output-location safety refuses to write into the git-tracked repo tree (writes outside the repo
    or under a git-ignored dir, defaulting to `output/docs/<system>`); generation is bounded per
    section with truncation recorded.
  - Tool: `bw_generate_docs(system, output_dir, limit)` returning a manifest (files, page count,
    gaps count, truncation flag).

- Hardening and publish preparation (build prompt B11):
  - Reusable, cross-platform `scripts/customer_metadata_scan.py` (working tree + full git history)
    is now the single source of truth for the customer-metadata leak check; CI runs it instead of
    inline bash (no drift).
  - README finalized: full 29-tool catalogue with parameters, the 6 prompts, supported-release
    matrix (BW 7.50 validated live), quickstart, MCP client config, and the security /
    no-customer-metadata model. Resources are documented as specified-but-not-yet-implemented.
  - Regression tests: the profile password is never exposed in repr/str/model_dump/json (Rule 5);
    all six workflow prompts render with the expected tool references and threaded arguments.
  - Startup `.env` loading: when launched by an MCP client, the server loads a git-ignored `.env`
    (`BW_DOTENV_PATH`, else beside `BW_PROFILES_PATH`, else `./.env`) into the environment without
    overriding existing variables, so a client config only needs `BW_PROFILES_PATH` and secrets stay
    out of the MCP config. README updated with the run-from-source registration form.

### Notes
- Live capability discovery (B2) is a hard gate before any repository or tool code.
- Scenario 9.6 reclassified as an ECC-connector capability (source lives in ECC, not BW).
- Query code values (DEFTP / LAYTP / VPROCTP / VARTYP / RSZTYPEFLAG) were decoded from the data
  dictionary (DD07T), not assumed. Customer-exit variables are a lineage dead end (values resolve
  in ABAP at runtime, mission Known Limitation 4). Query field lineage is object-level; per-field
  transformation-rule detail is available via the transformation tools.
- Lineage node ids are object technical names (unique enough for lineage); routine-derived edges are
  advisory (heuristic lower bound). The DataSource is a boundary node (`upstream_resolved=False`).
- Routine analysis is a heuristic lower bound (mission Known Limitation 3): dynamic SQL,
  function-module and class-method calls are not followed; `/BIC/`-to-object resolution is advisory.
- Chain frequency is classified from observed run cadence (RSPCLOGCHAIN), not from chain names or
  scheduled periodicity; TBTCO periodicity corroboration is deferred.
- CompositeProvider part-provider composition is stored as XML in RSOHCPR.XML_DEF; parsing it is
  deferred to the lineage build (B6). InfoObject attributes (RSDBCHATR/RSDATRNAV) are not yet
  resolved. Object descriptions are assessed on the long text (TXTLG) when present, since short
  texts are deliberately brief.
- HANA calc-view analysis uses direct dependencies only (`SYS.OBJECT_DEPENDENCIES.DEPENDENCY_TYPE=1`);
  transitive dependencies (type 2) are ~8.2M rows and are intentionally excluded. Calc-view ->
  CompositeProvider precise mapping is deferred: calc views are exposed by `_SYS_BIC` view name with
  their direct base tables resolved to BW objects (advisory). Calc-view XML-definition internals
  (`_SYS_REPO.ACTIVE_OBJECT`: joins, calculated columns, input parameters) are not parsed; the layer
  relies on the SYS catalog and dependency graph, which suffice for lineage and crossings.
- Risk analyzers detect the mission's scenarios structurally (not from "ADM"/"EDW" naming). A general
  `bw_review_scenario` dispatcher tool was added beyond mission Section 4's enumerated risk tools so
  scenarios 9.2/9.5/9.6/9.8 (which have no dedicated tool) are invocable and back the
  `review_scenario` prompt. Scenarios 9.6 (ECC), 9.7 and 9.8 (Tableau/BOBJ) are connector-gated:
  their external-system side reports `connector_required` until a connector is configured (deferred);
  the BW-derivable side (9.6 Z*/Y* field heuristic, 9.7 feeding-chain p95, 9.8 BW-consuming calc-view
  count) is populated. Object -> loading-chain frequency mapping is not derivable on this landscape
  (no RSPCVARIANT DTP_LOAD linkage), so 9.1/9.7 mark that as a documented gap rather than guessing.
