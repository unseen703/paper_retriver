# Dev entry points. Every BUILD.md task verifies through one of these.
.PHONY: install dev test test-unit lint typecheck migrate eval load-arxiv

install:
	uv sync
	@if [ -f frontend/package.json ]; then cd frontend && npm install; \
	 else echo "frontend/package.json not present yet (arrives at R1.20) -- skipping npm"; fi

dev:
	uv run uvicorn app.main:app --reload --port 8000 --app-dir backend

test:
	uv run pytest

test-unit:
	uv run pytest backend/tests/unit

lint:
	uv run ruff check backend eval scripts
	uv run ruff format --check backend eval scripts

typecheck:
	uv run mypy

migrate:
	uv run alembic -c backend/migrations/alembic.ini upgrade head

eval:
	uv run python eval/run.py

load-arxiv:
	uv run python scripts/load_arxiv_meta.py
