.PHONY: dev check lint typecheck test up down migrate seed replay-fixed bench

dev:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -e ".[dev]"

lint:
	.venv/bin/ruff check backend data simulator scripts tests

typecheck:
	.venv/bin/mypy

test:
	.venv/bin/pytest -q

check: lint typecheck test

up:
	docker compose up -d db
	@echo "waiting for db..."
	@until docker compose exec -T db pg_isready -U mre >/dev/null 2>&1; do sleep 0.5; done
	$(MAKE) migrate

down:
	docker compose down -v

migrate:
	.venv/bin/python -m scripts.migrate

seed:
	.venv/bin/python -m data.generator

replay-fixed:
	.venv/bin/python -m scripts.replay_fixed

bench:
	@echo "bench: not implemented yet (Phase 8 — paired multi-policy evaluation harness)"
	@echo "For the Phase 3 single-policy smoke replay, use: make replay-fixed"
	@exit 1
