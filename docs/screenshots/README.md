# Screenshots

Captured 2026-09-03 against a freshly-seeded demo (`make demo-seed`), via
real headless Chrome (CDP), not hand-picked crops — zero console errors
on any of the three screens (docs Day 6's definition of done: "the demo
path is clickable with no console errors"). Docs S.2's submission
package requirement. Re-captured after the frontend redesign (distinct
palette/typography, the plan-vs-counterfactual comparison rebuilt so the
MRE/fixed-schedule delta is visible rather than two identical boxes) —
the originals from 2026-09-02 are superseded, not kept alongside these.

- `case_list.png` — the case list (curated demo cases sorted to the top,
  40 real dev-split background cases below).
- `case_detail.png` — `CYC-0-RECOVERY` clicked open: the actual MRE plan
  timeline next to the fixed-schedule counterfactual, attempt intents
  with normalized cause, policy version. This is the flagship screen
  (docs Day 6: "this screen is the demo, build it first and best").
- `benchmark.png` — the locked test-split benchmark result, rendered
  from `reports/benchmark.json`, with the dynamic honesty note.
- `audit.png` — the tamper-evident audit chain (verified valid) and the
  kill-switch control.
