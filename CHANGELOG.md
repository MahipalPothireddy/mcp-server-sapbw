# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

### Notes
- Live capability discovery (B2) is a hard gate before any repository or tool code.
- Scenario 9.6 reclassified as an ECC-connector capability (source lives in ECC, not BW).
- Chain frequency is classified from observed run cadence (RSPCLOGCHAIN), not from chain names or
  scheduled periodicity; TBTCO periodicity corroboration is deferred.
- CompositeProvider part-provider composition is stored as XML in RSOHCPR.XML_DEF; parsing it is
  deferred to the lineage build (B6). InfoObject attributes (RSDBCHATR/RSDATRNAV) are not yet
  resolved. Object descriptions are assessed on the long text (TXTLG) when present, since short
  texts are deliberately brief.
