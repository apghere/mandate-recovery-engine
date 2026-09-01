# ADR 0004 — Phase 6 dependency: anthropic; model choice: Claude Haiku 4.5

- **anthropic** (runtime): the official SDK, needed to call Claude for the
  decline-string normaliser (docs §K.2) and notice generator (docs §K.5) —
  the two places this project deliberately puts an LLM, both narrow,
  off-the-hot-path, and behind a deterministic validator.
- **Model: `claude-haiku-4-5`**, not the newest/most capable model. This is
  a documented, explicit choice from the source plan (docs §K.2: "Cheap,
  fast, sufficient for a 13-way constrained classification. Escalate to
  Sonnet only if measured macro-F1 is unacceptable — measure it, do not
  assume."), not a cost-driven downgrade made independently — the
  escalation path (measure first, then decide) is itself part of the
  design and is what `scripts/evaluate_normalizer.py` exists to support.

No credentials are required for the system to function correctly: an
absent `ANTHROPIC_API_KEY` is a designed-for degraded mode (falls to
dictionary-match → `UNKNOWN`, docs §M.1), not an error path bolted on
after the fact.
