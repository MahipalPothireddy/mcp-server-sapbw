# S01_HUMAN_VERIFICATION.md

Independent verification form for scenario **S01 — Explain a real provider**.

This is for a **BW developer verifying from BW's own tooling**, not from this server. It is the step
that decides whether S01 can move from a technical pass to `REAL_BW_VALIDATED`.

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
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

### 2. Semantic key fields

| Field | Value |
|---|---|
| Expected comparator | The exact set of key fields, as a set — order does not matter |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

### 3. Direct inbound transformations

Transformations whose **target** is `<OBJECT>`.

| Field | Value |
|---|---|
| Expected comparator | The complete set of source objects feeding `<OBJECT>` directly. Exact-set match |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

### 4. Direct downstream transformations

Transformations whose **source** is `<OBJECT>`.

| Field | Value |
|---|---|
| Expected comparator | The complete set of direct targets. Exact-set match |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

### 5. Declared transformation self-loop

`<OBJECT>` is expected to be both source and target of at least one declared transformation. This is a
real BW modelling choice, not an artefact, and the engine must still represent it.

| Field | Value |
|---|---|
| Expected comparator | Whether a transformation exists with `<OBJECT>` as both source and target, and its update mode |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

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
| Human observed — CompositeProviders | |
| Human observed — count | |
| Engine value — CompositeProviders | |
| Engine value — query-generated views also returned | |
| Verdict | |
| Source / tool | |
| Notes | |

If your CompositeProvider count and ours disagree only by the query-view entries, that is defect **D9**
and not a lineage miss. Say so in the notes; it is a labelling defect we already know about and your
count is what settles its size.

### 7. Representative deeper path

Pick **one** upstream path and **one** downstream path of at least two hops, and follow each by hand.

| Field | Value |
|---|---|
| Expected comparator | The ordered object sequence along each chosen path |
| Human observed — upstream path | |
| Human observed — downstream path | |
| Engine value — upstream path | |
| Engine value — downstream path | |
| Verdict | |
| Source / tool | |
| Notes | |

---

## Optional checks

Complete only where BW's UI makes the fact independently verifiable. Leave as `unverifiable` otherwise
— that is a legitimate outcome and more useful than a guess.

### 8. DTP count and update modes

| Field | Value |
|---|---|
| Expected comparator | Number of DTPs loading `<OBJECT>` and each one's update mode (full / delta / init) |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

### 9. Last successful load

| Field | Value |
|---|---|
| Expected comparator | Date/time of the last **successful** request and its record count, from the manage screen |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

### 10. Routine-embedded consumers

Objects that read `<OBJECT>` only from inside ABAP routine code, invisible to BW's where-used list.

| Field | Value |
|---|---|
| Expected comparator | Any object whose transformation routine reads `<OBJECT>`, established by reading the ABAP |
| Human observed | |
| Engine value | |
| Verdict | |
| Source / tool | |
| Notes | |

Expect `partial` here at best. Record whether any routine you inspected used dynamic SQL, a function
module or a class method, because those are the cases our static parse cannot follow — a predicted
shortfall is acceptable, an unpredicted one is a defect.

---

## Verification metadata

Do not fill these in automatically. They are the record of who established the ground truth, when, and
how independently — and without them the result is not awardable.

```yaml
human_expert:
role:
verification_date:
source_tool:
independent:
ground_truth_established_at:
overall_verdict:
notes:
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

## Scope note

This form covers S01 only — object analysis. Field-level lineage is S02 and is deliberately not
verified here, so do not extend the deeper-path check into per-field derivation. Tableau is out of
scope for the current phase and no check above depends on it.
