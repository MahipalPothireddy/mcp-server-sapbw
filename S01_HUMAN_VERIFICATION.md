# S01_HUMAN_VERIFICATION.md

Independent verification form for scenario **S01 — Explain a real provider**.

This is for a **BW developer verifying from BW's own tooling**, not from this server. It is the step
that decides whether S01 can move from a technical pass to `REAL_BW_VALIDATED`.

> **Status: completed, verdict `partial`. S01 does NOT reach `REAL_BW_VALIDATED`.**
>
> A verification was performed against the production system by the landscape's BW developer. It
> found two real defects and settled the size of a third, so it did its job. It does not award the
> rung, and the reason is condition 3 rather than any disagreement: part of the ground truth was read
> from `RSZCOMPDIR` directly, which is the same table this server reads. That is *dependent* ground
> truth by this form's own definition — it catches join and decode errors but cannot catch a shared
> wrong assumption about what a table means.
>
> See **Outcome** at the foot of this file for exactly what would close the gap.

## Before you start

**The object under test is not named in this file.** Committed files carry no customer object names.
The technical name is supplied separately by whoever asks you to run this — it is held in the
git-ignored `output/s01_ground_truth.json`, field `name`. Everywhere below, `<OBJECT>` means that name.

**Record your reading before you are shown ours.** The right-hand column is deliberately blank. Fill in
the *Human observed* column from RSA1 first, then ask for the engine values and complete the row. If
you are shown our answer first, the comparison still has value but independence is weakened — and you
must record that in the notes, because it changes what a pass proves.

**Use BW tooling, not SQL.** RSA1, the data-flow display, the transformation editor, the manage screen.
Reading the same tables with SE16 still catches join and decode errors, but it is *dependent* ground
truth and must be recorded as such.

## What each verdict means

| Verdict | Use when |
|---|---|
| `pass` | Your reading and the engine's agree |
| `fail` | They disagree, and yours is correct |
| `partial` | The engine is correct but incomplete, **and said so** |
| `unverifiable` | BW's UI does not let you establish this independently |

`partial` is the expected verdict for anything routine-derived: static ABAP analysis is a lower bound
by construction. It only counts as acceptable if the engine's answer declared itself a lower bound. An
incomplete answer presented as complete is a `fail`, not a `partial`.

---

## Required checks

### 1. Object type

| Field | Value |
|---|---|
| Expected comparator | The object's type as RSA1 shows it (classic DSO, Advanced DSO, InfoCube, MultiProvider, CompositeProvider) |
| Human observed | Advanced DSO |
| Engine value | `adso` |
| Verdict | `pass` |
| Source / tool | Not stated by the verifier |
| Notes | Exact agreement. |

### 2. Semantic key fields

| Field | Value |
|---|---|
| Expected comparator | The exact set of key fields, as a set — order does not matter |
| Human observed | 5 InfoObject names, one of them customer-namespace |
| Engine value | The same 5, as generated **table column** names |
| Verdict | `pass` |
| Source / tool | Not stated by the verifier |
| Notes | Recorded as a `fail` on first reading, then withdrawn: the two are the same five objects in two different naming layers. A standard InfoObject loses its leading `0` in the generated column, a customer one gains `/BIC/`. The engine was right and the *comparison* was wrong. Two consequences were recorded rather than waved away — defect **D11** was withdrawn as a correctness defect, and the real gap it exposed (the response never said which naming layer it was handing back) became defect **D14**, now fixed: fields carry `name_layer`, plus the InfoObject with `infoobject_resolution` separating a catalogue-confirmed name from a convention-derived one. |

### 3. Direct inbound transformations

Transformations whose **target** is `<OBJECT>`.

| Field | Value |
|---|---|
| Expected comparator | The complete set of source objects feeding `<OBJECT>` directly. Exact-set match |
| Human observed | 6 sources: 2 Advanced DSOs (one of them `<OBJECT>` itself), 1 CompositeProvider, 3 DataSources |
| Engine value | The same 6 |
| Verdict | `pass` |
| Source / tool | Not stated by the verifier |
| Notes | Exact-set match, 6 of 6, including the self-referencing transformation covered by check 5. |

