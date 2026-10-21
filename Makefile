# Makefile for Orcastra CMP
# This Makefile provides convenient commands for managing the development and production environment

.PHONY: ci-local export-openapi release release-preflight release-build release-tag release-push release-verify setup-hooks help build build-nc up down restart logs logs-backend logs-frontend logs-redis ps clean prune status health test-backend test-frontend test-qa-artifacts lint-qa-artifacts install-backend install-frontend dev-backend dev-frontend shell-backend shell-frontend redis-cli backup restore config version version-sync version-check version-set env-check pull update quick-start quick-restart quick-rebuild

# Variables
COMPOSE_FILE := docker-compose.yml
DOCKER_COMPOSE := $(shell which docker-compose 2>/dev/null)
ifeq ($(DOCKER_COMPOSE),)
	DOCKER_COMPOSE := docker compose
endif

# Test and lint gates are hermetic and must not need deployment secrets just to let
# GNU make parse their recipes. Load the local deployment environment only when at
# least one requested goal can actually consume it.
ENV_INDEPENDENT_GOALS := test-backend test-frontend lint-backend lint-frontend test-qa-artifacts lint-qa-artifacts ci-local
LOAD_LOCAL_ENV := 1
ifneq ($(strip $(MAKECMDGOALS)),)
ifeq ($(strip $(filter-out $(ENV_INDEPENDENT_GOALS),$(MAKECMDGOALS))),)
    LOAD_LOCAL_ENV :=
endif
endif
ifeq ($(LOAD_LOCAL_ENV),1)
ifneq (,$(wildcard .env))
    include .env
    export
endif
endif
APP_VERSION ?= unknown
CONTAINER_PREFIX ?= dev-orcastra-dashboard

# Provenance stamped into every image. The Dockerfiles already declare the ARG and
# emit org.opencontainers.image.revision from it; nothing ever supplied the value,
# so every image built here carried an empty revision and could not say which
# commit it came from. That turned every "what is actually deployed" question into
# a hunt for a content marker that happened to differ between two commits.
# note: a tree with uncommitted changes gets a -dirty suffix. A bare sha would name
# a commit the image does not actually contain, which is worse than saying nothing.
BUILD_COMMIT := $(or $(BUILD_COMMIT),$(shell git rev-parse HEAD 2>/dev/null)$(shell git diff --quiet HEAD 2>/dev/null || echo -dirty))
BUILD_DATE := $(or $(BUILD_DATE),$(shell date -u +%Y-%m-%dT%H:%M:%SZ))
BUILD_ENV = APP_VERSION=$(APP_VERSION) BUILD_COMMIT=$(BUILD_COMMIT) BUILD_DATE=$(BUILD_DATE)

# Registry namespace for published images. One variable so a fork, a mirror or a staging
# registry is a single override instead of an edit in three recipes plus both compose files.
REGISTRY_REPO ?= svlct/orcastra-dashboard

