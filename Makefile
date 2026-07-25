SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV      := .venv
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
ALEMBIC   := $(VENV)/bin/alembic
PYTEST    := $(VENV)/bin/pytest
PMVL      := $(VENV)/bin/pmvl
WEB       := apps/web

# This machine's TLS-inspecting proxy breaks PyPI; a domestic mirror is routed
# directly. Override on a normal network with: make setup PIP_INDEX=
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple
PIP_ARGS  := $(if $(PIP_INDEX),-i $(PIP_INDEX) --trusted-host $(shell echo $(PIP_INDEX) | awk -F/ '{print $$3}'),)

.PHONY: help setup setup-web dev api web worker ingest orderbooks rank arbitrage \
        score settle snapshot backtest test test-unit test-integration lint \
        migrate revision reset-db lock docker-up docker-down clean status

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------- setup
setup: ## Create venv, install Python packages, run migrations
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip setuptools wheel $(PIP_ARGS)
	$(PIP) install -q -r requirements.txt $(PIP_ARGS)
	$(PIP) install -q -e packages/shared -e packages/market-normalization \
	                  -e services/api -e services/worker $(PIP_ARGS)
	@mkdir -p data/raw data/processed
	$(ALEMBIC) upgrade head
	@echo "✓ setup complete. Next: make ingest && make rank"

setup-web: ## Install frontend dependencies
	cd $(WEB) && npm install

lock: ## Freeze the fully-resolved dependency set
	$(PIP) freeze --exclude-editable > requirements.lock.txt
	@echo "✓ requirements.lock.txt written"

# ----------------------------------------------------------------- run
dev: ## Run API (:8000) and web (:3000) together
	@echo "API  → http://localhost:8000/docs"
	@echo "Web  → http://localhost:3000"
	@trap 'kill 0' INT TERM; \
	 $(VENV)/bin/uvicorn pmvl_api.main:app --reload --port 8000 & \
	 (cd $(WEB) && npm run dev) & \
	 wait

api: ## Run the FastAPI service only
	$(VENV)/bin/uvicorn pmvl_api.main:app --reload --port 8000

web: ## Run the Next.js app only
	cd $(WEB) && npm run dev

worker: ## Run the scheduler (all recurring jobs)
	$(PMVL) schedule

# ----------------------------------------------------------------- pipeline
ingest: ## Fetch live markets + orderbooks from Kalshi and Polymarket
	$(PMVL) ingest

orderbooks: ## Refresh orderbooks for already-ingested markets
	$(PMVL) orderbooks

score: ## Run the fair-probability ensemble over ingested markets
	$(PMVL) score

rank: ## Score, rank, and publish Top-10 for 24h / 7d / 30d
	$(PMVL) rank

arbitrage: ## Run all five arbitrage scanners
	$(PMVL) arbitrage

settle: ## Sync settlement results and grade past recommendations
	$(PMVL) settle

snapshot: ## Write today's immutable recommendation snapshot
	$(PMVL) snapshot

backtest: ## Walk-forward backtest across all strategies
	$(PMVL) backtest

status: ## Show job health and row counts
	$(PMVL) status

# ----------------------------------------------------------------- quality
test: ## Run the full test suite
	$(PYTEST) -q

test-unit: ## Unit tests only (no network, no DB)
	$(PYTEST) -q -m "not integration"

test-integration: ## Integration tests (fixtures + sqlite, still no live network)
	$(PYTEST) -q -m integration

lint: ## Compile-check every Python module
	$(PY) -m compileall -q packages services tests && echo "✓ lint ok"

# ----------------------------------------------------------------- database
migrate: ## Apply migrations
	$(ALEMBIC) upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	$(ALEMBIC) revision --autogenerate -m "$(m)"

reset-db: ## Drop and rebuild the local database (destructive)
	rm -f data/pmvl.db data/pmvl.db-wal data/pmvl.db-shm
	$(ALEMBIC) upgrade head
	@echo "✓ database reset"

# ----------------------------------------------------------------- docker
docker-up: ## Start Postgres + API + worker + web in Docker
	docker compose up --build -d

docker-down: ## Stop the Docker stack
	docker compose down

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache $(WEB)/.next
	@echo "✓ cleaned"
