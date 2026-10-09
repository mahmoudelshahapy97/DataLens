# Common tasks. `make help` lists them.
#
# Everything CI does is available here under the same name, so a failure can be
# reproduced locally without reading the workflow file.
#
# Three directories, three ways in:
#
#   backend/    Python. `make dev-backend` runs uvicorn against your venv.
#   frontend/   Node. `make dev-frontend` runs Vite, proxying /api to the backend.
#   database/   SQL. `make migrate` applies it.
#
# Or `make up` for all of it in Docker.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python
COMPOSE ?= docker compose
NPM ?= npm

# Where the integration tests find PostgreSQL. Override for a different instance:
#   make test-integration DB_URL=postgresql://user:pass@host:5432/postgres
DB_URL ?= postgresql://postgres:postgres123@localhost:5432/postgres
APP_DB_URL ?= postgresql://postgres:postgres123@localhost:5432/vanna_app

# Where the browser tests find the running stack, and who they sign in as. The
# password has no default on purpose: `make password` prints the generated one, and
# a Makefile that guesses a credential is a Makefile that fails confusingly.
#   make screenshots E2E_PASSWORD=... E2E_TENANT=chinook
E2E_URL ?= http://localhost:3000
E2E_EMAIL ?= demo@example.com
E2E_PASSWORD ?=
E2E_TENANT ?= chinook

# The Q&A export spans workspaces by default, because a corpus drawn from one
# database teaches you about that database. `all` means every membership the
# account has; a comma-separated list narrows it.
QA_TENANT ?= all

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ setup ---

.PHONY: install
install:  ## Install backend dependencies into the active environment
	$(PYTHON) -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt

.PHONY: install-frontend
install-frontend:  ## Install frontend dependencies
	cd frontend && $(NPM) ci

# ----------------------------------------------------------------- checks ---

.PHONY: lint
lint:  ## ruff over the whole tree
	$(PYTHON) -m ruff check .

.PHONY: fix
fix:  ## ruff with --fix
	$(PYTHON) -m ruff check . --fix

.PHONY: types
types:  ## mypy over the application layer and the library core
	$(PYTHON) -m mypy backend/vanna_app backend/vanna/core --ignore-missing-imports

.PHONY: typecheck-frontend
typecheck-frontend:  ## tsc over the web components
	cd frontend && $(NPM) run typecheck

.PHONY: test
test:  ## Unit tests (no database needed)
	$(PYTHON) -m pytest -m "not integration" -q

.PHONY: plotly-bundle
plotly-bundle:  ## Rebuild the Plotly bundle embedded in exported dashboards
	@docker run --rm \
	  -v "$(CURDIR)/tools/plotly_export_bundle:/src:ro" \
	  -v "$(CURDIR)/backend/vanna/dashboards/vendor:/out" \
	  -w /build node:20-alpine sh -c '\
	    cp /src/package.json /src/entry.js . && \
	    npm install --no-audit --no-fund --silent && \
	    ./node_modules/.bin/esbuild entry.js --bundle --minify --format=iife \
	      --target=es2018 --define:global=window \
	      --define:process.env.NODE_ENV=\"production\" \
	      --outfile=/out/plotly-export.min.js'
	@# The figure builder is the browser's own, copied rather than reimplemented:
	@# the export and the dashboard page have to draw a tile the same way.
	cp frontend/public/assets/shared/tile-figure.js backend/vanna/dashboards/vendor/
	@ls -l backend/vanna/dashboards/vendor/

.PHONY: seed-config
seed-config:  ## Migrate, import backend/ into the config catalog, regenerate database/
	VANNA_APP_DATABASE_URL=$(APP_DB_URL) $(PYTHON) tools/seed_database.py

.PHONY: export-schema
export-schema:  ## Regenerate database/sql_schema.sql and database/seed/
	VANNA_APP_DATABASE_URL=$(APP_DB_URL) $(PYTHON) tools/export_sql_schema.py

.PHONY: test-integration
test-integration:  ## Integration tests (needs PostgreSQL at DB_URL)
	VANNA_TEST_DATABASE_URL=$(DB_URL) $(PYTHON) -m pytest -m integration -q

.PHONY: test-all
test-all:  ## Every test
	VANNA_TEST_DATABASE_URL=$(DB_URL) $(PYTHON) -m pytest -q

.PHONY: test-e2e
test-e2e:  ## Browser tests against a running stack (needs E2E_PASSWORD)
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	VANNA_E2E_URL=$(E2E_URL) VANNA_E2E_EMAIL=$(E2E_EMAIL) \
	VANNA_E2E_PASSWORD=$(E2E_PASSWORD) \
		$(PYTHON) -m pytest tests/e2e -m e2e -q