# Colors for output
COLOR_RESET = \033[0m
COLOR_BOLD = \033[1m
COLOR_GREEN = \033[32m
COLOR_YELLOW = \033[33m
COLOR_BLUE = \033[34m
COLOR_CYAN = \033[36m

##@ Help

help: ## Display this help message
	@echo "$(COLOR_BOLD)Orcastra CMP - Available Commands$(COLOR_RESET)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf ""} /^[a-zA-Z_-]+:.*?##/ { printf "  $(COLOR_CYAN)%-20s$(COLOR_RESET) %s\n", $$1, $$2 } /^##@/ { printf "\n$(COLOR_BOLD)%s$(COLOR_RESET)\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Build Commands

build: ## Build all Docker images
	@echo "$(COLOR_GREEN)Building Docker images...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build
	@echo "$(COLOR_GREEN)✓ Build complete$(COLOR_RESET)"

build-nc: ## Build all Docker images without cache
	@echo "$(COLOR_YELLOW)Building Docker images without cache...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build --no-cache
	@echo "$(COLOR_GREEN)✓ Build complete$(COLOR_RESET)"

build-backend: ## Build backend image only
	@echo "$(COLOR_GREEN)Building backend image...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build backend
	@echo "$(COLOR_GREEN)✓ Backend build complete$(COLOR_RESET)"

build-backend-nc: ## Build backend image without cache
	@echo "$(COLOR_YELLOW)Building backend image without cache...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build --no-cache backend
	@echo "$(COLOR_GREEN)✓ Backend build complete$(COLOR_RESET)"

build-frontend: ## Build frontend image only
	@echo "$(COLOR_GREEN)Building frontend image...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build frontend
	@echo "$(COLOR_GREEN)✓ Frontend build complete$(COLOR_RESET)"

build-frontend-nc: ## Build frontend image without cache
	@echo "$(COLOR_YELLOW)Building frontend image without cache...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build --no-cache frontend
	@echo "$(COLOR_GREEN)✓ Frontend build complete$(COLOR_RESET)"

##@ Docker Compose Commands

up: ## Start all services
	@echo "$(COLOR_GREEN)Starting all services...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) up -d
	@echo "$(COLOR_GREEN)✓ All services started$(COLOR_RESET)"
	@make status

up-build: ## Start all services with build
	@echo "$(COLOR_GREEN)Starting all services with build...$(COLOR_RESET)"
	@$(BUILD_ENV) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) up -d --build
	@echo "$(COLOR_GREEN)✓ All services started$(COLOR_RESET)"
	@make status

down: ## Stop and remove all containers
	@echo "$(COLOR_YELLOW)Stopping all services...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) down
	@echo "$(COLOR_GREEN)✓ All services stopped$(COLOR_RESET)"

down-v: ## Stop and remove all containers with volumes
	@echo "$(COLOR_YELLOW)Stopping all services and removing volumes...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) down -v
	@echo "$(COLOR_GREEN)✓ All services stopped and volumes removed$(COLOR_RESET)"

restart: ## Restart all services
	@echo "$(COLOR_YELLOW)Restarting all services...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) restart
	@echo "$(COLOR_GREEN)✓ All services restarted$(COLOR_RESET)"

restart-backend: ## Restart backend service only
	@echo "$(COLOR_YELLOW)Restarting backend service...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) restart backend
	@echo "$(COLOR_GREEN)✓ Backend restarted$(COLOR_RESET)"

restart-frontend: ## Restart frontend service only
	@echo "$(COLOR_YELLOW)Restarting frontend service...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) restart frontend
	@echo "$(COLOR_GREEN)✓ Frontend restarted$(COLOR_RESET)"

restart-redis: ## Restart Redis service only
	@echo "$(COLOR_YELLOW)Restarting Redis service...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) restart redis
	@echo "$(COLOR_GREEN)✓ Redis restarted$(COLOR_RESET)"

stop: ## Stop all services without removing
	@echo "$(COLOR_YELLOW)Stopping all services...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) stop
	@echo "$(COLOR_GREEN)✓ All services stopped$(COLOR_RESET)"

start: ## Start previously stopped services
	@echo "$(COLOR_GREEN)Starting services...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) start
	@echo "$(COLOR_GREEN)✓ All services started$(COLOR_RESET)"

##@ Logs Commands

logs: ## Tail logs from all services
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs -f

logs-backend: ## Tail logs from backend service
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs -f backend

logs-frontend: ## Tail logs from frontend service
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs -f frontend

logs-redis: ## Tail logs from Redis service
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs -f redis

logs-last: ## Show last 100 lines of logs from all services
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs --tail=100

logs-backend-last: ## Show last 100 lines of logs from backend
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs --tail=100 backend

logs-frontend-last: ## Show last 100 lines of logs from frontend
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) logs --tail=100 frontend

##@ Status & Health Commands

ps: ## List all containers
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) ps

status: ## Show status of all services
	@echo "$(COLOR_BOLD)Service Status:$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

health: ## Check health status of all services
	@echo "$(COLOR_BOLD)Health Status:$(COLOR_RESET)"
	@echo ""
	@echo "$(COLOR_CYAN)Backend:$(COLOR_RESET)"
	@docker inspect --format='{{.State.Health.Status}}' $(CONTAINER_PREFIX)-backend 2>/dev/null || echo "Container not running"
	@echo ""
	@echo "$(COLOR_CYAN)Frontend:$(COLOR_RESET)"
	@docker inspect --format='{{.State.Health.Status}}' $(CONTAINER_PREFIX)-frontend 2>/dev/null || echo "Container not running"
	@echo ""
	@echo "$(COLOR_CYAN)Redis:$(COLOR_RESET)"
	@docker inspect --format='{{.State.Health.Status}}' $(CONTAINER_PREFIX)-redis 2>/dev/null || echo "Container not running"

##@ Development Commands

dev: ## Run all services in development mode (with logs)
	@echo "$(COLOR_GREEN)Starting all services in development mode...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) up

dev-backend: ## Run backend in development mode (local)
	@echo "$(COLOR_GREEN)Starting backend in development mode...$(COLOR_RESET)"
	@cd backend && uvicorn app.main:app --host 0.0.0.0 --port 4050 --reload

dev-frontend: ## Run frontend in development mode (local)
	@echo "$(COLOR_GREEN)Starting frontend in development mode...$(COLOR_RESET)"
	@cd frontend && npm run dev

shell-backend: ## Open shell in backend container
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) exec backend /bin/bash

shell-frontend: ## Open shell in frontend container
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) exec frontend /bin/sh

redis-cli: ## Open Redis CLI
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) exec redis redis-cli

##@ Installation Commands

install-backend: ## Install backend dependencies locally
	@echo "$(COLOR_GREEN)Installing backend dependencies...$(COLOR_RESET)"
	@cd backend && pip install -r requirements.txt
	@echo "$(COLOR_GREEN)✓ Backend dependencies installed$(COLOR_RESET)"

install-frontend: ## Install frontend dependencies locally
	@echo "$(COLOR_GREEN)Installing frontend dependencies...$(COLOR_RESET)"
	@cd frontend && npm install
	@echo "$(COLOR_GREEN)✓ Frontend dependencies installed$(COLOR_RESET)"

install-all: install-backend install-frontend ## Install all dependencies locally

##@ Testing Commands

# note: the runtime image does not ship pytest (it is in requirements-dev.txt, not
# requirements.txt), so the default target runs on the host. Override PYTEST to point at
# a specific interpreter, ex; PYTEST=.venv/bin/pytest make test-backend
PYTEST ?= python3 -m pytest

