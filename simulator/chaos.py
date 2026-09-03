"""Injectable chaos configuration for the simulator (docs G.2 M4, M.1).

Two knobs, matched to failure-matrix rows that are correctness-critical
rather than merely inconvenient:

- `error_5xx_rate`: the rail is down. Caller must retry; the attempt's
  sequence number stays reserved (docs H.3) and nothing is recorded here.
- `timeout_rate`: the rail accepted the request but the outcome is
  genuinely unknown (docs M.1: "Never retry the debit. Mark
  outcome=unknown, reconcile by polling."). This is recorded — it consumes
  the attempt slot, unlike a 5xx.

Delivery-level chaos (duplicate / delayed / out-of-order webhooks) is a
property of event *ingestion* (Phase 3's worker/API), not of this rail, so
it isn't modelled here — the caller is free to call /execute more than once
with the same idempotency_key to exercise that, and idempotent replay is
handled by the store lookup in app.py regardless of chaos config.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class ChaosConfig:
    error_5xx_rate: float = 0.0
    timeout_rate: float = 0.0

    def __post_init__(self) -> None:
        rates = (("error_5xx_rate", self.error_5xx_rate), ("timeout_rate", self.timeout_rate))
        for name, value in rates:
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")


def roll_5xx(cfg: ChaosConfig, rng: random.Random) -> bool:
    return rng.random() < cfg.error_5xx_rate


def roll_timeout(cfg: ChaosConfig, rng: random.Random) -> bool:
    return rng.random() < cfg.timeout_rate
