# Design Document — mcp-server-sapbw

## Overview

`mcp-server-sapbw` is a read-only MCP server built on FastMCP. It exposes SAP BW-on-HANA metadata
as tools, resources, and prompts, and is portable across BW 7.4, 7.5, and BW/4HANA because every
data access is gated by a runtime **capability resolver** rather than by hardcoded assumptions
about which tables exist.

The design follows four strictly separated layers so a release quirk never leaks into tool logic:

```
MCP layer          tools / resources / prompts  (FastMCP decorators)          src/mcp_server_sapbw/server.py, prompts/
  ↓ (calls services; never touches SQL)
Service layer      lineage graph · routine parser · latency · descriptions · docgen · analyzers   services/
  ↓ (calls repositories; holds domain logic, no SQL strings)
Repository layer   chains · providers · transformations · queries · hana · texts (one per domain) repositories/
  ↓ (builds SQL from capability record; the only layer that emits SQL)
Core layer         profile manager · connection pool · capability resolver · SQL dialect · cache  core/
```

**Dependency rule:** a layer may call only the layer directly beneath it. The MCP layer never
builds SQL; the repository layer never registers tools. This is what keeps the server portable and
keeps read-only enforcement in exactly one place.

This document details the five mechanisms called out in the seed prompt — the capability-resolution
mechanism, the lineage graph model, the routine parser, caching, and the security model — plus the
data models and error handling that bind them together. It maps to the requirements in
`requirements.md` and to the build order in `tasks.md`.

---

## Architecture and module layout

```
src/mcp_server_sapbw/
├── server.py                 # FastMCP instance; registers tools/resources/prompts
├── core/
│   ├── profiles.py           # ProfileManager: load + ${VAR} interpolation
│   ├── connection.py         # ReadOnlyConnectionPool, fail-closed grant check, secret scrubbing
│   ├── capabilities.py       # CapabilityResolver + CapabilityRecord
│   ├── dialect.py            # SQL builder; parameterized; SELECT-only guard
│   └── cache.py              # SqliteCache (per-profile), TTL policy
├── repositories/
│   ├── base.py               # Repository base: capability gating, provenance stamping
│   ├── chains.py             # RSPC*, TBTC* → chain structure + runtimes
│   ├── providers.py          # RSDODSO*, RSOADSO*, RSDCUBE*, RSOHCPR*, RSDIOBJ*
│   ├── transformations.py    # RSTRAN*, RSAABAP, RSBK*, RSDS*, RSLDP*
│   ├── queries.py            # RSZ*, RSRREPDIR, RSDDSTAT*
│   ├── hana.py               # SYS.OBJECT_DEPENDENCIES, _SYS_REPO/HDI, SYS.VIEWS
│   └── texts.py              # DD02T/DD04T + object text tables; language fallback
├── services/
│   ├── lineage.py            # LineageGraph builder + traversal
│   ├── routine_parser.py     # ABAP SELECT/anti-pattern parser (heuristic); consumes RSAABAP or a bundle
│   ├── source_bundle.py      # offline ABAP source-file ingestion (extension point, deferred)
│   ├── descriptions.py       # read → assess → generate → label
│   ├── latency.py            # scenario 9.1 / 9.7 timing math
│   ├── analyzers.py          # the eight risk analyzers (9.1–9.8) + layer violations
│   └── docgen.py             # markdown knowledge base renderer
├── connectors/                # pluggable external-system connectors (separate from BW core)
│   ├── base.py               # ExternalConnector interface + NullConnector
│   ├── ecc.py                # ECC connector — SQL Server via pyodbc; inventory-only (9.6); deferred
│   └── external_bi.py        # Tableau / BOBJ connector (9.7 / 9.8); deferred
├── models/
│   ├── provenance.py         # Provenance, UnsupportedResult
│   ├── description.py        # Description
│   ├── capability.py         # CapabilityRecord, TableStatus
│   ├── graph.py              # LineageNode, LineageEdge, LineageGraph
│   └── findings.py           # Finding, Severity
└── prompts/                  # analyst-workflow prompt templates
```

Technology choices (mission Section 10): **FastMCP** (server), **hdbcli** (HANA driver),
**pydantic** (models/validation), **pyyaml** (profiles). `pyrfc` is an optional extra for RFC-based
query resolution. FastMCP 3.x went GA in Feb 2026 and changed the auth model from 2.x, so the exact
version is pinned in `pyproject.toml` and its decorator/auth API is verified against the pinned
docs before any server code is written (task B0).

