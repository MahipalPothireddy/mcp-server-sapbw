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
  metadata to any external endpoint.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainers rather than opening a public
issue. Include a description, reproduction steps, and impact assessment. We aim to acknowledge
reports promptly and coordinate a fix and disclosure timeline.

> Replace this section with the project's real private reporting channel (e.g. a security contact
> email or GitHub private vulnerability reporting) before the repository is made public.

## Scope

This policy covers the server code in this repository. It does not cover the security of the BW/HANA
systems you connect to, your credential management, or your MCP client configuration.