### 4. Direct downstream transformations

Transformations whose **source** is `<OBJECT>`.

| Field | Value |
|---|---|
| Expected comparator | The complete set of direct targets. Exact-set match |
| Human observed | Answered together with check 6 — the verifier gave consuming CompositeProviders rather than transformation targets |
| Engine value | Reported under check 6 |
| Verdict | `unverifiable` as posed |
| Source / tool | Not stated by the verifier |
| Notes | Not a disagreement: a CompositeProvider consumes a part provider through a generated calculation view, not through a transformation, so the objects the verifier named do not appear in a transformation where-used list at all. The question as written and the answer given are about different relationships. Settled under check 6; this row is left `unverifiable` rather than credited as a pass. |

### 5. Declared transformation self-loop

`<OBJECT>` is expected to be both source and target of at least one declared transformation. This is a
real BW modelling choice, not an artefact, and the engine must still represent it.

| Field | Value |
|---|---|
| Expected comparator | Whether a transformation exists with `<OBJECT>` as both source and target, and its update mode |
| Human observed | Yes, one such transformation; update mode Full |
| Engine value | Yes, one transformation, named; update mode `full` |
| Verdict | `pass` |
| Source / tool | Not stated by the verifier |
| Notes | Agreement on existence and on update mode. Reading this check prompted the check on how update modes are reported generally, which surfaced defect **D13**: where several active DTPs connect one pair of objects with different modes — 208 of 1043 active pairs on this system — the reported mode was whichever row the database returned last. Now every mode is reported. |

### 6. Direct CompositeProvider consumers

CompositeProviders that consume `<OBJECT>` as a part provider. A CompositeProvider consumes its parts
through a **generated calculation view**, not through a transformation, so this will *not* appear in a
transformation where-used list. Read it from the CompositeProvider side: which CompositeProviders list
`<OBJECT>` among their part providers.

**Read this check carefully — it is the one most likely to disagree.** The engine currently returns a
consumer set that mixes two different things: genuine CompositeProviders, and generated calculation
views belonging to **BEx queries**. Please record the two separately.

| Field | Value |
|---|---|
| Expected comparator | The set of genuine CompositeProviders having `<OBJECT>` as a part provider. Exact-set match |
| Human observed — CompositeProviders | 6 |
| Human observed — count | 6 |
| Engine value — CompositeProviders | 13 |
| Engine value — query-generated views also returned | 19 at the time of verification, since re-typed as queries |
| Verdict | `pass` for the engine; the human list was a subset |
| Source / tool | Not stated by the verifier for the CompositeProvider list; `RSZCOMPDIR` (SE16-style) for the query list |
| Notes | The engine's 13 is a superset of the verifier's 6, and the 7 extras were settled against BW's **declared CompositeProvider part definitions** — a different source from the calculation-view dependency path the engine used, so the confirmation is not self-referential. Defect **D9** was the labelling half and is fixed: a query's generated view is now typed `query`, not `compositeprovider`. |

### 6b. BEx queries reading `<OBJECT>` (added during verification)

| Field | Value |
|---|---|
| Expected comparator | The set of BEx queries defined on `<OBJECT>` and on the CompositeProviders above it |
| Human observed | 50 queries, listed by technical name |
| Engine value at verification | 23, none of them from the list — and the walk reported `completeness="complete"` |
| Engine value after fix | All 50 found, 0 missing, at depth 2 |
| Verdict | `fail` at verification; the defect is fixed and re-measured, but **the re-measurement is ours, not the verifier's** |
| Source / tool | `RSZCOMPDIR` with `OBJVERS='A'`, `COMPDIM=1` and name filters — SE16-style, **dependent** ground truth |
| Notes | This is the most valuable row in the form: it found defect **D12**. Query consumers were only ever discovered side-on, by noticing that the calculation view BW generates for a query touched the object's active table. Nothing asked `RSZCOMPIC` which reports are *defined on* a provider, so the 48 queries on the CompositeProvider above it were invisible — and the answer still called itself complete, which is the worse half. Fixed: the declared assignment is read, filtered to query roots via `RSZELTDIR` (`DEFTP='REP'`), and a capped result now reports `semantic_limit` instead of `complete`. Re-verification by the human is still outstanding. |