---

## Core data models

All models are pydantic. Two are cross-cutting and appear on virtually every response.

### Provenance (Requirement 5)

```python
class Provenance(BaseModel):
    source_table: str  # e.g. "RSTRAN"
    source_key: dict[str, str]  # e.g. {"TRANID": "0ABC123", "OBJVERS": "A"}


# Any record aggregated from multiple rows carries list[Provenance].
```

Every model that represents a fact embeds `provenance: Provenance | list[Provenance]`. Repositories
stamp provenance at read time; services propagate it; the MCP layer never strips it.

### UnsupportedResult (Requirements 3, 7)

```python
class UnsupportedResult(BaseModel):
    status: Literal["unsupported_on_release"] = "unsupported_on_release"
    missing: list[str]  # table/column names confirmed absent
    release: str
    alternative: str | None = None  # correct object for this release, if known
    detail: str
```

Repository methods return this (never raise a generic error and never guess) when the capability
record shows a required object is absent.

### CapabilityRecord (Requirement 3)

```python
class TableStatus(BaseModel):
    logical_name: str  # e.g. "adso_header"
    resolved_name: str | None  # actual table found, or None
    tier: Literal["existence", "discover"]
    present: bool
    schema: str | None  # ABAP schema or SYS/_SYS_REPO
    row_estimate: int | None


class CapabilityRecord(BaseModel):
    system: str
    bw_release: str  # e.g. "7.50", "BW4HANA 2.0"
    abap_schema: str  # resolved, never hardcoded
    object_models: dict[
        str, bool
    ]  # classic_dso, adso, composite_provider, multiprovider, open_ods_view
    hana_repo_style: Literal["sys_repo", "hdi", "none"]
    processlog_retention_days: int  # measured from earliest RSPCPROCESSLOG entry
    tables: dict[str, TableStatus]  # keyed by logical name
    discovered_at: datetime
    ttl_seconds: int = 86400
```

### Description (Requirement 13 / mission Section 7)

```python
class Description(BaseModel):
    description_short: str
    description_long: str
    origin: Literal["stored", "generated", "stored_augmented"]
    quality_flag: Literal["ok", "missing", "generic", "copy_artifact"]
    evidence: list[str]  # ["RSDODSOT", "RSTRAN:0ABC123", "RSAABAP:0XYZ789"]
```

### Lineage graph models and Findings — defined in their sections below.

---

## Core layer

### Profile manager (Requirement 1)

`ProfileManager` loads `profiles.yaml` from `BW_PROFILES_PATH`, parses it with pyyaml, and performs
`${VAR}` interpolation against the process environment. Rules:

- Any value not matching `^\$\{[A-Z0-9_]+\}$` for a secret field (`password`, and `user`/`host` when
  templated) is rejected — inline literal secrets are a load-time error.
- `abap_schema: auto` is preserved as a sentinel and resolved later by the capability resolver.
- Profiles are validated into a `Profile` pydantic model; unknown profile name → structured error
  listing configured names (no secrets in the message).

### Connection pool and read-only enforcement (Requirement 2 — security model)

`ReadOnlyConnectionPool` wraps `hdbcli` connections, one pool per profile. It is the single choke
point for the security model:

1. **SELECT-only cursor.** All execution goes through `execute_select(sql, params)`, which asserts
   the statement's first significant token is `SELECT` (or `WITH … SELECT`) and rejects anything
   else. There is no `execute()` that accepts arbitrary DML/DDL anywhere in the codebase.
2. **Fail-closed grant check.** On first connect, when `read_only_user: true`, the pool queries the
   user's effective privileges (e.g. `GRANTED_PRIVILEGES`/role grants) and refuses the connection if
   any `INSERT/UPDATE/DELETE/EXECUTE/CREATE/DROP/ALTER` grant is present on reachable schemas. A
   detected write grant raises a `ReadOnlyViolation` and the pool never yields a usable connection.
3. **Session hardening.** Connections are opened with `encrypt` per profile; autocommit is
   irrelevant because only SELECT is issued, but transaction isolation is set read-committed and the
   session is flagged read-only where the driver supports it.