# important: the suite runs with every variable this Makefile sourced from .env UNSET, which is
# what "hermetic" above was always supposed to mean and did not.
#
# `include .env` plus `export` near the top of this file hands every value in .env to every
# recipe's environment, verbatim. Docker Compose strips the quotes around a value; GNU make does
# not. So a perfectly ordinary API_PREFIX="/api/v1" reached pydantic-settings as the nine
# characters "/api/v1 WITH the quotes, app/main.py builds the app at import time, and FastAPI's
# include_router asserts a prefix starts with "/". Two tests that import create_app failed to
# COLLECT, which reads as a broken test file rather than as a broken environment. CI has no .env,
# so CI stayed green and only the machine doing the work saw it.
#
# Unsetting the names rather than fixing the one that bit: any other quoted value is the same
# defect waiting, and a test suite that reads the developer's deployment settings is not a test
# suite. The public template is the source of variable names, so lint and test goals never need
# to open the secret-bearing local file. Values are never read from either file.
ENV_NAMES := $(shell sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p' .env.example 2>/dev/null | sort -u)
ENV_SCRUB := $(if $(ENV_NAMES),env $(foreach v,$(ENV_NAMES),-u $(v)),)

test-backend: ## Run backend tests (host, in a scrubbed environment)
	@echo "$(COLOR_GREEN)Running backend tests...$(COLOR_RESET)"
	@cd backend && $(ENV_SCRUB) $(PYTEST)

QA_PYTHON ?= python3

test-qa-artifacts: ## Run offline isolated-lab contracts without provisioning
	@$(ENV_SCRUB) $(QA_PYTHON) -B -m unittest discover -s scripts/qa-hardening -p '*test*.py' -v

lint-qa-artifacts: ## Lint isolated-lab helpers and their offline contracts
	@$(ENV_SCRUB) ruff check --no-cache scripts/qa-hardening

# The deployed image, which is what this target is meant to exercise. Falls back to the
# APP_VERSION tag when nothing is running.
BACKEND_IMAGE = $(shell docker inspect --format '{{.Config.Image}}' $(CONTAINER_PREFIX)-backend 2>/dev/null || echo $(CONTAINER_PREFIX)-backend:$(APP_VERSION))

# note: a production image deliberately ships neither the test tree nor pytest, so the suite
# cannot be run inside the live container. This mounts the source read-only into a throwaway
# container built from the deployed image, which is the thing actually worth proving: that the
# shipped interpreter and dependency set run the suite green. pytest writes its cache next to
# the source, so it is disabled against the read-only mount.
test-backend-docker: ## Run backend tests against the deployed image's dependency set
	@echo "$(COLOR_GREEN)Running backend tests against $(BACKEND_IMAGE)...$(COLOR_RESET)"
	@docker run --rm -v $(PWD)/backend:/src:ro -w /src --entrypoint sh $(BACKEND_IMAGE) \
		-c 'pip install --user -q -r requirements-dev.txt && python -m pytest -p no:cacheprovider'

# note: there is no frontend unit-test suite and package.json has no `test` script, so this
# target used to invoke a command that could not succeed. The checks that DO exist are a
# typecheck, a lint and three structural guards; the behavioural coverage lives in the
# out-of-tree Playwright harness. CONTRIBUTING.md points here, so it has to be honest.
test-frontend: ## Run the frontend checks that exist (typecheck + structural guards)
	@echo "$(COLOR_GREEN)Typechecking frontend...$(COLOR_RESET)"
	@cd frontend && npx tsc --noEmit
	@echo "$(COLOR_GREEN)Checking mutation coherence registry...$(COLOR_RESET)"
	@node scripts/checks/check-mutation-coherence.mjs
	@echo "$(COLOR_GREEN)Checking instance inventory freshness...$(COLOR_RESET)"
	@node scripts/checks/check-instance-freshness.mjs
	@echo "$(COLOR_GREEN)Checking instance specification and create workflows...$(COLOR_RESET)"
	@node scripts/checks/check-instance-specification-workflow.mjs
	@echo "$(COLOR_GREEN)Checking create request staging and cancellation...$(COLOR_RESET)"
	@node scripts/checks/check-instance-create-orchestration.mjs
	@echo "$(COLOR_GREEN)Checking durable image and ISO upload completion...$(COLOR_RESET)"
	@node scripts/checks/check-upload-terminal-durability.mjs
	@echo "$(COLOR_GREEN)Checking submit guards...$(COLOR_RESET)"
	@node scripts/checks/check-submit-guards.mjs
	@echo "$(COLOR_GREEN)Checking table scroll containers...$(COLOR_RESET)"
	@node scripts/checks/check-table-scroll.mjs .
	@echo "$(COLOR_GREEN)Checking table typography...$(COLOR_RESET)"
	@node scripts/checks/check-table-typography.mjs
	@echo "$(COLOR_GREEN)Checking unreachable-cluster banners...$(COLOR_RESET)"
	@node scripts/checks/check-unreachable-banner.mjs
	@echo "$(COLOR_GREEN)Checking node picker status coverage...$(COLOR_RESET)"
	@node scripts/checks/check-node-picker-status.mjs
	@echo "$(COLOR_GREEN)Checking poll jitter...$(COLOR_RESET)"
	@node scripts/checks/check-poll-jitter.mjs
	@echo "$(COLOR_GREEN)Checking WebSocket tickets...$(COLOR_RESET)"
	@node scripts/checks/check-ws-tickets.mjs
	@echo "$(COLOR_GREEN)Checking long-list selects...$(COLOR_RESET)"
	@node scripts/checks/check-long-list-selects.mjs
	@echo "$(COLOR_GREEN)Checking the banner role gate...$(COLOR_RESET)"
	@node scripts/checks/check-banner-role-gate.mjs
	@echo "$(COLOR_GREEN)Checking role gates...$(COLOR_RESET)"
	@node scripts/checks/check-role-gates.mjs
	@echo "$(COLOR_GREEN)Checking auth transition isolation...$(COLOR_RESET)"
	@node scripts/checks/check-auth-transition-isolation.mjs
	@echo "$(COLOR_GREEN)Checking Migration Center authority read errors...$(COLOR_RESET)"
	@node scripts/checks/check-migration-authority-read-errors.mjs
	@echo "$(COLOR_GREEN)Checking that CI runs the same guards...$(COLOR_RESET)"
	@node scripts/checks/check-guard-parity.mjs
	@echo "$(COLOR_GREEN)Checking tenant node-inventory isolation...$(COLOR_RESET)"
	@node scripts/checks/check-tenant-node-inventory.mjs
	@echo "$(COLOR_GREEN)Checking controlled status labels...$(COLOR_RESET)"
	@node scripts/checks/check-status-display.mjs
	@echo "$(COLOR_GREEN)Checking Technologies role split...$(COLOR_RESET)"
	@node scripts/checks/check-technologies-role-split.mjs
	@echo "$(COLOR_GREEN)Checking instance live Metrics...$(COLOR_RESET)"
	@node scripts/checks/check-instance-live-metrics.mjs
	@echo "$(COLOR_GREEN)Checking console reloads...$(COLOR_RESET)"
	@node scripts/checks/check-no-console-reload.mjs
	@echo "$(COLOR_GREEN)Checking console session project scope...$(COLOR_RESET)"
	@node scripts/checks/check-console-session-project-scope.mjs
	@echo "$(COLOR_GREEN)Checking searchparams Suspense boundaries...$(COLOR_RESET)"
	@node scripts/checks/check-searchparams-suspense.mjs
	@echo "$(COLOR_GREEN)Checking table filter coverage...$(COLOR_RESET)"
	@node scripts/checks/check-table-filters.mjs
	@echo "$(COLOR_GREEN)Checking filter URL round trip...$(COLOR_RESET)"
	@node scripts/checks/check-filter-url-roundtrip.mjs
	@echo "$(COLOR_GREEN)Checking URL writes...$(COLOR_RESET)"
	@node scripts/checks/check-url-writes.mjs
	@echo "$(COLOR_GREEN)Checking inventory links...$(COLOR_RESET)"
	@node scripts/checks/check-inventory-links.mjs
	@echo "$(COLOR_GREEN)Checking Node and Cluster cells...$(COLOR_RESET)"
	@node scripts/checks/check-node-link.mjs
	@echo "$(COLOR_GREEN)Checking icon button names...$(COLOR_RESET)"
	@node scripts/checks/check-icon-button-names.mjs
	@echo "$(COLOR_GREEN)Checking stray JSX attributes...$(COLOR_RESET)"
	@node scripts/checks/check-jsx-stray-attrs.mjs
	@echo "$(COLOR_GREEN)Checking truncation reveal...$(COLOR_RESET)"
	@node scripts/checks/check-truncation-reveal.mjs
	@echo "$(COLOR_GREEN)Checking heading case...$(COLOR_RESET)"
	@node scripts/checks/check-heading-case.mjs
	@echo "$(COLOR_GREEN)Checking dialog affordances...$(COLOR_RESET)"
	@node scripts/checks/check-dialog-affordances.mjs
	@echo "$(COLOR_GREEN)Checking semantic colour...$(COLOR_RESET)"
	@node scripts/checks/check-semantic-colour.mjs
	@echo "$(COLOR_GREEN)Checking personalization defaults...$(COLOR_RESET)"
	@node scripts/checks/check-personalization-defaults.mjs
	@echo "$(COLOR_GREEN)Checking the org branding target...$(COLOR_RESET)"
	@node scripts/checks/check-org-branding-target.mjs
	@echo "$(COLOR_GREEN)Checking Settings routes, cache and branding synchronization...$(COLOR_RESET)"
	@node scripts/checks/check-settings-hardening.mjs
	@echo "$(COLOR_GREEN)Checking the sign-in return path...$(COLOR_RESET)"
	@node scripts/checks/check-auth-callback.mjs
	@echo "$(COLOR_GREEN)Checking task journal pagination and duration behavior...$(COLOR_RESET)"
	@node scripts/checks/check-task-journal-behavior.mjs
	@echo "$(COLOR_GREEN)Checking the terminal theme...$(COLOR_RESET)"
	@node scripts/checks/check-terminal-theme.mjs
	@echo "$(COLOR_GREEN)Checking prose typography...$(COLOR_RESET)"
	@node scripts/checks/check-prose-typography.mjs
	@echo "$(COLOR_GREEN)Checking byte units...$(COLOR_RESET)"
	@node scripts/checks/check-byte-units.mjs
	@echo "$(COLOR_GREEN)Checking operator UI terminology...$(COLOR_RESET)"
	@node scripts/checks/check-ui-glossary.mjs
	@echo "$(COLOR_GREEN)Checking the documentation index...$(COLOR_RESET)"
	@node scripts/checks/check-doc-index.mjs
	@echo "$(COLOR_GREEN)✓ Frontend checks passed$(COLOR_RESET)"

# Hosted CI runs only on pull requests and by hand, so this is the gate before a direct push: every
# job of .github/workflows/ci.yml, read from that file, on this machine. STAGES picks a subset of
# commits guards backend frontend e2e; E2E_PROJECTS the browser projects; CI_LOCAL_BASE the commit
# range base when the branch has no upstream. Scrubbed like test-backend, and for the same reason.
ci-local: ## Run every CI job locally (the gate before a direct push)
	@$(ENV_SCRUB) bash scripts/ci-local.sh $(STAGES)

setup-hooks: ## Point git at the repo's hooks (run once per clone)
	@git config core.hooksPath .githooks
	@echo "$(COLOR_GREEN)✓ core.hooksPath set to .githooks$(COLOR_RESET)"
	@echo "  The commit-msg hook needs frontend deps: cd frontend && npm install"

# note: ruff, not pylint. There is no pylintrc anywhere in this repo, and unconfigured pylint on
# 42k lines of FastAPI and SQLAlchemy emits thousands of missing-docstring and
# too-many-arguments findings, so it could never be green without a curated disable-list. ruff's
# E9,F set is bug-shaped and is green today. Scope and rationale: backend/ruff.toml.
lint-backend: ## Lint backend code
	@echo "$(COLOR_GREEN)Linting backend code...$(COLOR_RESET)"
	@cd backend && ruff check app/

# note: the local eslint binary, not `next lint`. The latter is deprecated in Next 15 and passes
# eslintrc-era options that a newer ESLint rejects, so it fails for reasons unrelated to the
# code. Scope and rationale: frontend/.eslintrc.README.md.
# Scrubbed for the reason documented above test-backend: make hands .env values through with
# their quotes intact, and this target builds the app, so an unscrubbed API_PREFIX aborts it.
export-openapi: ## Regenerate the committed add-on API document in orcastra-docs
	@echo "$(COLOR_GREEN)Exporting the add-on API document...$(COLOR_RESET)"
	@$(ENV_SCRUB) .venv/bin/python scripts/export-public-openapi.py ../orcastra-docs/docs/extensions/orcastra-extensions-v1.yaml

lint-frontend: ## Lint frontend code
	@echo "$(COLOR_GREEN)Linting frontend code...$(COLOR_RESET)"
	@cd frontend && ./node_modules/.bin/eslint src --ext .ts,.tsx

##@ Cleanup Commands

clean: ## Remove all stopped containers and dangling images
	@echo "$(COLOR_YELLOW)Cleaning up Docker resources...$(COLOR_RESET)"
	@docker system prune -f
	@echo "$(COLOR_GREEN)✓ Cleanup complete$(COLOR_RESET)"

prune: ## Remove all unused Docker resources (containers, images, volumes, networks)
	@echo "$(COLOR_YELLOW)WARNING: This will remove all unused Docker resources!$(COLOR_RESET)"
	@read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		docker system prune -a --volumes -f; \
		echo "$(COLOR_GREEN)✓ Prune complete$(COLOR_RESET)"; \
	fi

clean-logs: ## Clean up container logs
	@echo "$(COLOR_YELLOW)Cleaning up container logs...$(COLOR_RESET)"
	@truncate -s 0 /var/lib/docker/containers/*/*-json.log 2>/dev/null || echo "No logs to clean or insufficient permissions"
	@echo "$(COLOR_GREEN)✓ Logs cleaned$(COLOR_RESET)"

##@ Backup & Restore Commands

backup-redis: ## Backup Redis data
	@echo "$(COLOR_GREEN)Backing up Redis data...$(COLOR_RESET)"
	@mkdir -p ./backup-files/redis
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) exec -T redis redis-cli SAVE
	@docker cp $(CONTAINER_PREFIX)-redis:/data/dump.rdb ./backup-files/redis/dump-$(shell date +%Y%m%d-%H%M%S).rdb
	@echo "$(COLOR_GREEN)✓ Redis backup complete$(COLOR_RESET)"

restore-redis: ## Restore Redis data (specify BACKUP_FILE=path/to/dump.rdb)
	@echo "$(COLOR_YELLOW)Restoring Redis data...$(COLOR_RESET)"
	@if [ -z "$(BACKUP_FILE)" ]; then \
		echo "$(COLOR_YELLOW)Please specify BACKUP_FILE=path/to/dump.rdb$(COLOR_RESET)"; \
		exit 1; \
	fi
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) stop redis
	@docker cp $(BACKUP_FILE) $(CONTAINER_PREFIX)-redis:/data/dump.rdb
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) start redis
	@echo "$(COLOR_GREEN)✓ Redis restore complete$(COLOR_RESET)"

##@ Utility Commands

version: ## Show application version
	@echo "$(COLOR_BOLD)Application Version:$(COLOR_RESET) $(APP_VERSION)"

version-sync: ## Sync version from .env to all project files (package.json, etc)
	@echo "$(COLOR_BOLD)Syncing version across all files...$(COLOR_RESET)"
	@python3 scripts/sync_version.py
	@echo "$(COLOR_GREEN)✓ Version sync complete$(COLOR_RESET)"

version-check: ## Check version consistency across all files
	@echo "$(COLOR_BOLD)Checking version consistency...$(COLOR_RESET)"
	@python3 scripts/sync_version.py --check

version-set: ## Set new version (usage: make version-set VERSION=1.2.3)
	@if [ -z "$(VERSION)" ]; then \
		echo "$(COLOR_YELLOW)Error: VERSION not specified$(COLOR_RESET)"; \
		echo "Usage: make version-set VERSION=1.2.3"; \
		exit 1; \
	fi
	@echo "$(COLOR_BOLD)Setting version to $(VERSION)...$(COLOR_RESET)"
	@sed -i 's/^APP_VERSION=.*/APP_VERSION=$(VERSION)/' .env
	@echo "$(COLOR_GREEN)✓ Updated .env with version $(VERSION)$(COLOR_RESET)"
	@$(MAKE) version-sync

env-check: ## Check if .env file exists
	@if [ ! -f .env ]; then \
		echo "$(COLOR_YELLOW)Warning: .env file not found!$(COLOR_RESET)"; \
		exit 1; \
	else \
		echo "$(COLOR_GREEN)✓ .env file exists$(COLOR_RESET)"; \
	fi

config: ## Validate $(DOCKER_COMPOSE) configuration
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) config

