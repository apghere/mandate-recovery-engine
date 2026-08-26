# Data model — synthetic payer population and decline taxonomy

Written before `data/generator.py` (docs §J.1 rule: distributions get frozen
in writing first, so we cannot unconsciously tune the world to flatter our
own policy later). Parameters below are frozen at end of Day 2 and not
edited afterwards; if a bug is found in the generator's *code*, the fix goes
in code, not in a parameter chosen to fix the benchmark.

This generator embodies stated assumptions, not observed reality. The
benchmark it feeds validates policy logic under those assumptions, not
real-world uplift. That sentence appears verbatim in the README and video.

## Resolving an ambiguity between §J.2 and §J.5

The plan's §J.2 says "10,000 payers, 500 in the sealed test cohort" (5%).
§J.5's split table lists test as "15% (500 mandates)". These are only
consistent if §J.5's percentages are of *generated recovery cases*
(mandate-cycles), not of the raw payer count — a payer can appear in
multiple cycles over a multi-cycle horizon. For Phase 2 (this file + the
generator), we only need the **payer-level** split, since case generation is
a Phase 3 concern (worker/simulator interaction). Decision: split payers
5% test / 15% calibration / 20% dev / 60% train, so the test cohort is
exactly the 500 payers §J.2 states explicitly. §J.5's case-level splits are
revisited when the case-generation pipeline (Phase 3+) exists; this doesn't
block Phase 2 and is not a numerical claim we make in the benchmark yet.

## Payer population — 10,000 payers

All distributions below use `random.Random(seed)` per-payer, seeded
deterministically from `(global_seed, payer_index)` so any single payer's
attributes are reproducible in isolation (needed for the chaos/adversarial
tests without regenerating the whole population).

| Attribute | Distribution | Parameters |
|---|---|---|
| `segment` | categorical | salaried 0.55 / gig 0.25 / self_employed 0.15 / student 0.05 |
| `credit_day` | segment-dependent, see below | day of month, 1-28 |
| `mean_balance` | lognormal | `mu`/`sigma` by segment, see below |
| `balance_volatility` | gamma | `shape`/`scale` by segment, see below |
| `issuer_code` | categorical over 12 codes | unequal `base_success_rate` per issuer, see below |
| `chronic_fail_propensity` | Beta(1.5, 12) | ~8% tail exceeds 0.4 — the "never worth retrying" population the stopping rule exists for |
| `annoyance_sensitivity` | Beta(2, 5) | scaled by a per-segment multiplier, see below |

We use months capped at 28 days everywhere in the simulator's calendar
arithmetic, so `credit_day` never needs to handle month-length edge cases.

### `credit_day` by segment

- **salaried**: 70% mass split evenly across `{1, 2, 7, 28}` (28 stands in
  for "end of month" / "30" from §J.2 given the 28-day calendar), 30% mass
  uniform over the remaining 24 days.
- **gig**: near-uniform with a weekly mode — base uniform weight 1.0 per
  day, with days `{7, 14, 21, 28}` weighted 2.5x (weekly payout mode).
- **self_employed**: uniform over 1-28. Not specified by §J.2; chosen as the
  least-structured segment since self-employed income timing has no single
  dominant cycle.
- **student**: 60% mass on `{1, 5}` (stipend/allowance-like), 40% uniform.
  Not specified by §J.2; a plausible assumption for a small segment (5% of
  population) documented here rather than left silent.

### `mean_balance` — lognormal(mu, sigma), rupees

| Segment | mu (= ln(median ₹)) | sigma |
|---|---|---|
| salaried | ln(25,000) ≈ 10.13 | 0.50 |
| gig | ln(12,000) ≈ 9.39 | 0.60 |
| self_employed | ln(18,000) ≈ 9.80 | 0.70 |
| student | ln(4,000) ≈ 8.29 | 0.50 |

### `balance_volatility` — gamma(shape, scale), interpreted as a coefficient
of variation applied to the day-of-month balance curve (higher = balance
swings harder between credit days)

| Segment | shape | scale | mean |
|---|---|---|---|
| salaried | 4 | 0.15 | 0.60 |
| gig | 2 | 0.40 | 0.80 (highest — §J.2 requirement) |
| self_employed | 3 | 0.25 | 0.75 |
| student | 3 | 0.20 | 0.60 |

### Issuers — 12 codes, unequal reliability

`ISS01`..`ISS12`. `base_success_rate` fixed per code (not resampled per
payer — this is what makes issuer a real covariate, not noise):

`ISS01`=0.93, `ISS02`=0.91, `ISS03`=0.90, `ISS04`=0.89, `ISS05`=0.88,
`ISS06`=0.87, `ISS07`=0.85, `ISS08`=0.84, `ISS09`=0.82, `ISS10`=0.80,
`ISS11`=0.78, `ISS12`=0.55 (deliberately poor — creates the issuer-downtime
covariate the planner and the "downtime-aware deferral" SHOULD-HAVE feature
exploit). Payers are assigned an issuer uniformly at random.

