# Mandate Recovery Engine

"You get four attempts. Spend them well."

A recovery decision service for Indian recurring-payment mandates (UPI AutoPay / eNACH):
diagnose the decline cause, forecast calibrated success probability across every legally
permitted retry slot, plan the optimal sequence of at most four attempts and their required
24-hour notices — or decide to stop — then execute through an idempotent, fully audited
state machine.

Full design in the accompanying PRD / Implementation Strategy. This README will be filled
out per that doc's ten-question structure (§S.1) once the system is demoable.

**Status: Phase 1 in progress.** Start reading at `backend/app/domain/policy.py` and
`backend/app/domain/fsm.py` — the pure, correctness-critical core.

## Setup

```
make dev     # create venv, install deps
make check   # lint + typecheck + test
```