pull: ## Pull latest images
	@echo "$(COLOR_GREEN)Pulling latest images...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) pull
	@echo "$(COLOR_GREEN)✓ Images pulled$(COLOR_RESET)"

update: pull up-build ## Update and restart all services

##@ Vault PKI Commands

vault-status: ## Check Vault PKI certificate status
	@./scripts/vault_pki_manager.sh status

vault-renew: ## Renew Vault Intermediate CA certificate (interactive)
	@./scripts/vault_pki_manager.sh renew

vault-help: ## Show Vault PKI Manager help
	@./scripts/vault_pki_manager.sh help

##@ Quick Commands

quick-start: env-check build up ## Quick start: build and run all services
	@echo ""
	@echo "$(COLOR_GREEN)✓ Application started successfully!$(COLOR_RESET)"
	@echo ""
	@echo "$(COLOR_BOLD)Access URLs:$(COLOR_RESET)"
	@echo "  Frontend: $(COLOR_CYAN)http://localhost:2025$(COLOR_RESET)"
	@echo "  Backend:  $(COLOR_CYAN)http://localhost:4050$(COLOR_RESET)"
	@echo "  Redis:    $(COLOR_CYAN)localhost:6381$(COLOR_RESET)"
	@echo ""

quick-restart: down up ## Quick restart all services