### 7. Representative deeper path

Pick **one** upstream path and **one** downstream path of at least two hops, and follow each by hand.

| Field | Value |
|---|---|
| Expected comparator | The ordered object sequence along each chosen path |
| Human observed — upstream path | Not supplied |
| Human observed — downstream path | Partially: one 3-hop chain through two CompositeProviders to an InfoObject |
| Engine value — upstream path | Full graph produced: 4 upstream levels |
| Engine value — downstream path | Full graph produced: 3 downstream levels, including the chain the verifier named |
| Verdict | `unverifiable` |
| Source / tool | Not stated by the verifier |
| Notes | The engine's depth-3 both-directions graph contains the downstream chain the verifier described. It is left `unverifiable` because no ordered path was recorded by hand for comparison — the engine agreeing with itself is not a check. Producing that graph did surface a production failure worth recording: at the shipped default budget the call returned **no graph at all**, which is covered under REQ-19 and now fixed. |

---

## Optional checks

Complete only where BW's UI makes the fact independently verifiable. Leave as `unverifiable` otherwise
— that is a legitimate outcome and more useful than a guess.

### 8. DTP count and update modes

| Field | Value |
|---|---|
| Expected comparator | Number of DTPs loading `<OBJECT>` and each one's update mode (full / delta / init) |
| Human observed | Not supplied |
| Engine value | Reported per edge; see check 5 for the self-loop's mode |
| Verdict | `unverifiable` |
| Source / tool | — |
| Notes | Not attempted. Related defect **D13** was nonetheless found and fixed while checking how modes are reported. |

### 9. Last successful load

| Field | Value |
|---|---|
| Expected comparator | Date/time of the last **successful** request and its record count, from the manage screen |
| Human observed | Not supplied |
| Engine value | Not compared |
| Verdict | `unverifiable` |
| Source / tool | — |
| Notes | Not attempted. |

### 10. Routine-embedded consumers

Objects that read `<OBJECT>` only from inside ABAP routine code, invisible to BW's where-used list.

| Field | Value |
|---|---|
| Expected comparator | Any object whose transformation routine reads `<OBJECT>`, established by reading the ABAP |
| Human observed | Not supplied |
| Engine value | 49 routine-derived edges in the depth-3 graph, each flagged advisory |
| Verdict | `unverifiable` |
| Source / tool | — |
| Notes | Not attempted. This is the check that would test the claim the tool exists to make, so it is the most valuable one still open. |

Expect `partial` here at best. Record whether any routine you inspected used dynamic SQL, a function
module or a class method, because those are the cases our static parse cannot follow — a predicted
shortfall is acceptable, an unpredicted one is a defect.

---

## Verification metadata

Do not fill these in automatically. They are the record of who established the ground truth, when, and
how independently — and without them the result is not awardable.

```yaml
human_expert: PENDING - to be supplied by the verifier
role: BW developer / landscape owner (production and QA)
verification_date: 2026-07-24
source_tool: >
  Not stated for checks 1-6a. For check 6b: RSZCOMPDIR read directly with
  OBJVERS='A', COMPDIM=1 and technical-name filters (SE16-style).
independent: false
ground_truth_established_at: >
  Before being shown the engine's values. The verifier supplied their reading for
  checks 1-6 in one message, unprompted by any engine output, and the query list in
  a second message before the engine's query count was given.
overall_verdict: partial
notes: >
  Two defects found and one sized: D12 (query consumers incomplete AND the answer
  called itself complete), D14 (Advanced DSO field list read from the text table),
  and the CompositeProvider consumer count settled at 13 against the verifier's 6.
  D11 was raised and withdrawn after the verifier corrected the BW-to-HANA naming
  convention. Blocked from REAL_BW_VALIDATED by `independent: false` only - see
  Outcome below. `human_expert` is deliberately left PENDING rather than filled in
  on the verifier's behalf: it is their name to give, and a personal name in a
  tracked file is the same class of content as a customer object name, so it belongs
  in the git-ignored record.
```

