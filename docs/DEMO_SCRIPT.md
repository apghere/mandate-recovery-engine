# Demo video script — 5 minutes, timed

Docs Day 7: "Record the video. Budget 8-10 takes. 1080p, subtitles, real
system, no mockups." This script is the rehearsal aid — read it, cut it
in your own words, then do the wall-clock rehearsal (Day 6's own
instruction) three times before the real take. Every command and screen
below was re-verified on 2026-09-03 against the redesigned dashboard and
a fresh `make demo-seed` — the counterfactual panel described in section
2:15–3:45 is now visually distinct (accent-tinted column vs. neutral
column, a computed attempt-count delta line), not just data in
identical-looking boxes, so that beat lands faster on screen than it used
to. Total budget: 5:00. Pad time is deliberately built in — if you're
behind at any checkpoint, cut the next section's detail, not its
existence.

**Before recording:** `make demo-seed` (right before, not hours in
advance — `make test` truncates the same tables), then start the API
(`.venv/bin/python -m uvicorn app.api.app:app --app-dir backend --reload`,
from repo root). Have `docs/screenshots/` open as a fallback if anything
misbehaves live — and the live deploy
(https://mandate-recovery-engine.vercel.app/dashboard/index.html) as a
second fallback if your local environment itself misbehaves.

---

## 0:00–0:30 — The problem, in one sentence (voice over the README's
architecture diagram or a title card)

> "In India, a failed recurring UPI or eNACH debit gives you four
> attempts, in restricted time windows, each needing 24 hours' notice
> that itself lets the payer opt out. Most systems burn all four on a
> fixed D+1/D+3/D+7 schedule copied from American dunning playbooks —
> whether or not the payer will ever have money, and whether or not the
> mandate is already gone. This is Mandate Recovery Engine: you get four
> attempts, spend them well."

## 0:30–1:15 — What it is, and what it deliberately is not

> "MRE diagnoses why a debit failed, forecasts calibrated success
> probability across every legally permitted future slot, and solves for
> the attempt-and-notification plan that maximises expected recovered
> value — with an explicit rule for when to *stop* and hand off to a
> human, instead of blindly spending the budget."

Say the "what it's not" line on camera, not just in the README — it's
the strongest pre-emption of the "it's an LLM wrapper" attack (docs T
item 1): *"There is no agent here, and no LLM making a money decision.
The language model does exactly two things: read a bank's decline string
and decide when it's not worth continuing to write text to a human — it
has zero authority over timing or execution."*

## 1:15–2:15 — Architecture, start at the best 200 lines

Screen: `backend/app/domain/planner.py` open in an editor, or the
Mermaid diagram in README.md 5.

> "Start reading here. This is exact backward induction over (slot,
> attempts remaining, notice state) — not a heuristic, not an LLM. It's
> verified against an independently-coded brute-force enumeration at
> every reachable state, plus property tests that check monotonicity —
> more budget or a higher success probability never lowers the value.
> It solves in under half a millisecond. Everything upstream of it —
> decline-string normalisation, the calibrated success model — feeds
> this one function a probability per slot; everything downstream just
> executes what it decided, through a deterministic, idempotent,
> Postgres-backed state machine with a tamper-evident audit log."

## 2:15–3:45 — Three workflows, live in the dashboard

Screen: `http://localhost:8000/dashboard/index.html`.

**W1 — automatic recovery** (click `CYC-0-RECOVERY`):

> "This mandate failed on attempt 1 for insufficient funds. The planner
> didn't just retry on a fixed schedule — look at the counterfactual
> panel: the fixed D+1/D+3/D+7 schedule would have attempted here, here,
> here. MRE's plan waited for a slot closer to this payer's actual salary
> day. It recovered on the last attempt."

**W2 — stop and escalate** (click `CYC-0-HOPELESS`):

> "This one never consumed a single attempt. The planner valued every
> future attempt at essentially zero and escalated straight to a human
> queue with one compliant message — instead of burning four attempts
> chasing money that was never coming. [Honest caveat if you have the
> extra 10 seconds: the trained model doesn't yet discriminate on decline
> cause — this scenario is constructed by feeding the same planner a
> genuinely near-zero probability directly, documented as a named
> limitation, not hidden.]"

**W3 — the killer demo, compliance-blocked execution** (click
`CYC-0-BLOCKED`):

> "This is the demo failure the plan is built to survive. The plan
> wanted to debit at a slot the notice hadn't actually cleared for yet —
> the policy engine denied it with `RBI_NOTICE_NOT_SATISFIED` before any
> money moved, the attempt was *not* consumed, and the plan re-solved
> around the shortened horizon. You can see the denial with its reason
> code right here in the ledger."

If you have time and nerve, do this one **live** instead of pre-seeded:
toggle the global kill switch on the Audit screen mid-narration, show a
pending attempt get denied with `GLOBAL_KILL_SWITCH`, then switch it back
off. It's the single most convincing 15 seconds in the whole demo because
it's not staged — you're doing it on camera.

## 3:45–4:35 — The evaluation, and the honesty paragraph

Screen: `http://localhost:8000/dashboard/benchmark.html`.

> "Here's the real number, from a sealed 500-mandate test split, touched
> exactly once, with a paired bootstrap confidence interval — not a
> hand-picked demo run. And here's the honest part: on this locked run,
> the naive fixed-schedule baseline actually recovered *more* gross
> rupees than MRE. We didn't hide that. We investigated it: the planner
> is pricing in a real cost — the risk that one more low-odds contact
> annoys a payer into revoking the mandate entirely — and this benchmark's
> single-cycle metric structurally can't credit that trade-off's benefit,
> because nothing here models a payer churning *later* from being
> over-contacted now. So the honest claim is narrower than 'recovers more
> money': it's RBI-compliant by construction, with a timing edge that's
> real but modest, and a hazard-avoidance rationale the benchmark can't
> yet measure."

Then, in the same breath, the pre-emption docs T item 4 says to concede
before anyone raises it:

> "And one more thing I'll say before you ask: I own the simulated world
> these numbers come from. That's a real limit on what this benchmark can
> claim — I can't tell you this generalises to real payer behaviour, only
> that the comparison is fair *within* this world, because every policy
> sees the identical realised draws. I'm not going to pretend otherwise."

If you built and are including P0b (the deterministic lookup-table
baseline, README 8): *"I also pre-built the objection 'a lookup table
could do this' — a plain (cause, day-of-month) table, same training data
as the model. On the dev split it's statistically indistinguishable from
the naive fixed schedule, and significantly behind the DP-planned policy.
The value is in the planning, not just having some probability estimate."*

## 4:35–5:00 — Safety model and close

> "Three independent layers protect the attempt cap: the policy engine's
> `authorize()` check, the database's own CHECK and UNIQUE constraints,
> and the simulator's independently-coded rail — I red-teamed all of them,
> including trying to force a fifth attempt through every path I could
> find. Every decision is on an append-only, tamper-evident audit chain.
> That's Mandate Recovery Engine — a constrained planner with a real
> stopping rule, for a country-specific, regulator-created problem, built
> and evaluated honestly. Thanks for watching."

If you have the extra 10 seconds and it fits your take, this is the
strongest closing line available — it's externally confirmed, not
self-claimed: *"Since I built this, Razorpay's own Agent Studio shipped a
production Subscription Recovery Agent solving this exact problem, with
guardrails described the same way this project's architecture works —
an LLM that drafts, and a deterministic layer that decides. I didn't
know that when I picked this problem. I think that's the strongest
evidence the problem is real."*

---

## Cut list if you're running long

Drop in this order, cheapest-to-lose first: the P0b callout (4:35 line),
the live kill-switch toggle (fall back to the pre-seeded W3 screenshot),
the W2 honest caveat's bracketed sentence, the architecture file-open
(just narrate over the Mermaid diagram instead). Never cut: the
honesty-paragraph concession in section 4, and the "what it's not" line
in section 2 — those are the two lines a skeptical judge is listening
for hardest.

## Things to have memorised, not read off a screen

The value function the planner solves, sketched from memory if asked
live (docs T item 9: "did you build this, or did an LLM? ... deriving
the value function on a whiteboard from memory ... is the
highest-probability live question"): at each state, compare the value of
attempting now (p·success_value + (1-p)·(continue - attempt_cost -
revoke_hazard_cost)) against stopping (E_manual) against waiting one slot
— `domain/planner.py` is the literal, tested implementation of exactly
this comparison at every (slot, attempts-remaining, notice-state), so
re-reading it once before recording is the actual rehearsal for this
question, not a separate thing to memorise.
