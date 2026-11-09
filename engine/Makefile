.PHONY: install test lint format typecheck demo serve check cov

install:
	uv sync --python 3.12

test:
	uv run pytest

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

serve:
	uv run scruple serve

check: lint typecheck test