4. **Secret scrubbing.** A `scrub()` helper is applied to every exception surfaced from this layer:
   it removes host, port, user, password, and full connection strings, replacing them with
   `<redacted>`. `scrub()` is also installed on the logging formatter so no log line can leak them.

The read-only guarantee lives here, not in higher layers, satisfying "enforce it in the connection
layer, not by convention."

### SQL dialect (Requirements 2, 6)

`dialect.py` builds parameterized SELECT statements. Responsibilities:

- Always bind values as parameters (never string-interpolate) to avoid injection.
- Auto-inject `OBJVERS = 'A'` on any table matching `RSD*`, `RSO*`, `RSTRAN*`, `RSZ*` unless the
  caller passes `compare_versions=True`, in which case `OBJVERS` becomes a selected column.
- Qualify ABAP tables with the resolved schema from the capability record; qualify HANA catalog
  objects with `SYS`/`_SYS_REPO`/`_SYS_BIC` as resolved.
- Provide `paginate(sql, limit, offset)` and a matching `count(sql)` so list endpoints can return
  `total_count` cheaply.

### Capability resolution mechanism (Requirement 3 — focus area)

This is built first and run first; it is the mechanism that makes the server portable.

**Trigger.** On first use of a profile (or on `bw_refresh_capabilities`), `CapabilityResolver.resolve(profile)`
runs once and the result is cached (in-memory for the session and persisted in the profile's SQLite
cache) with `ttl_seconds` (default 86400).

**Step 1 — resolve ABAP schema.** If `abap_schema == "auto"`, find the schema that owns BW
dictionary tables:

```sql
SELECT SCHEMA_NAME FROM SYS.TABLES WHERE TABLE_NAME = 'RSTRAN';
```

The schema owning core `RS*` tables is the ABAP schema; it is never assumed to be `SAPABAP1`.

**Step 2 — existence tier.** For every EXISTENCE-tier table in the Appendix A matrix, confirm
presence in one batched probe:

```sql
SELECT TABNAME FROM <ABAP_SCHEMA>.DD02L WHERE TABNAME IN (...);   -- ABAP dictionary tables
SELECT TABLE_NAME  FROM SYS.TABLES  WHERE SCHEMA_NAME = 'SYS' AND TABLE_NAME IN (...);
SELECT VIEW_NAME   FROM SYS.VIEWS   WHERE VIEW_NAME IN (...);      -- HANA views/monitoring
```

**Step 3 — discover tier.** For every DISCOVER-tier entry, discover the actual name by pattern
instead of assuming it:

```sql
SELECT TABNAME FROM <ABAP_SCHEMA>.DD02L WHERE TABNAME LIKE 'RSOADSO%';   -- Advanced DSO family
SELECT TABNAME FROM <ABAP_SCHEMA>.DD02L WHERE TABNAME LIKE 'RSOHCPR%';   -- CompositeProvider family
SELECT TABNAME FROM <ABAP_SCHEMA>.DD02L WHERE TABNAME LIKE 'RSDDSTAT%';  -- BW statistics family
SELECT TABNAME FROM <ABAP_SCHEMA>.DD02L WHERE TABNAME LIKE 'RSTRAN%';    -- transformation text table
```

The resolver classifies discovered members into logical roles (header / texts / part-provider /
fields) using column signatures from `DD03L`, and records both the resolved name and how it was
derived. HANA calc-view definition location is decided here too: probe `_SYS_REPO.ACTIVE_OBJECT`;
if absent, mark `hana_repo_style = "hdi"` and target the HDI-container equivalent.

**Step 4 — populate & measure.** For present tables, capture a row estimate; determine which
object-model variants are actually populated (not merely present); and measure the
`RSPCPROCESSLOG` retention window from its earliest entry (Known Limitation 5) so runtime tools
report the real window.

**Step 5 — gate.** `Repository.require(logical_name)` consults the record before building SQL. If
the table is absent, the method returns `UnsupportedResult` naming the missing table and the correct
alternative where known. No repository ever issues SQL against an unconfirmed object.

**Discover-tier set (never assumed from memory):** `RSOADSO*`, `RSOHCPR*`, `RSDDSTAT*`, the
`RSTRAN%` transformation text table, the calc-view definition location, and `ROOSOURCE`/`ROOSFIELD`.
See `requirements.md` Appendix A for the full matrix.

### Caching (Requirement 4 — focus area)

`SqliteCache` is one SQLite file per profile, living under a git-ignored `cache/` path. Design:

- **Key.** `(system, object_type, object_id, extraction_kind)` plus a stored `extracted_at`
  timestamp and the capability `discovered_at` fingerprint (so a capability refresh invalidates
  dependent extracts).
- **Value.** The serialized pydantic model(s) for the extract (e.g. a transformation with mappings,
  or a query element tree). Provenance is stored with the value.
- **TTL policy — two tiers:**
  - *Structural metadata* (definitions, mappings, routine source, element trees): long TTL, matches
    the capability TTL, because it changes slowly. This is the expensive `RSTRAN`/`RSZ*`/`RSAABAP`
    content that the mission calls out as costly on large systems.
  - *Runtime statistics* (chain/query runtimes, request durations, request status): **hard cap of
    one hour**, per Non-Negotiable behavior — never cached beyond an hour so currency questions stay
    honest.
- **Invalidation.** `bw_refresh_cache(system, scope)` deletes rows matching a scope
  (`all | chains | providers | transformations | queries | hana | <object_id>`).
- **Cold-start ordering.** Because first extraction of `RSAABAP`/`RSZ*` on a large PRD system is
  expensive (Known Limitation 7), the cache is built before the services that need it (task order),
  the first full extraction is recommended against QA, and all extracts paginate.

---

## Repository layer

One module per domain. A `Repository` base class provides: `require(logical_name)` capability
gating, `select(...)` via the read-only pool + dialect, and `stamp(row, source_table, key_cols)`
which attaches `Provenance`. Repositories return pydantic models, never raw rows, and never contain
tool registration or cross-domain orchestration (that is the service layer's job).

Key per-domain notes:

- **chains.py** — reads the `RSPCCHAIN` edge list and resolves meta-chains recursively; classifies
  frequency strictly from `TBTCO`/`TBTCP`/`TBTCS` periodicity, never from names; computes runtime
  stats (min/median/mean/p95/max, success rate, critical path) from `RSPCLOGCHAIN`/`RSPCPROCESSLOG`
  over the measured retention window.
- **providers.py** — universal provider reader; uses the capability record to pick the right family
  (classic DSO / ADSO / cube / MultiProvider by cube type / CompositeProvider) and returns a common
  provider model regardless of type.
- **transformations.py** — header + field mappings + rule types; full ABAP retrieval by joining
  `RSAABAP` on code ID ordered by `LINE_NO`; DTP `UPDMODE` from `RSBKDTP`; request history from
  `RSBKREQUEST`; DataSource fields from `RSDS`/`RSDSSEGFD`.
- **queries.py** — the BEx tables; implements the `RSZELTTXT`-on-`COMPUID` description join and the
  recursive `RSZELTXREF` walk; usage from the discovered `RSDDSTAT*` family.
- **hana.py** — `SYS.OBJECT_DEPENDENCIES`, calc-view definition from the resolved repo/HDI location,
  `/BIC/`→BW resolution via `DD02L`.
- **texts.py** — object text tables with logon-language + English fallback; discovers the
  transformation text table by pattern.

---

## Service layer

### Lineage graph model (Requirement 12 — focus area)

The lineage service builds a single directed multigraph over BW/HANA objects and traverses it for
`bw_get_lineage`, `bw_impact_analysis`, and `bw_trace_to_source`.

**Scope boundary (intentional sequencing).** The current build resolves lineage from the
**DataSource down to reports** — not from ECC down to reports. That is a sequencing decision, not a
permanent boundary, so the graph model carries the extension points *now* and an ECC connector (or
an offline source bundle) can later attach parent nodes **without changing any node or edge type**.

**Node.**

```python
class SourceSystemRef(BaseModel):
    """Slot on a boundary (DataSource) node for source-system detail.

    Populated by an external connector (e.g. ECC) when the upstream is resolved; None until then.
    Identifiers only — never credentials.
    """
    system_type: Literal["ecc", "other", "unknown"] = "unknown"
    system_id: str | None = None       # logical system / SID, filled by a connector
    object_name: str | None = None     # the extract structure / source object, filled by a connector


class UnresolvedRef(BaseModel):
    """An ABAP-layer dependency the routine parser could not follow (named, never dropped)."""
    call_kind: Literal["class_method", "function_module", "form", "dynamic", "unknown"]
    object_name: str                   # the class / FM / method / subroutine as written in source
    detail: str | None = None          # e.g. method name or raw call snippet (no secrets)


class LineageNode(BaseModel):
    id: str  # canonical "TYPE:NAME" e.g. "dso:<technical_name>"
    object_type: Literal[
        "datasource",
        "dso",
        "adso",
        "cube",
        "multiprovider",
        "compositeprovider",
        "infoobject",
        "transformation",
        "dtp",
        "calcview",
        "query",
        "report",
        "source_object",          # a node in a source system (e.g. ECC extract structure)
        "unresolved_dependency",  # a custom class/FM/method the parser could not resolve
    ]
    name: str
    # Boundary / extension fields (default so existing construction is unaffected):
    upstream_resolved: bool = True     # False on a DataSource with no source-system parents yet
    source_system: SourceSystemRef | None = None  # populated on DataSource nodes by a connector
    unresolved_ref: UnresolvedRef | None = None    # populated on unresolved_dependency nodes
    provenance: Provenance | list[Provenance]
```

The **DataSource is an explicit boundary node**: in the BW-only build it is created with
`upstream_resolved = False` (there *is* an upstream — the source system — we simply have not resolved
it), and `source_system` left `None`. An ECC connector, or the offline source-bundle path, later adds
`source_object` parent nodes joined by `source_extract` edges, fills `source_system`, and flips
`upstream_resolved = True` — reusing existing types only.

**Edge.**

```python
class LineageEdge(BaseModel):
    src: str  # producer node id
    dst: str  # consumer node id  (direction = data flow, src → dst)
    kind: Literal[
        "transformation",
        "dtp",
        "multiprovider_part",
        "composite_part",
        "calcview_base",
        "query_provider",
        "routine_lookup",
        "source_extract",   # source_object -> datasource (attached by an ECC connector / bundle)
        "unresolved_call",  # transformation/routine -> unresolved_dependency
    ]
    derivation: Literal["declared", "routine"]  # "routine" edges are advisory
    update_mode: Literal["F", "D", None] = None  # from RSBKDTP for dtp edges
    chain_id: str | None = None  # executing chain
    chain_frequency: str | None = None  # classified frequency
    confidence: Literal["exact", "advisory"]
    provenance: Provenance | list[Provenance]
    note: str | None = None  # e.g. "routine lookup; heuristic lower bound"
```

**Construction.** Edges come from two sources merged into one graph:

1. **Declared edges** — from `RSTRAN` (source/target), `RSBKDTP` (with `UPDMODE`), `RSDCUBEMULTI`
   (MultiProvider parts), the CompositeProvider part-provider table, `SYS.OBJECT_DEPENDENCIES`
   (calc-view base tables), and `RSZCOMPIC` (query→provider). `confidence = "exact"`.
2. **Advisory edges** — from the routine parser (below): for each routine, every resolved `/BIC//BI0/`
   table read becomes a `routine_lookup` edge into the routine's transformation target, with
   `derivation = "routine"`, `confidence = "advisory"`, and the routine ID in `note`. These are the
   dependencies invisible to BW's own where-used list.
3. **Boundary + unresolved nodes** — every DataSource reached during construction is created as a
   boundary node (`upstream_resolved = False`). Every custom class / function module / method the
   routine parser cannot follow becomes an `unresolved_dependency` node (named, carrying an
   `UnresolvedRef`) joined by an `unresolved_call` edge from the routine's transformation. These
   nodes are never dropped, so the ABAP-layer gaps are **countable and visible** (surfaced in the
   generated docs and the gaps register). They also carry the called object name, so a later ECC
   connector or source bundle can match and resolve them.

Each edge is annotated with the executing chain's ID and classified frequency by joining the
transformation/DTP to the chains that run it.

**Traversal.**

- `get_lineage(object, direction, depth)` — BFS upstream (follow edges into the node), downstream
  (follow edges out), or both, to `depth` hops; returns `{nodes, edges}` JSON. Cycles are detected
  and broken with a `visited` set; cycle points are annotated rather than silently dropped.
- `impact_analysis(object)` — full downstream closure including `routine_lookup`, `calcview_base`,
  `query_provider`, and report edges; the response separates `exact` from `advisory` findings so a
  reviewer sees the routine-embedded lookups explicitly (acceptance criterion, Section 11).
- `trace_to_source(object)` — upstream closure terminating at `datasource` nodes, returned hop by
  hop.

Every advisory edge carries the "heuristic lower bound" caveat in its payload, not just in docs
(Known Limitation 3).

### Routine parser (Requirement 11 — focus area)

`routine_parser.py` turns ABAP routine source (assembled from `RSAABAP`) into a structured, honest
dependency and quality report. It is deliberately heuristic and **advertises that it is a lower
bound**.

**Input.** Full source for a code ID (start/end/expert/field routine), lines ordered by `LINE_NO`.

**Pipeline.**

1. **Normalize.** Strip comments (capturing the leading comment block separately as candidate
   documentation), uppercase keywords, join statement continuations to `.`-terminated statements.
2. **Extract table reads.** Regex/statement scan for `SELECT … FROM <table>`, `SELECT SINGLE`,
   `INTO TABLE … FROM`, and `FOR ALL ENTRIES` targets. Resolve each `<table>`:
   - `/BIC/A<dso>00`, `/BIC/<...>` and `/BI0/<...>` → resolve to the owning BW object via `DD02L`
     naming so the dependency is reported as a BW object, not a raw table.
   - Standard tables (e.g. `T001`, `MARA`) are reported as-is.
3. **Detect anti-patterns** (Requirement 11.2):
   - `SELECT` inside `LOOP … ENDLOOP` (nested-read performance smell).
   - `FOR ALL ENTRIES` without a preceding emptiness guard on the driver table.
   - Hardcoded literals in `WHERE` (client, company code, date constants).
   - Record-set-altering logic: `DELETE <itab>`, `DELETE ADJACENT DUPLICATES`, `REFRESH`,
     row-count-changing `MODIFY`.
4. **Complexity signals.** Statement count, nesting depth, number of distinct tables, count of
   dynamic constructs seen.
5. **Unresolved dependencies (named, never dropped).** For every construct the parser cannot follow
   — dynamic SQL (`(lv_tabname)`), `CALL FUNCTION`, `CALL METHOD` / `->` / `=>`, `PERFORM` of an
   external form — emit a structured `UnresolvedRef` naming the called object, and set
   `completeness = "lower_bound"`. The lineage service turns each `UnresolvedRef` into an
   `unresolved_dependency` node (see above), so these ABAP-layer gaps are countable and visible
   rather than silently dropped.

**Output.**

```python
class RoutineAnalysis(BaseModel):
    code_id: str
    routine_type: Literal["start", "end", "expert", "field"]
    leading_comment: str | None
    table_dependencies: list[TableDependency]  # resolved to BW objects where possible
    anti_patterns: list[AntiPattern]  # type, line, snippet, severity
    complexity: ComplexitySignals
    completeness: Literal["lower_bound"]  # always a lower bound
    caveats: list[str]  # freeform notes
    unresolved: list[UnresolvedRef]  # named class/FM/method/dynamic calls the parser could not follow
    provenance: Provenance  # RSAABAP:<code_id>
```

The lineage service consumes `table_dependencies` to synthesize advisory `routine_lookup` edges and
`unresolved` to create `unresolved_dependency` nodes; the description service consumes
`leading_comment` + a behavior summary for routine descriptions; the 9.1 latency analyzer consumes
the resolved lookup targets.

**Source-bundle resolution (extension point, deferred).** The same parser is designed to consume
ABAP source from a **local directory of exported files** (a "source bundle"), not just `RSAABAP`.
Because `unresolved_dependency` nodes carry the called object name, a bundle can be matched to them
offline — with no live connection and no RFC — resolving them from both sides: BW routines that call
custom classes/FMs, and (once exported) ECC extractor-exit source. This is an extension point in the
design; the implementation is deferred.

### Description subsystem (Requirement 13 / Section 7)

Four steps, matching the mission: **read** stored text (`texts.py`, logon language + English
fallback, active version), **assess** quality (empty / equals technical name / copy artifact
`Copy of …`|`ZZ_TEST`|`tmp` / < 4 words / wrong language), **generate** when missing or low-value
from evidence the server already holds (sources, key fields, semantic key, update mode, load
frequency, routine summary, consumers), and **label** provenance (`origin`, `quality_flag`,
`evidence`). Generated text is never written back to BW and always renders with a visible marker in
docs.

### Latency service (Requirements 16, 22)

Shared timing math for scenarios 9.1 and 9.7: joins a consumer's start time (chain schedule from
`TBTC*`, or report schedule from a connector) against the p95 completion of the feeding chain, and
computes a **safety margin**. 9.1 additionally walks routine lookups to the load frequency of each
looked-up DSO to build the latency-contract table; 9.7 flags negative or sub-30-minute margins.