.PHONY: role-accounts
role-accounts:  ## Create the four QA accounts the role screenshots sign in as
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/provision_role_accounts.py --url $(E2E_URL) \
		--admin-email $(E2E_EMAIL) --password $(E2E_PASSWORD)

.PHONY: role-screenshots
role-screenshots:  ## Photograph the app as admin, analyst, viewer and an outsider
	VANNA_E2E_URL=$(E2E_URL) $(PYTHON) -m pytest tests/e2e/test_roles.py -m e2e -q
	@echo "Images in ./artifacts/{admin,analysis,viewer,user}"

.PHONY: demo-video
demo-video:  ## Record a walkthrough of the running app into artifacts/video
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/record_demo.py --url $(E2E_URL) --email $(E2E_EMAIL) \n		--password $(E2E_PASSWORD)
	@echo "Clips in ./artifacts/video"

.PHONY: screenshots
screenshots:  ## Photograph every screen into artifacts/ (needs E2E_PASSWORD)
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	VANNA_E2E_URL=$(E2E_URL) VANNA_E2E_EMAIL=$(E2E_EMAIL) \
	VANNA_E2E_PASSWORD=$(E2E_PASSWORD) \
		$(PYTHON) -m pytest tests/e2e/test_screenshots.py -m e2e -q
	@echo "Images in ./artifacts"

.PHONY: seed
seed:  ## Fill a workspace with demo content, so no screen is empty
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/seed_demo_data.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--tenant $(E2E_TENANT) --password $(E2E_PASSWORD) --clean

.PHONY: seed-questions
seed-questions:  ## Ask real questions through the chat, for real Q&A history (slow, costs LLM calls)
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/ask_demo_questions.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--tenant $(E2E_TENANT) --password $(E2E_PASSWORD)
	@echo "Tip: E2E_TENANT=all asks every workspace. Budget an hour or two."

.PHONY: provision
provision:  ## Apply domains.yml -- workspace rules and starter questions (idempotent)
	$(COMPOSE) exec backend python -m vanna_app.domains provision

.PHONY: provision-list
provision-list:  ## Show what domains.yml would provision, without applying it
	$(COMPOSE) exec backend python -m vanna_app.domains list

.PHONY: enable-packs
enable-packs:  ## Opt each workspace into its starter-library pack
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/enable_domain_packs.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--password $(E2E_PASSWORD)

.PHONY: revoke-packs
revoke-packs:  ## Remove those packs again, keeping any rule a workspace edited
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/enable_domain_packs.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--password $(E2E_PASSWORD) --revoke

.PHONY: retire-wrong-rules
retire-wrong-rules:  ## Switch off the rules naming columns the databases do not have
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/retire_wrong_rules.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--password $(E2E_PASSWORD)

.PHONY: seed-writes
seed-writes:  ## Ask the write bank (propose_write/confirm_write). Refused unless grants exist
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/ask_demo_questions.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--tenant $(E2E_TENANT) --password $(E2E_PASSWORD) --writes

.PHONY: grant-writes
grant-writes:  ## Grant DML on genre, playlist and customer (undo with make revoke-writes)
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/grant_demo_writes.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--tenant $(E2E_TENANT) --password $(E2E_PASSWORD)

.PHONY: revoke-writes
revoke-writes:  ## Put those tables back to read-only
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/grant_demo_writes.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--tenant $(E2E_TENANT) --password $(E2E_PASSWORD) --revoke

.PHONY: qa-json
qa-json:  ## Export Q&A history across every workspace to qa.json
	@test -n "$(E2E_PASSWORD)" || { echo "Set E2E_PASSWORD. 'make password' prints it."; exit 1; }
	$(PYTHON) tools/export_qa.py --url $(E2E_URL) --email $(E2E_EMAIL) \
		--tenant $(QA_TENANT) --password $(E2E_PASSWORD) --out qa.json

# text2sql-eval-toolkit lives beside this repository and wants Python >= 3.11 in
# its own venv: `pip install -e ../text2sql-eval-toolkit` there, then point
# TOOLKIT_PYTHON at it. EVAL_WORKSPACES says which workspace answers each suite
# database, e.g. "--workspace chinook=demo --workspace pagila=acme".
TOOLKIT_PYTHON ?= $(PYTHON)
EVAL_TARGET_HOST ?= postgresql://postgres:postgres123@localhost:5432
EVAL_PIPELINE ?= vanna-baseline
EVAL_WORKSPACES ?=

