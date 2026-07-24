# Test fixtures — synthetic metadata only

Everything in this directory is **synthetic**. It exists so the entire test suite can run
offline with no live BW system.

Hard rules (enforced by CI and by the mission's non-negotiable rules):

- **No customer metadata, ever.** No real object names, chain names, DSO/ADSO names,
  query technical names, ABAP routine source, or schedules.
- Use invented names that do not resemble any real landscape. For generated-table
  examples, use clearly synthetic forms such as `/BIC/AZDEMO01` scoped to this directory
  only — concrete `/BIC/…` or `/BI0/…` names must never appear anywhere outside
  `tests/fixtures/`.
- No credentials, host names, ports, or connection strings.

The customer-object CI check scans the whole repository except this directory. If you need
a realistic-looking fixture, it belongs here and nowhere else.
