# Contributing to mcp-server-sapbw

Thanks for helping build a safe, portable metadata server for SAP BW-on-HANA.

## Ground rules (non-negotiable)

1. **Read-only forever.** Never add a code path that can write to a BW system — no DDL, DML,
   activation, chain triggering, or writing RFC/BAPI calls. All DB access goes through the
   `SELECT`-only entry point.
2. **No customer metadata, ever.** Do not commit real object names, ABAP source, query
   definitions, chain schedules, credentials, or host names. Extracted content stays local and
   git-ignored.
3. **Synthetic fixtures only.** Test data lives in `tests/fixtures/` and uses invented names.
   Concrete generated-table names (`/BIC/<name>`, `/BI0/<name>`) must never appear outside that
   directory. In source and docs, use the placeholder form `/BIC/<name>` — never a concrete
   instance.
4. **Never invent metadata.** If a table/column may not exist on a release, gate it through the
   capability resolver and return a structured "unsupported on this release" result rather than
   guessing.
5. **Provenance on every fact.** Every returned record must carry `source_table` and `source_key`.

## Development setup

```bash
# Create an environment and install with dev extras
python -m venv .venv
# Windows: .venv\Scripts\activate   |   POSIX: source .venv/bin/activate
pip install -e ".[dev]"
```

## Checks (run before pushing)

```bash
ruff check .
ruff format --check .
mypy src tests
pytest
```

The entire suite runs offline. No test may open a network connection or touch a live BW system.

## Secret scanning

CI runs `detect-secrets`. Run it locally before committing if you touch config or examples:

```bash
detect-secrets scan --all-files
```

## Commit and PR conventions

- Keep changes scoped to a single build prompt / task where possible.
- Reference the relevant requirement IDs (see `.kiro/specs/mcp-server-sapbw/requirements.md`).
- Update `CHANGELOG.md` under `[Unreleased]`.
- Update `PROGRESS.md` at the end of a build-prompt session.

## Build order

Development follows the ordered build prompts B0–B11 in `.kiro/steering/mission.md` Section 13, one
per session, with the B2 capability-discovery gate blocking repository/tool code. See
`.kiro/specs/mcp-server-sapbw/tasks.md` for the task breakdown and dependencies.