.PHONY: eval-toolkit-export
eval-toolkit-export:  ## Register the SQL suite and the toolkit's benchmarks for text2sql-eval-toolkit
	$(TOOLKIT_PYTHON) backend/evals/toolkit_bridge.py export --target-host $(EVAL_TARGET_HOST)

.PHONY: eval-toolkit
eval-toolkit:  ## Score the SQL suite with text2sql-eval-toolkit (live instance, costs LLM calls)
	@test -n "$(VANNA_API_TOKEN)" || { echo "Set VANNA_API_TOKEN to an analyst's API token."; exit 1; }
	@test -n "$(EVAL_WORKSPACES)" || { echo "Set EVAL_WORKSPACES, e.g. \"--workspace chinook=demo\"."; exit 1; }
	$(TOOLKIT_PYTHON) backend/evals/toolkit_bridge.py suite --pipeline-id $(EVAL_PIPELINE) \
		--target-host $(EVAL_TARGET_HOST) --token $(VANNA_API_TOKEN) --app-db $(APP_DB_URL) \
		--server $(E2E_URL) --judge backend/evals/toolkit_judge.yaml $(EVAL_WORKSPACES)

.PHONY: coverage
coverage:  ## Coverage over the application layer
	VANNA_TEST_DATABASE_URL=$(DB_URL) $(PYTHON) -m pytest -q \
		--cov=backend/vanna_app --cov-report=term-missing --cov-report=html

.PHONY: check
check: lint test  ## What CI runs before it needs a database

# --------------------------------------------------------------- dev loop ---
#
# Two processes, two terminals. The frontend proxies /api, /health and /ready to
# 127.0.0.1:8000, so the browser sees one origin exactly as it does behind nginx --
# which is what keeps session cookies working without a CORS exception.

.PHONY: dev-backend
dev-backend:  ## uvicorn with reload, from backend/ (activate your venv first)
	cd backend && $(PYTHON) -m uvicorn main:app --reload --port 8000

.PHONY: dev-frontend
dev-frontend:  ## Vite dev server on :3000, proxying the API
	cd frontend && $(NPM) run dev

.PHONY: build-frontend
build-frontend:  ## Type-check and bundle the UI into frontend/dist
	cd frontend && $(NPM) run build

# ------------------------------------------------------------------- run ---

.PHONY: up
up:  ## Build and start the stack
	$(COMPOSE) up --build -d

.PHONY: down
down:  ## Stop the stack, keeping volumes
	$(COMPOSE) down

.PHONY: clean
clean:  ## Stop the stack and delete its volumes (catalog, knowledge, index)
	$(COMPOSE) down -v

.PHONY: logs
logs:  ## Follow the backend log
	$(COMPOSE) logs -f backend

.PHONY: password
password:  ## Show the generated first-run administrator password
	@$(COMPOSE) logs backend | grep -A4 "First-run administrator" || \
		echo "Not found. It is printed once, on the first start of an empty install."

# -------------------------------------------------------------- database ---

.PHONY: migrate
migrate:  ## Apply pending migrations (in the running stack)
	$(COMPOSE) exec backend python -m vanna_app.migrate upgrade

.PHONY: migrate-status
migrate-status:  ## Show the schema version and anything pending
	$(COMPOSE) exec backend python -m vanna_app.migrate status

.PHONY: migrate-local
migrate-local:  ## Apply pending migrations from your venv, not the container
	cd backend && $(PYTHON) -m vanna_app.migrate upgrade

.PHONY: seal-secrets
seal-secrets:  ## Encrypt any datasource credentials still stored in plaintext
	$(COMPOSE) exec backend python -c \
		"import asyncio; from vanna_app.wiring import _build_services; \
		 from vanna_app.config import get_settings; \
		 s=_build_services(get_settings()); \
		 print(asyncio.run(s['directory'].seal_plaintext_urls()), 'sealed')"

# ------------------------------------------------------------ dependencies ---

.PHONY: lock
lock:  ## Regenerate backend/requirements.txt from requirements.in
	docker run --rm -v "$(PWD)/backend:/w" -w /w python:3.12-slim sh -c \
		"pip install -q pip-tools && pip-compile --quiet --strip-extras \
		   --output-file=requirements.txt requirements.in"

.PHONY: secret
secret:  ## Generate a value for VANNA_SECRET_KEY
	@$(PYTHON) -c "import secrets; print(secrets.token_urlsafe(48))"
