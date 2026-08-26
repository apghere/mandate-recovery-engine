.PHONY: dev check lint typecheck test up down seed bench

dev:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -e ".[dev]"

lint:
	.venv/bin/ruff check backend tests

typecheck:
	.venv/bin/mypy

test:
	.venv/bin/pytest -q

check: lint typecheck test

up:
	docker compose up --build

down:
	docker compose down -v

seed:
	@echo "seed: not implemented yet (Phase 2 — data generator)"
	@exit 1

bench:
	@echo "bench: not implemented yet (Phase 8 — evaluation harness)"
	@exit 1