### Risk analyzers (Requirements 16–24)

`analyzers.py` implements the eight scenarios plus the layer-violation finder. Each returns a common
`Finding`:

```python
class Finding(BaseModel):
    scenario: str  # "9.1", "9.7", "layer_violation", ...
    severity: Literal["info", "low", "medium", "high", "critical"]
    title: str
    affected_objects: list[str]
    evidence: list[Provenance]
    recommendation: str
    unpopulated_reason: str | None = None  # set when an external connector is required but absent
```

Scenarios **9.6 (ECC), 9.7, and 9.8** depend on metadata that lives outside BW-on-HANA and is
unreachable over the BW HANA connection: 9.6 needs the ECC extractor-enhancement source
(CMOD/BAdI, `ROOSOURCE`/`ROOSFIELD`) via an ECC connector; 9.7/9.8 need Tableau/BOBJ metadata. When
the required connector is not configured, the analyzer still emits its template with
`unpopulated_reason` naming the connector (Requirement 30). Only the parts derivable from BW itself
are populated in that case.

**9.6 from BW alone (heuristic).** Even with no ECC connector, the analyzer flags *likely* extractor
enhancements by comparing a DataSource's `RSDSSEGFD` fields against its standard extract-structure
field set and detecting customer-namespace fields (`Z*` / `Y*` / `ZZ*`). The result is labelled
`heuristic`: it establishes *that* an enhancement probably exists, never *what it does*.

