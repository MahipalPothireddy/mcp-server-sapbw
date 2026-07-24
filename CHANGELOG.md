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

### Notes
- Live capability discovery (B2) is a hard gate before any repository or tool code.
- Scenario 9.6 reclassified as an ECC-connector capability (source lives in ECC, not BW).
