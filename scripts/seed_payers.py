"""Load data.generator's payer population into Postgres (migrations/
0002_payers.sql). Needed once real cycles need real payer context
(credit_day, mean_balance, ...) to score planner slots via the trained
Phase 4 model — see app/ml/inference.py.

Usage: `make seed-payers` (after `make up`).
"""
from __future__ import annotations

from app.db import get_connection
from app.repo import upsert_payer

from data.generator import generate_population


def main() -> None:
    payers = generate_population()
    with get_connection() as conn:
        for p in payers:
            upsert_payer(
                conn,
                payer_id=p.payer_id,
                segment=p.segment,
                credit_day=p.credit_day,
                mean_balance=p.mean_balance,
                balance_volatility=p.balance_volatility,
                issuer_code=p.issuer_code,
                chronic_fail_propensity=p.chronic_fail_propensity,
                annoyance_sensitivity=p.annoyance_sensitivity,
                mandate_amount=p.mandate_amount,
                split=p.split,
            )
        conn.commit()
    print(f"seeded {len(payers)} payers")


if __name__ == "__main__":
    main()