quick-rebuild: down build-nc up ## Quick rebuild without cache and restart

##@ Release

RELEASE_SRC_BACKEND  = $(CONTAINER_PREFIX)-backend:$(APP_VERSION)
RELEASE_SRC_FRONTEND = $(CONTAINER_PREFIX)-frontend:$(APP_VERSION)
RELEASE_DST_BACKEND  = $(REGISTRY_REPO):backend-$(APP_VERSION)
RELEASE_DST_FRONTEND = $(REGISTRY_REPO):frontend-$(APP_VERSION)

# important: overwriting a tag that is already published is what made 1.0.0-RC4 name two
# different commits, a backend from one day beside a frontend from eight days earlier.
# It stays possible, but only when asked for by name.
RELEASE_ALLOW_OVERWRITE ?= 0

# Reads a provenance label straight off the registry manifest. No pull, so it is cheap
# enough to run on every release and safe against a tag this machine never built.
# note: exits non-zero when the tag does not exist, which is also how the overwrite guard
# tests for existence.
IMAGETOOLS_REV = docker buildx imagetools inspect --format '{{ index .Image.Config.Labels "org.opencontainers.image.revision" }}'
IMAGETOOLS_AT  = docker buildx imagetools inspect --format '{{ index .Image.Config.Labels "org.opencontainers.image.created" }}'