### `annoyance_sensitivity` — Beta(2, 5), then multiplied by a segment
factor and clipped to [0, 1]: salaried x0.8, gig x1.0, self_employed x0.9,
student x1.2 (assumption: students, contacted about smaller amounts, are
more prone to opting out from repeated notices — not specified by §J.2,
documented here as a design choice).

## Correlations realised by construction (not sampled independently)

- **balance ↔ day-of-month ↔ segment**: `mean_balance` and `credit_day` are
  both segment-conditioned, and the simulator (not this generator) derives a
  day-of-month balance curve from `mean_balance`, `credit_day`, and
  `balance_volatility` together — so a payer's actual balance on a given
  day is never independent of segment.
- **issuer ↔ technical-decline rate ↔ downtime windows**: `issuer_code`'s
  fixed `base_success_rate` is the single source both the simulator's
  decline generator and any downtime-window logic read from.
- **amount ↔ segment ↔ insufficient-funds probability**: `mandate_amount`
  (below) is drawn from a segment-scaled distribution, so higher-balance
  segments also tend to carry higher mandate amounts, keeping the
  insufficient-funds odds from becoming a pure amount effect.
- **annoyance ↔ notification count ↔ opt-out hazard**: `annoyance_sensitivity`
  is the single input the (Phase 4) opt-out hazard model reads; notification
  count accumulates against it at simulation time, not generation time.

## `mandate_amount`

With probability 0.04 (exactly hitting §J.2's "4% above the ₹15,000 AFA
threshold" by construction, not by chance): draw uniform(15,000, 100,000).
Otherwise: draw `lognormal(mu=ln(600 * segment_multiplier), sigma=0.6)`
clipped to `[50, 14,999]`, where `segment_multiplier` is salaried=1.4,
gig=0.9, self_employed=1.1, student=0.5 (subscription/EMI-sized amounts
scaled by segment purchasing power).

## Canonical failure taxonomy (§J.3) — reference, not redefinition

The 13-value `Cause` enum and its `RetryDisposition` mapping already live in
`backend/app/domain/types.py` (`Cause`, `RetryDisposition`,
`CAUSE_DISPOSITION`) as the single source of truth, enforced total by
`test_types.py`. This file does not redefine them — `data/taxonomy.yaml`
only adds the raw-decline-string templates per cause, keyed by the same 13
enum values, so a drift between the two would fail fast (generator refuses
to load a taxonomy file whose cause keys don't match the domain enum
exactly — see `data/generator.py::_validate_taxonomy`).

## Raw decline strings (§J.4)

`data/taxonomy.yaml` holds a curated set of raw templates per cause,
covering the required shapes: casing chaos, truncation, issuer jargon,
numeric-only codes, composites, and bilingual fragments. We ship a
representative set (8-10 templates per cause, ~110 total) rather than
mechanically padding to exactly 120 — the point is coverage of the shape
categories the LLM normaliser (Phase 6) must handle, not a specific count.

At generation time: 15% of *instances* drawn are held back from ever being
shown to the dictionary-matching stage (novel-string test set), and 3% of
instances get an adversarial suffix appended (a prompt-injection attempt
embedded in the remark field, per §J.4) — both are draw-time properties, not
template properties, so the same template can appear in both a
dictionary-visible and a held-out instance.

## Splits

| Split | Share of payers | Payer count |
|---|---|---|
| train | 60% | 6,000 |
| calibration | 15% | 1,500 |
| dev | 20% | 2,000 |
| test | 5% | 500 |

Assignment: payers sorted by a deterministic hash of `payer_id` (not by
generation order, to avoid any accidental correlation with e.g. issuer
assignment order), then cut at the cumulative thresholds above. Split
assignment does not change if the population size changes in a later run
with the same seed prefix — new payers land in whichever bucket their hash
falls into.

The test split's payer-ID set, canonically serialised (sorted, one per
line, trailing newline), is SHA-256'd and committed to
`data/TEST_SPLIT_SHA256`. A CI test (`tests/data/test_generator.py`)
regenerates the population at the pinned seed and asserts the hash matches
— this is the thing that makes "the test split was touched exactly once"
a checkable claim instead of a promise.

## Rare and adversarial cases (§J.6)

Not generated by `data/generator.py` (payer population has no notion of a
"cycle" yet). These are properties of the *case/event* generator that
consumes this payer population in Phase 3 (worker/simulator integration),
and are deferred to that phase's own design note rather than specified
speculatively here.

## Seed

Global seed: `20260827` (today's date at the time this was written, chosen
for no reason other than being memorable and fixed). Changing it is a
parameter change and therefore requires re-freezing this document.
