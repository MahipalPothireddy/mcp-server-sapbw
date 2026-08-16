# Deployment modes and required grants

This is the answer to "what do we grant the user this server connects with, and what does it cost
us to withhold part of it?"

Two postures are supported. Both are legitimate; they trade coverage against the size of the
conversation with your security team. Declare the one you provisioned as `access_mode` on the
profile, and the server will report whether the evidence agrees.

| | `technical_read` | `least_privilege` |
|---|---|---|
| Grant | SELECT on the ABAP schema + `CATALOG READ` | SELECT on an explicit object list |
| Statements to issue | 2 | ~100, or per group |
| Coverage | everything implemented here | stated per group below |
| Row-level-security tools | work | usually withheld, by design |
| Typical use | a discovery or documentation exercise | standing access to production |

Neither mode can write. That is not a property of the grant — the server rejects any statement
whose leading verb is not `SELECT`/`WITH` before it reaches the driver, and refuses the connection
outright if the user holds write privileges and `read_only_user: true` is set. The grant is
defence in depth, not the guarantee. See [SECURITY.md](../SECURITY.md).

## Why the mode has to be visible

A refused read and an object your release does not have both end the same way: no rows. Until
these were separated, discovery recorded a refusal as an observed absence, and the consequence was
a specific wrong answer — a user without SELECT on `DD02L` produced `adso: false`, and tools then
reported *"not available on BW 7.50"*. That sends you to plan a BW upgrade for something one
`GRANT SELECT` fixes.

Probes now record `denied` separately from `absent`. Where presence is unknown the server says
unknown, and `bw_access_report(system)` names the grant. Read it before concluding that your
system lacks a feature.

```
bw_access_report(system="prd")
  -> observed_mode, declared_mode, mode_mismatch
     groups[]        state per grant group, and what withholding it costs
     grants_required ready-to-run statements for whatever was refused
     blocked_tools   tools that read at least one refused object
```

## Mode 1 — `technical_read`

```sql
-- Substitute your ABAP schema (bw_system_profile reports it; often SAPHANADB or SAPABAP1)
-- and the user this server connects as.
GRANT SELECT ON SCHEMA "SAPHANADB" TO BW_DISCOVERY_USER;
GRANT CATALOG READ TO BW_DISCOVERY_USER;
```

`CATALOG READ` is not optional and not cosmetic. HANA filters `SYS` catalog views by the reader's
privileges rather than raising an error, so without it `SYS.OBJECT_DEPENDENCIES`, `SYS.VIEWS` and
`SYS.M_CS_TABLES` return **empty** and the HANA half of the landscape looks absent. That silent
filtering is the failure mode this whole page exists to make visible.

To withhold row-level-security metadata while keeping everything else, revoke that group back:

```sql
REVOKE SELECT ON "SAPHANADB"."RSECVAL" FROM BW_DISCOVERY_USER;
REVOKE SELECT ON "SAPHANADB"."RSECHIE" FROM BW_DISCOVERY_USER;
REVOKE SELECT ON "SAPHANADB"."RSECUSERAUTH" FROM BW_DISCOVERY_USER;
REVOKE SELECT ON "SAPHANADB"."RSECTXT" FROM BW_DISCOVERY_USER;
```

The four security tools then report a documented gap. That is a supported configuration, not a
misconfiguration — see the `security` row below.

## Mode 2 — `least_privilege`

Grant by group. `bw_access_report` prints the exact statements for whatever is missing, so the
practical route is: grant the two required groups, connect, run the report, and grant onward from
its output.

| Group | Required | What it gives you | What withholding it costs |
|---|:-:|---|---|
| `dictionary` | **yes** | Which metadata tables exist here, and the BW release. Gates everything else. | Nothing works. There is no degraded mode. |
| `catalog` | **yes** | ABAP schema resolution, row estimates, the read-only assertion. | Schema must be named on the profile; no row counts; the fail-closed check cannot complete, which refuses the connection when `read_only_user: true`. |
| `providers` | | Providers, InfoObjects, fields, descriptions, attributes. | No descriptions, field lists, search or inventory. |
| `dataflow` | | Transformations, field rules, ABAP routine source, DTPs, DataSources, request ledger. | No lineage, impact analysis, routine analysis or load currency. Most of the server's value. |
| `chains` | | Chain structure, run logs, job periodicity. | No runtime statistics, no p95, no schedule matrix, no load cadence. |
| `queries` | | BEx query definitions, element trees, variables. | No query definitions or field-level query lineage. |
| `flows_3x` | | BW 3.x transfer and update rules. | Lineage stops at any DataSource with no 7.x transformation — over a thousand of them on the reference system. |
| `hana` | | Calc view dependencies, BW↔HANA crossings, volume. | No calc view lineage or crossings, so consumption from outside BW is invisible. |
| `statistics` | | Query usage ranking from execution history. | Usage falls back to `LASTUSED` alone; decommissioning candidates rest on weaker evidence. |
| `extractor` | | Extract structures and appended customer fields. | Source-side extractor enhancements are invisible from BW. |
| `security` | | Authorisation shape, coverage gaps, per-user exposure. | Four tools report a gap. **Withhold this first if you withhold anything.** |

The group definitions live in [`core/access.py`](../src/mcp_server_sapbw/core/access.py) and a test
asserts every capability discovery tracks belongs to exactly one group, so a table added later
cannot become an ungranted dependency this page does not mention.

## Two things worth knowing before you grant

**`RSAABAP` holds ABAP routine source.** It is in the `dataflow` group because lineage and routine
analysis are built on it. It is customer intellectual property and frequently business logic. Grant
it knowingly, and note that structural extracts are cached on local disk by default — set
`cache_enabled: false` on the profile to keep nothing at rest.

**`RSECVAL` holds permission values.** Joined to a user id it is personal data. Nothing read by the
security repository is ever cached, at any tier, and only `bw_get_analysis_auth` returns concrete
ranges (its payload is labelled `contains_data_values`). Withholding the group entirely is the
stronger control and is fully supported.

## What this report is not

`bw_access_report` reports what was *exercised* on this connection. A group reported `granted` was
probed, not audited: it says a representative read succeeded, not that every object in the group
was checked, and not that the grant is appropriate. It is a provisioning aid, not a permission
review.