release: ## Build, tag, push and verify a release of $(APP_VERSION)
	@$(MAKE) release-preflight
	@$(MAKE) release-build BUILD_COMMIT=$(BUILD_COMMIT) BUILD_DATE=$(BUILD_DATE)
	@$(MAKE) release-tag BUILD_COMMIT=$(BUILD_COMMIT)
	@$(MAKE) release-push
	@$(MAKE) release-verify EXPECT_COMMIT=$(BUILD_COMMIT)
	@echo ""
	@echo "$(COLOR_BOLD)Released $(APP_VERSION) from $(BUILD_COMMIT)$(COLOR_RESET)"
	@echo "  $(RELEASE_DST_BACKEND)"
	@echo "  $(RELEASE_DST_FRONTEND)"
	@echo ""
	@echo "$(COLOR_BOLD)On the deployment VM:$(COLOR_RESET)"
	@echo "  set APP_VERSION=$(APP_VERSION) in .env, then"
	@echo "  docker compose -f docker-compose.prod.yml pull"
	@echo "  docker compose -f docker-compose.prod.yml up -d"

release-preflight: ## Check tree, version and registry before building a release
	@if [ -z "$(APP_VERSION)" ] || [ "$(APP_VERSION)" = "unknown" ] || [ "$(APP_VERSION)" = "latest" ]; then \
		echo "$(COLOR_YELLOW)APP_VERSION is '$(APP_VERSION)'.$(COLOR_RESET)"; \
		echo "  Set it first: make version-set VERSION=1.0.0-RC5"; \
		exit 1; \
	fi
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "$(COLOR_YELLOW)Working tree is not clean. A release has to name a commit that exists.$(COLOR_RESET)"; \
		git status --short; \
		exit 1; \
	fi
	@case "$(BUILD_COMMIT)" in \
		*-dirty) echo "$(COLOR_YELLOW)BUILD_COMMIT is $(BUILD_COMMIT); refusing to publish a commit nobody can check out.$(COLOR_RESET)"; exit 1 ;; \
		"") echo "$(COLOR_YELLOW)BUILD_COMMIT is empty; git rev-parse failed. Refusing to publish an unidentifiable image.$(COLOR_RESET)"; exit 1 ;; \
	esac
	@python3 scripts/sync_version.py --check >/dev/null 2>&1 || { \
		echo "$(COLOR_YELLOW)Version drift between .env and frontend/package.json.$(COLOR_RESET)"; \
		echo "  The frontend image bakes package.json, so this would ship a lie. Run: make version-sync"; \
		exit 1; \
	}
	@for ref in $(RELEASE_DST_BACKEND) $(RELEASE_DST_FRONTEND); do \
		if $(IMAGETOOLS_REV) $$ref >/dev/null 2>&1; then \
			if [ "$(RELEASE_ALLOW_OVERWRITE)" != "1" ]; then \
				echo "$(COLOR_YELLOW)REFUSING: $$ref is already published.$(COLOR_RESET)"; \
				echo "  published:   $$($(IMAGETOOLS_AT) $$ref 2>/dev/null)"; \
				echo "  from commit: $$($(IMAGETOOLS_REV) $$ref 2>/dev/null)"; \
				echo "  Everyone who already pulled this tag would keep a different image under"; \
				echo "  the same name, and nothing would tell them."; \
				echo "  Bump APP_VERSION, or re-run with RELEASE_ALLOW_OVERWRITE=1."; \
				exit 1; \
			fi; \
			echo "$(COLOR_YELLOW)WARNING: overwriting published tag $$ref$(COLOR_RESET)"; \
			echo "  was: $$($(IMAGETOOLS_REV) $$ref 2>/dev/null) at $$($(IMAGETOOLS_AT) $$ref 2>/dev/null)"; \
			echo "  now: $(BUILD_COMMIT)"; \
		fi; \
	done
	@echo "$(COLOR_GREEN)✓ Preflight passed: $(APP_VERSION) at $(BUILD_COMMIT)$(COLOR_RESET)"