Field meanings:

| Field | What to write |
|---|---|
| `human_expert` | Name of the person who did the verification |
| `role` | Their role, e.g. BW developer, BW operator, data architect |
| `verification_date` | When the form was completed |
| `source_tool` | What you read from, e.g. RSA1 Display Data Flow, transformation editor, manage screen |
| `independent` | `true` if established without reading the same tables this server reads; `false` for SE16-style reading |
| `ground_truth_established_at` | When you recorded your values — **before or after** being shown ours |
| `overall_verdict` | `pass`, `fail`, `partial` or `unverifiable` across the required checks |
| `notes` | Anything that qualifies the result, including whether you saw our answer first |

## What `REAL_BW_VALIDATED` requires

All four, together. Any one missing and the scenario stays a technical pass:

1. **`human_expert` is non-blank** — a person, not a tool run.
2. **`ground_truth_established_at` carries a date and time** — and it is what decides whether your
   reading was independent in practice, not just in principle.
3. **`independent` is `true`** — established without reading the same metadata this server reads.
4. **A passing comparator and verdict** across the required checks, where `partial` counts only if the
   engine's own answer declared itself a lower bound.

`CUSTOMER_VALIDATED` is a separate and higher rung. It is not awardable by this project at all: it
requires an external validator on their own landscape, and the model refuses the claim without one.

---

## Outcome

**S01 stays at `INTEGRATION_TESTED`.** Measured against the four conditions above:

| # | Condition | Met | Why |
|---|---|---|---|
| 1 | `human_expert` non-blank | **no** | Left `PENDING` on purpose; the verifier's name is theirs to supply |
| 2 | Dated, and blind in practice | **yes** | The reading was recorded before the engine's values were shown |
| 3 | `independent: true` | **no** | Check 6b was read from `RSZCOMPDIR`, a table this server also reads |
| 4 | Passing verdict across required checks | **no** | 4 of 7 pass; 6b was a fail, and its fix has not been re-verified by the human |

Two of those are administrative and one is not. Condition 3 is the substantive one: ground truth taken
from the same table the server reads proves the SQL round-tripped. It cannot catch a wrong assumption
about what a table means, because it shares the assumption — and this session found two defects of
exactly that shape (D12 and D14), which is the argument for the condition rather than against it.

**What would close it**, in order of value:

1. **Re-verify check 6b from BEx tooling** — Query Designer, or RSA1's provider view of its queries —
   rather than from `RSZCOMPDIR`. That single change flips condition 3, and it also re-tests the D12
   fix from an independent direction instead of ours.
2. **State the source tool for checks 1, 2, 3, 5 and 6a.** If those were read from RSA1, they are
   already independent and only the record is missing.
3. **Supply `human_expert`.** Into the git-ignored `output/s01_ground_truth.json` rather than here.
4. **Attempt check 10.** Routine-embedded consumers are the dependency BW's own where-used list does
   not have, so it is the check that tests the claim this server exists to make. `partial` is the
   expected and acceptable verdict.

Until 1, 3 and 4 above are recorded, the honest statement is the one at the top of this file: a
verification happened, it was valuable, and it does not award the rung.

## Scope note

This form covers S01 only — object analysis. Field-level lineage is S02 and is deliberately not
verified here, so do not extend the deeper-path check into per-field derivation. Tableau is out of
scope for the current phase and no check above depends on it.
