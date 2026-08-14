# Security Policy

## Design guarantees

`mcp-server-sapbw` is built to be structurally safe to point at a production BW system.

- **Read-only, permanently.** The server issues `SELECT` statements only. There is no code path
  capable of DDL, DML, activation, chain triggering, or writing RFC/BAPI calls. Read-only is
  enforced at the connection layer, not by convention.
- **Fail closed on write grants.** When a profile sets `read_only_user: true`, the server asserts
  that the connecting user holds no write/DDL/execute grants and refuses the connection otherwise.
- **Secrets by environment variable only.** Credentials are supplied via `${VAR}` interpolation.
  No secret is ever stored in code, configuration committed to the repo, logs, test fixtures, or
  error messages. Host names and connection strings are scrubbed from all error text before it
  leaves the process.
- **No customer metadata in the repository or package.** Extracted BW content is customer
  intellectual property. It is cached locally (git-ignored) and never committed or distributed. CI
  fails if customer object-naming patterns appear outside `tests/fixtures/`.
- **No third-party exfiltration.** The server does not transmit project code, credentials, or
  metadata to any external endpoint. Diagram rendering is local; there is no hosted renderer.
- **Bounded per call.** Every tool runs inside a query and time budget, so a single call cannot
  issue unbounded statements or hold a database session indefinitely.

## What the server writes to disk

Two things leave memory. Both are local to the machine running the server, and neither is ever
transmitted anywhere.

### 1. The metadata cache (on by default)

Extracts are cached so repeat questions do not re-read millions of rows. **This means customer
intellectual property is stored at rest**, specifically:

| Cached object type | What it contains |
|---|---|
| `routine_code`, `routine_analysis` | **ABAP routine source** and its parsed dependencies |
| `query`, `query_lineage` | BEx query definitions, restrictions, variables |
| `transformation` | Field mappings and rule types |
| `provider`, `chain`, `calc_view` | Object names, fields, structures |
| `chain_runtimes` | Run statistics (separate one-hour tier) |

- **Location.** A per-user directory, resolved in this order: `SAPBW_CACHE_DIR`, then
  `%LOCALAPPDATA%\mcp-server-sapbw\cache` on Windows, then `$XDG_CACHE_HOME/mcp-server-sapbw` or
  `~/.cache/mcp-server-sapbw`. Per-user rather than system-wide, because the cache inherits the
  reach of the credentials that filled it. It is never written relative to the working directory.
- **Retention.** Structural extracts 24 hours, runtime statistics one hour (hard-capped). Entries
  are also invalidated automatically whenever capability discovery re-runs.
- **Inspect it.** `bw_cache_status(system)` reports the location, size and entry counts per object
  type without reading any cached value.
- **Purge it.** `bw_refresh_cache(system, scope)` — `all`, or one object type from the table above.
  Deleting the file is equally safe.
- **Turn it off.** Set `cache_enabled: false` on the profile. Nothing is then written to disk, at
  the cost of re-reading on every call. Use this where customer metadata at rest is not acceptable.

### 2. Generated documentation (only when you ask)

`bw_generate_docs` writes a markdown knowledge base containing object names, routine source and
query definitions to a directory you name. It defaults to `output/`, which is git-ignored. Treat
that directory with the same care as the source system.

## Authorisation data is never cached

The four security tools (`bw_security_overview`, `bw_list_analysis_auths`,
`bw_get_analysis_auth`, `bw_get_query_auth_exposure`) read a different class of data from the rest of
the server. `RSECVAL` holds permission *values* — "cost centres 1000–1999, company code DE01" states
what a named person may see, and joined to a user id it is personal data.

So, by exception to the section above:

- **Nothing read by the security repository is cached, at any tier.** It is constructed without a
  cache and ignores one if passed, so the guarantee holds even if a future call site gets it wrong.
  Persisting permission data would widen this file's blast radius, and a stale answer to "who can see
  this" is worse than a slow one. A test asserts the repository has no cache.
- **Concrete values are opt-in.** Listing and overview return shape only (which characteristics, how
  many ranges, catch-all or not), so a landscape-wide question cannot place a permission dump into a
  transcript. Only `bw_get_analysis_auth` returns ranges, and its payload is labelled
  `contains_data_values` so that is auditable after the fact.
- **`bw_get_query_auth_exposure` never resolves who sees what.** It reports the characteristics that
  make a query user-specific. Per-user value resolution is deliberately out of scope.

Note that these tables are often unreadable by a locked-down reporting user — they *are* the
authorisation model. That is reported as a documented gap, never as "no authorisations exist".

## What is never written or logged

- Credentials, host names and connection strings are scrubbed from every error and log record.
- **Bound query parameters are never logged at any level**, because they carry concrete object
  names. Query logs record the table, elapsed milliseconds and row count only.
- Logs go to stderr, never stdout (stdout carries the MCP protocol).

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainers rather than opening a public
issue. Include a description, reproduction steps, and impact assessment. We aim to acknowledge
reports promptly and coordinate a fix and disclosure timeline.

> Replace this section with the project's real private reporting channel (e.g. a security contact
> email or GitHub private vulnerability reporting) before the repository is made public.

## Scope

This policy covers the server code in this repository. It does not cover the security of the BW/HANA
systems you connect to, your credential management, or your MCP client configuration.