# note: BUILD_COMMIT and BUILD_DATE are passed down explicitly so both images carry the SAME
# stamp. Left alone, each recursive make recomputes BUILD_DATE and the two halves of one
# release disagree about when it was cut.
# The two builds are separate steps because the frontend fails intermittently on the shared
# orcastra-builder with "DeadlineExceeded ... exporting to oci image format". Retry the
# frontend alone; do not reconfigure the builder, other work on this host shares it.
release-build: ## Build both images with one pinned provenance stamp
	@echo "$(COLOR_GREEN)Building $(APP_VERSION) at $(BUILD_COMMIT) ($(BUILD_DATE))...$(COLOR_RESET)"
	@$(MAKE) build-backend BUILD_COMMIT=$(BUILD_COMMIT) BUILD_DATE=$(BUILD_DATE)
	@$(MAKE) build-frontend BUILD_COMMIT=$(BUILD_COMMIT) BUILD_DATE=$(BUILD_DATE)

release-tag: ## Tag the local release images for $(REGISTRY_REPO)
	@for img in $(RELEASE_SRC_BACKEND) $(RELEASE_SRC_FRONTEND); do \
		docker image inspect $$img >/dev/null 2>&1 || { \
			echo "$(COLOR_YELLOW)Missing local image $$img. Run: make release-build$(COLOR_RESET)"; exit 1; }; \
		got=$$(docker image inspect $$img --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'); \
		if [ "$$got" != "$(BUILD_COMMIT)" ]; then \
			echo "$(COLOR_YELLOW)$$img carries revision '$$got', expected '$(BUILD_COMMIT)'.$(COLOR_RESET)"; \
			echo "  That is a stale image from an earlier build. Run: make release-build"; \
			exit 1; \
		fi; \
	done
	@docker tag $(RELEASE_SRC_BACKEND) $(RELEASE_DST_BACKEND)
	@docker tag $(RELEASE_SRC_FRONTEND) $(RELEASE_DST_FRONTEND)
	@echo "$(COLOR_GREEN)✓ Tagged $(RELEASE_DST_BACKEND)$(COLOR_RESET)"
	@echo "$(COLOR_GREEN)✓ Tagged $(RELEASE_DST_FRONTEND)$(COLOR_RESET)"

release-push: ## Push both release tags
	@echo "$(COLOR_GREEN)Pushing $(RELEASE_DST_BACKEND)...$(COLOR_RESET)"
	@docker push $(RELEASE_DST_BACKEND)
	@echo "$(COLOR_GREEN)Pushing $(RELEASE_DST_FRONTEND)...$(COLOR_RESET)"
	@docker push $(RELEASE_DST_FRONTEND)

# important: this reads what is ON THE REGISTRY after the push, not what is on this disk
# before it. A pre-push check passes while a half-finished push leaves the old manifest live,
# which is how 1.0.0-RC4 carried a backend from 2026-08-26 beside a frontend from 2026-08-18
# under one name for eight days.
#
#   make release-verify                                   verify $(APP_VERSION) against HEAD
#   make release-verify VERSION=1.0.0-RC3                 verify a past release, pair agreement only
#   make release-verify VERSION=1.0.0-RC3 EXPECT_COMMIT=<sha>
release-verify: ## Verify a published tag pair (VERSION=x.y.z EXPECT_COMMIT=sha)
	@set -u; \
	V="$(or $(VERSION),$(APP_VERSION))"; \
	B="$(REGISTRY_REPO):backend-$$V"; \
	F="$(REGISTRY_REPO):frontend-$$V"; \
	echo "$(COLOR_BOLD)Verifying published $$V$(COLOR_RESET)"; \
	for ref in $$B $$F; do \
		docker buildx imagetools inspect $$ref >/dev/null 2>&1 || { \
			echo "$(COLOR_YELLOW)FAIL: $$ref is not published$(COLOR_RESET)"; exit 1; }; \
	done; \
	BREV=$$($(IMAGETOOLS_REV) $$B 2>/dev/null || true); \
	FREV=$$($(IMAGETOOLS_REV) $$F 2>/dev/null || true); \
	if [ -z "$$BREV" ] || [ -z "$$FREV" ]; then \
		echo "$(COLOR_YELLOW)UNVERIFIABLE: $$V is published but carries no revision label.$(COLOR_RESET)"; \
		echo "  backend  label: $${BREV:-(none)}"; \
		echo "  frontend label: $${FREV:-(none)}"; \
		echo "  Images built before provenance stamping cannot say which commit they came from."; \
		echo "  Nothing is wrong with the tag; it simply cannot be checked."; \
		exit 1; \
	fi; \
	echo "  backend  $$BREV  ($$($(IMAGETOOLS_AT) $$B 2>/dev/null))"; \
	echo "  frontend $$FREV  ($$($(IMAGETOOLS_AT) $$F 2>/dev/null))"; \
	if [ "$$BREV" != "$$FREV" ]; then \
		echo "$(COLOR_YELLOW)FAIL: backend and frontend of $$V were built from different commits.$(COLOR_RESET)"; \
		exit 1; \
	fi; \
	EXPECT="$(or $(EXPECT_COMMIT),$(if $(VERSION),,$(BUILD_COMMIT)))"; \
	if [ -n "$$EXPECT" ]; then \
		if [ "$$BREV" != "$$EXPECT" ]; then \
			echo "$(COLOR_YELLOW)FAIL: published revision $$BREV does not match expected $$EXPECT$(COLOR_RESET)"; \
			exit 1; \
		fi; \
		echo "  matches the expected commit"; \
	else \
		echo "  no expected commit given, checked pair agreement only"; \
	fi; \
	echo "$(COLOR_GREEN)✓ $$V is one commit: $$BREV$(COLOR_RESET)"

##@ Deployment

deploy-package: ## Create deployment package (tar.gz) for on-prem installation
	@echo "$(COLOR_GREEN)Creating deployment package...$(COLOR_RESET)"
	@for f in config/fluent-bit/fluent-bit.conf config/fluent-bit/parsers.conf; do \
		[ -f "$$f" ] || { \
			echo "$(COLOR_YELLOW)Missing $$f$(COLOR_RESET)"; \
			echo "  config/ is gitignored, so a fresh clone does not carry it. Copy it from a"; \
			echo "  running deployment before packaging."; \
			exit 1; \
		}; \
	done
	@DEPLOY_DIR=orcastra-dashboard-$(APP_VERSION) && \
	rm -rf /tmp/$$DEPLOY_DIR && \
	mkdir -p /tmp/$$DEPLOY_DIR/config/fluent-bit && \
	mkdir -p /tmp/$$DEPLOY_DIR/config/opensearch-dashboards && \
	mkdir -p /tmp/$$DEPLOY_DIR/scripts && \
	cp docker-compose.prod.yml /tmp/$$DEPLOY_DIR/ && \
	cp .env.example /tmp/$$DEPLOY_DIR/ && \
	cp config/fluent-bit/fluent-bit.conf /tmp/$$DEPLOY_DIR/config/fluent-bit/ && \
	cp config/fluent-bit/parsers.conf /tmp/$$DEPLOY_DIR/config/fluent-bit/ && \
	cp config/opensearch-dashboards/access-logs-dashboard-v3.ndjson /tmp/$$DEPLOY_DIR/config/opensearch-dashboards/ && \
	cp config/opensearch-dashboards/audit-logs-dashboard-v3.ndjson /tmp/$$DEPLOY_DIR/config/opensearch-dashboards/ && \
	cp config/opensearch-dashboards/logs-overview-dashboard.ndjson /tmp/$$DEPLOY_DIR/config/opensearch-dashboards/ && \
	cp scripts/setup_opensearch_dashboards.sh /tmp/$$DEPLOY_DIR/scripts/ && \
	chmod +x /tmp/$$DEPLOY_DIR/scripts/*.sh && \
	printf '%s\n' \
		"Orcastra CMP $(APP_VERSION)" \
		"" \
		"Images this package expects:" \
		"  $(REGISTRY_REPO):backend-$(APP_VERSION)" \
		"  $(REGISTRY_REPO):frontend-$(APP_VERSION)" \
		"" \
		"Before upgrading an existing deployment, run the operator tooling your team keeps" \
		"on the deployment VM. It is deliberately not shipped here: see the upgrade runbook." \
		"" \
		"The install and upgrade guides are not bundled on purpose. A copy frozen at package" \
		"time is how the published deployment guide came to embed a compose file four releases" \
		"out of date. Read them at docs.orcastra.io instead." \
		> /tmp/$$DEPLOY_DIR/README.txt && \
	cd /tmp && tar -czf $$DEPLOY_DIR.tar.gz $$DEPLOY_DIR/ && \
	mv -f /tmp/$$DEPLOY_DIR.tar.gz $(CURDIR)/$$DEPLOY_DIR.tar.gz && \
	rm -rf /tmp/$$DEPLOY_DIR && \
	echo "$(COLOR_GREEN)✓ Deployment package created: $$DEPLOY_DIR.tar.gz$(COLOR_RESET)" && \
	echo "" && \
	echo "$(COLOR_BOLD)Package contents:$(COLOR_RESET)" && \
	echo "  docker-compose.prod.yml        production compose file" && \
	echo "  .env.example                   annotated configuration template" && \
	echo "  config/fluent-bit/             log pipeline config" && \
	echo "  config/opensearch-dashboards/  dashboard templates" && \
	echo "  scripts/                       OpenSearch dashboard setup" && \
	echo "  README.txt                     pinned image tags and where the guides live" && \
	echo "" && \
	echo "$(COLOR_YELLOW)Transfer to deployment VM:$(COLOR_RESET)" && \
	echo "  scp $$DEPLOY_DIR.tar.gz user@<VM_IP>:/opt/orcastra-dashboard/"

# Default target
.DEFAULT_GOAL := help
