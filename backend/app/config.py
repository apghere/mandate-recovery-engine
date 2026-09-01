"""Centralized environment configuration.

The only place `os.environ` is read directly, so `domain/` (and everything
else) stays free of ad-hoc env lookups — docs CLAUDE.md's "domain/ stays
pure: no network, no DB, no clock" extends naturally to "and nobody else
scatters os.environ.get() calls either."
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    simulator_url: str
    anthropic_api_key: str | None


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://mre:mre@localhost:5432/mre"
        ),
        simulator_url=os.environ.get("SIMULATOR_URL", "http://localhost:8001"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )
