.PHONY: dev check lint typecheck test up down migrate seed seed-payers replay-fixed replay-compare train demo-predictability demo-seed bench bench-sensitivity

dev:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -e ".[dev]"

lint:
	.venv/bin/ruff check backend data simulator scripts tests evaluation

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

seed-payers:
	.venv/bin/python -m scripts.seed_payers

replay-fixed:
	.venv/bin/python -m scripts.replay_fixed

replay-compare:
	.venv/bin/python -m scripts.replay_compare

train:
	.venv/bin/python -m scripts.train

demo-predictability:
	.venv/bin/python -m scripts.demo_predictability

demo-seed:
	.venv/bin/python -m scripts.demo_seed

bench:
	.venv/bin/python -m evaluation.runner --split dev

bench-sensitivity:
	.venv/bin/python -m evaluation.runner --split dev --sensitivity