### External-system connectors & source bundle (deferred extension points)

These are designed now and built later; the point is that the graph and parser already accommodate
them (boundary nodes, `unresolved_dependency` nodes, a bundle-consuming parser).

- **ECC connector — separate driver, reduced scope.** ECC runs on **SQL Server, not HANA**, so this
  is a distinct driver: `pyodbc` with *ODBC Driver 18 for SQL Server*, schema typically `SAP<SID>`.
  Scope is the enhancement **inventory only** — `ROOSOURCE`, `ROOSFIELD`, `DD02L`/`DD03L` append
  structures, `MODSAP`/`MODACT`, `SXS_ATTR`/`SXC_EXIT`, `ENHHEADER`/`ENHOBJ` — with every name
  validated at connect time exactly as the BW capability resolver does. It **cannot read ABAP
  source**: `REPOSRC.DATA` is compressed on every platform. It attaches `source_object` parents to
  DataSource boundary nodes via `source_extract` edges.
- **Source-bundle ingestion — the higher-value path.** A local directory of exported ABAP source
  files (the `ZXRSAU0x` extractor user-exit include family, numbers 01–04; plus custom BW classes
  and function modules), parsed **offline** by the existing routine parser — no live connection, no
  RFC. Because `unresolved_dependency` nodes carry the called object name, a bundle resolves them
  from both sides: BW routines calling custom classes/FMs, and ECC extractor-exit source once
  exported. Modeled as a filesystem source the parser consumes.

### Documentation generator (Requirement 25 / Section 8)

`docgen.py` renders the Section 8 directory tree to a git-ignored output location: inventory,
per-chain, lineage (Mermaid + graph JSON), per-provider, per-transformation (with full routine
source and parsed deps), per-query (definition + element tree + field lineage + usage), HANA
crossings, the eight scenarios, and a **non-empty** `99-gaps-and-risks.md`. Every page carries
backlinks and source-table citations; generated descriptions render with their visible marker.

---

## MCP surface

`server.py` instantiates FastMCP and registers the 28 tools, 7 resource templates, and 6 prompts
from mission Section 4. Conventions enforced here:

- **Naming.** Every tool name matches `^[a-zA-Z][a-zA-Z0-9_]*$`, no hyphens/dots, ≤40 chars (stays
  under 64 including Kiro's server prefix). A registration-time assertion rejects any violating name.
- **`system` parameter.** Every tool/resource/prompt takes `system: str`; the server resolves the
  profile and (lazily) the capability record per call, staying stateless.
- **Pagination.** List tools accept `limit`/`offset` and return `total_count`; oversized results
  return a summary + a `bw://…` resource URI instead of dumping rows.
- **Thin layer.** Tool functions validate inputs, call one service/repository method, and return the
  pydantic model. No SQL, no domain logic here.
- **Resources** mirror the tool data as URI-addressable read-only context and carry identical
  provenance. **Prompts** compose read-only tools into analyst workflows and never call a mutating
  path (there are none).

Auto-approve (per mission Kiro registration) is limited to cheap read-only tools: `bw_list_systems`,
`bw_system_profile`, `bw_search_objects`.

---

## Security model (consolidated — Requirement 2, Rules 1/4/5)

Defense in depth, with the hard guarantee at the lowest layer:

1. **No write code path.** The only DB entry point is `execute_select`; there is no method that
   accepts INSERT/UPDATE/DELETE/DDL/activation/chain-trigger/RFC-write. Read-only is a structural
   property, not a policy.
2. **Fail-closed grant assertion.** `read_only_user: true` connections are refused if the user holds
   any write grant.
3. **Secret hygiene.** Credentials come only from env-var interpolation; `scrub()` strips host/port/
   user/password/connection strings from every exception and log record; no secret is ever written
   to a response, fixture, or generated artifact.
4. **No customer data in the repo.** `.gitignore` excludes `.env`, `profiles.yaml`, `output/`,
   `extracts/`, `cache/`, `*.sqlite`, `.kiro/settings/mcp.json`. A CI job fails the build if any
   commit outside `tests/fixtures/` contains customer object patterns (`/BIC/`, `/BI0/`, real chain/
   DSO prefixes). Fixtures use synthetic names only.
5. **Generated ≠ stored.** Synthesized descriptions are labelled `origin: generated` and never
   written back to BW.
6. **Provenance everywhere.** Every fact is traceable to a real row; untraceable facts are dropped
   and recorded as gaps rather than emitted.

---

## Error handling

- **Unsupported release** → repositories return `UnsupportedResult` (structured), never a guess and
  never a stack trace. The MCP layer passes it through as a normal typed result.
- **Missing profile / bad config** → structured error listing valid profile names, no secrets.
- **Write grant detected** → `ReadOnlyViolation`, connection refused, scrubbed message.
- **Connectivity/driver errors** → scrubbed of host/credentials before surfacing.
- **Partial data / dead ends** (customer-exit variables, dynamic SQL in routines) → returned as
  explicit `advisory`/`dead_end` markers in the payload, plus a gap entry for the docs register.
- **Oversized results** → summary + resource URI, never an unbounded dump.

---

## Testing strategy (Requirement 29)

- **Offline only.** No live BW system in tests or CI. `hdbcli` connections are mocked; fixtures are
  synthetic anonymized metadata rows under `tests/fixtures/` using invented names (no `/BIC/`,
  `/BI0/`, or real prefixes).
- **Layer coverage.** Unit tests for: capability resolution across simulated 7.4 / 7.5 / BW4HANA
  fixture sets (existence + discover tiers, ADSO/CP present vs. absent); read-only pool (SELECT-only
  guard, fail-closed grant check, secret scrubbing); SQL dialect (`OBJVERS='A'` injection,
  parameterization, pagination); routine parser (each anti-pattern, `/BIC/` resolution, lower-bound
  caveats); lineage traversal (declared + advisory edges, cycle handling); description subsystem
  (each `quality_flag`, each `origin`); each analyzer's `Finding` shape.
- **Contract tests.** Every tool returns provenance; no response contains host/credentials
  (asserted by scanning serialized output); tool names satisfy the naming regex and length.
- **CI gates.** `ruff`, `mypy`, `pytest` against fixtures, secret scanning, and the customer-object
  pattern check across the diff.

---

## Traceability

| Design section | Requirements |
|---|---|
| Capability resolution | R3, R7, Appendix A |
| Connection pool / security model | R2, R5 (Rules 1,4,5) |
| Caching | R4 |
| Profile manager | R1 |
| SQL dialect | R2, R6 |
| chains / providers / transformations / queries / hana / texts repositories | R8, R9, R10, R14, R15, R13 |
| Lineage graph model | R11, R12 |
| Routine parser | R11 |
| Description subsystem | R13 |
| Latency + analyzers | R16–R24 |
| Doc generator | R25 |
| MCP surface | R26, R27, R28 |
| External-BI connectors | R30, R22, R23 |
| Testing strategy | R29 |
