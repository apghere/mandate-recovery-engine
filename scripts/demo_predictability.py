"""Demo evidence for docs/SIGNAL_LEGITIMACY.md's predictability claim:
`balance_volatility` alone — a continuous, learned signal, not a
hand-coded segment classifier — already makes the success-probability
curve sharp for a "predictable" payer and flat for an "irregular" one,
which is what makes the planner behave more conservatively for the
latter without anyone telling it which payer is which.

Usage: `make demo-predictability`. Pure computation, no DB/network
needed — this is meant to be runnable live in a demo.
"""
from __future__ import annotations

from simulator.decline import funds_sufficiency_probability

MEAN_BALANCE = 10_000.0
AMOUNT = 8_000.0
DAYS_SINCE_CREDIT = (0, 7, 14, 21, 27)


def _curve(volatility: float) -> list[float]:
    return [
        funds_sufficiency_probability(
            mean_balance=MEAN_BALANCE, balance_volatility=volatility, days_since=d, amount=AMOUNT
        )
        for d in DAYS_SINCE_CREDIT
    ]


def main() -> None:
    predictable = _curve(volatility=0.2)
    irregular = _curve(volatility=1.4)

    header = "".join(f"day+{d:<6}" for d in DAYS_SINCE_CREDIT)
    print(f"{'':22}{header}")
    print(f"{'predictable (vol=0.2)':22}" + "".join(f"{p:<10.2f}" for p in predictable))
    print(f"{'irregular   (vol=1.4)':22}" + "".join(f"{p:<10.2f}" for p in irregular))
    print()
    print(f"predictable payer's spread (best - worst): {max(predictable) - min(predictable):.2f}")
    print(f"irregular payer's spread   (best - worst): {max(irregular) - min(irregular):.2f}")
    print()
    print(
        "Same model, same code path, no segment label anywhere — the predictable\n"
        "payer gets a sharp curve the planner will wait for; the irregular payer\n"
        "gets a flat one, which makes the planner's optimal-stopping math naturally\n"
        "more conservative about spending scarce attempts on blind retries.\n"
        "See docs/SIGNAL_LEGITIMACY.md for the full argument."
    )


if __name__ == "__main__":
    main()
