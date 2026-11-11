.PHONY: install test test-slow test-postgres lint format typecheck demo bench serve check cov \
	release db-up db-down db-migrate formal

# Local postgres from docker-compose.yml (needs POSTGRES_PASSWORD in env or .env).
DB_URL ?= postgresql://scruple@127.0.0.1:$${POSTGRES_PORT:-5432}/scruple

install:
	uv sync --python 3.12

# Parallel across every core (pytest-xdist); tests use tmp dirs, in-memory dbs and
# a postgres cluster per worker on a free port, so they are safe to run together.
PYTEST_PAR ?= -n auto --dist worksteal

test:
	uv run pytest $(PYTEST_PAR)

# Suites marked @pytest.mark.slow, with the larger hypothesis profile.
test-slow:
	HYPOTHESIS_PROFILE=thorough uv run pytest $(PYTEST_PAR) -m slow

# Storage contract and migrations on postgres (a throwaway local cluster, or
# SCRUPLE_TEST_POSTGRES_URL).
test-postgres:
	uv run pytest $(PYTEST_PAR) -m postgres

cov:
	uv run pytest --cov --cov-report=term-missing

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

demo:
	uv run scruple demo

# Wall-clock throughput of the hot paths on synthetic data, on this machine.
bench:
	uv run python scripts/bench.py

serve:
	uv run scruple serve

# Bounded model checking and induction (z3) of the dispute, ACH and reconciliation
# models; docs/FORMAL.md says what is proved. A few minutes.
formal:
	uv run python -m scruple.formal.run

check: lint typecheck test

# Build sdist and wheel into dist/ from a clean, fully checked tree. Prints the
# checksums; tagging and publishing stay manual.
release: check
	@git diff --quiet && git diff --cached --quiet || (echo "release: working tree is dirty" && exit 1)
	rm -rf dist
	uv build
	@echo "built scruple $$(uv run python -c 'from scruple.version import package_version as v; print(v())') at $$(git rev-parse --short HEAD)"
	@cd dist && sha256sum *

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down

db-migrate:
	SCRUPLE_DATABASE_URL=$(DB_URL) uv run alembic upgrade head
