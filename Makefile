# Orcastra CMP: build, run, test and release commands. `make help` lists them.

.PHONY: ci-local setup-hooks help build build-nc up down restart logs logs-backend logs-frontend logs-redis ps clean prune status health test-backend test-frontend install-backend install-frontend dev-backend dev-frontend shell-backend shell-frontend redis-cli backup restore config version env-check pull update quick-start quick-restart quick-rebuild

# Variables
COMPOSE_FILE := docker-compose.yml
DOCKER_COMPOSE := $(shell which docker-compose 2>/dev/null)
ifeq ($(DOCKER_COMPOSE),)
	DOCKER_COMPOSE := docker compose
endif

# Test and lint goals are hermetic. Load the local deployment environment only when a
# requested goal uses it.
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

# Commit stamped into every image as org.opencontainers.image.revision. A tree with
# uncommitted changes gets a -dirty suffix, since a bare sha would name a commit the
# image does not contain.
BUILD_COMMIT := $(or $(BUILD_COMMIT),$(shell git rev-parse HEAD 2>/dev/null)$(shell git diff --quiet HEAD 2>/dev/null || echo -dirty))
BUILD_DATE := $(or $(BUILD_DATE),$(shell date -u +%Y-%m-%dT%H:%M:%SZ))
BUILD_ENV = APP_VERSION=$(APP_VERSION) BUILD_COMMIT=$(BUILD_COMMIT) BUILD_DATE=$(BUILD_DATE)

# Registry namespace for published images; override it for a fork, mirror or staging registry.
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

# The runtime image does not ship pytest (requirements-dev.txt), so tests run on the host.
# Override PYTEST for a specific interpreter, ex; PYTEST=.venv/bin/pytest make test-backend
PYTEST ?= python3 -m pytest

# important: tests run with every variable named in .env.example unset. `include .env` plus
# `export` hands .env values to recipes verbatim, and make keeps the quotes Compose strips:
# API_PREFIX="/api/v1" would reach the app with the quotes and break app creation. Names come
# from the public template, so test and lint goals never open the local .env.
ENV_NAMES := $(shell sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p' .env.example 2>/dev/null | sort -u)
ENV_SCRUB := $(if $(ENV_NAMES),env $(foreach v,$(ENV_NAMES),-u $(v)),)

test-backend: ## Run backend tests (host, in a scrubbed environment)
	@echo "$(COLOR_GREEN)Running backend tests...$(COLOR_RESET)"
	@cd backend && $(ENV_SCRUB) $(PYTEST)

# The deployed image, or the APP_VERSION tag when nothing is running.
BACKEND_IMAGE = $(shell docker inspect --format '{{.Config.Image}}' $(CONTAINER_PREFIX)-backend 2>/dev/null || echo $(CONTAINER_PREFIX)-backend:$(APP_VERSION))

# Runs the suite with the deployed image's interpreter and dependencies. The production image
# ships neither tests nor pytest, so the source is mounted read-only into a throwaway container
# (with the pytest cache disabled).
test-backend-docker: ## Run backend tests against the deployed image's dependency set
	@echo "$(COLOR_GREEN)Running backend tests against $(BACKEND_IMAGE)...$(COLOR_RESET)"
	@docker run --rm -v $(PWD)/backend:/src:ro -w /src --entrypoint sh $(BACKEND_IMAGE) \
		-c 'pip install --user -q -r requirements-dev.txt && python -m pytest -p no:cacheprovider'

# There is no frontend unit-test suite: this runs the typecheck and the structural guards.
# Browser coverage lives in frontend/e2e.
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
	@echo "$(COLOR_GREEN)Checking number typography...$(COLOR_RESET)"
	@node scripts/checks/check-number-typography.mjs
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
	@echo "$(COLOR_GREEN)Checking identity calls go through the edition seams...$(COLOR_RESET)"
	@node scripts/checks/check-identity-seams.mjs
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
	@echo "$(COLOR_GREEN)Checking IdP account settings link...$(COLOR_RESET)"
	@node scripts/checks/check-idp-urls.mjs
	@echo "$(COLOR_GREEN)Checking URL writes...$(COLOR_RESET)"
	@node scripts/checks/check-url-writes.mjs
	@echo "$(COLOR_GREEN)Checking inventory links...$(COLOR_RESET)"
	@node scripts/checks/check-inventory-links.mjs
	@echo "$(COLOR_GREEN)Checking Node and Cluster cells...$(COLOR_RESET)"
	@node scripts/checks/check-node-link.mjs
	@echo "$(COLOR_GREEN)Checking icon button names...$(COLOR_RESET)"
	@node scripts/checks/check-icon-button-names.mjs
	@echo "$(COLOR_GREEN)Checking card illustrations...$(COLOR_RESET)"
	@node scripts/checks/check-illustrations.mjs
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
	@echo "$(COLOR_GREEN)Checking capacity bars...$(COLOR_RESET)"
	@node scripts/checks/check-capacity-bars.mjs
	@echo "$(COLOR_GREEN)Checking personalization defaults...$(COLOR_RESET)"
	@node scripts/checks/check-personalization-defaults.mjs
	@echo "$(COLOR_GREEN)Checking the orca mark keeps its colours...$(COLOR_RESET)"
	@node scripts/checks/check-brand-mark-untinted.mjs
	@echo "$(COLOR_GREEN)Checking every Rebuild surface asks the same question...$(COLOR_RESET)"
	@node scripts/checks/check-rebuild-gate-parity.mjs
	@echo "$(COLOR_GREEN)Checking the org branding target...$(COLOR_RESET)"
	@node scripts/checks/check-org-branding-target.mjs
	@echo "$(COLOR_GREEN)Checking Settings routes, cache and branding synchronization...$(COLOR_RESET)"
	@node scripts/checks/check-settings-hardening.mjs
	@echo "$(COLOR_GREEN)Checking the sign-in return path...$(COLOR_RESET)"
	@node scripts/checks/check-auth-callback.mjs
	@echo "$(COLOR_GREEN)Checking token refresh under refresh-token rotation...$(COLOR_RESET)"
	@node scripts/checks/check-token-rotation.mjs
	@echo "$(COLOR_GREEN)Checking the middleware session cookie echo...$(COLOR_RESET)"
	@node scripts/checks/check-session-cookie-echo.mjs
	@echo "$(COLOR_GREEN)Checking who the API proxy speaks for...$(COLOR_RESET)"
	@node scripts/checks/check-api-proxy-auth.mjs
	@echo "$(COLOR_GREEN)Checking the nonce Content Security Policy...$(COLOR_RESET)"
	@node scripts/checks/check-csp-nonce.mjs
	@echo "$(COLOR_GREEN)Checking what a failed token refresh means...$(COLOR_RESET)"
	@node scripts/checks/check-auth-refresh-contract.mjs
	@echo "$(COLOR_GREEN)Checking the terminal and console close contract...$(COLOR_RESET)"
	@node scripts/checks/check-ws-close-contract.mjs
	@echo "$(COLOR_GREEN)Checking the access gate...$(COLOR_RESET)"
	@node scripts/checks/check-access-gate.mjs
	@echo "$(COLOR_GREEN)Checking the Vault notice...$(COLOR_RESET)"
	@node scripts/checks/check-vault-notice.mjs
	@echo "$(COLOR_GREEN)Checking the tenant term notice...$(COLOR_RESET)"
	@node scripts/checks/check-tenant-term-notice.mjs
	@echo "$(COLOR_GREEN)Checking the branding surfaces...$(COLOR_RESET)"
	@node scripts/checks/check-branding-surfaces.mjs
	@echo "$(COLOR_GREEN)Checking the release notice...$(COLOR_RESET)"
	@node scripts/checks/check-release-notice.mjs
	@echo "$(COLOR_GREEN)Checking the console guest actions...$(COLOR_RESET)"
	@node scripts/checks/check-console-actions.mjs
	@echo "$(COLOR_GREEN)Checking the SPICE client...$(COLOR_RESET)"
	@node scripts/checks/check-spice-client.mjs
	@echo "$(COLOR_GREEN)Checking where guest passwords and keys may go...$(COLOR_RESET)"
	@node scripts/checks/check-guest-credentials-secrets.mjs
	@echo "$(COLOR_GREEN)Checking task journal pagination and duration behavior...$(COLOR_RESET)"
	@node scripts/checks/check-task-journal-behavior.mjs
	@echo "$(COLOR_GREEN)Checking the terminal theme...$(COLOR_RESET)"
	@node scripts/checks/check-terminal-theme.mjs
	@node scripts/checks/check-terminal-output.mjs
	@echo "$(COLOR_GREEN)Checking dates and copy...$(COLOR_RESET)"
	@node scripts/checks/check-date-format.mjs
	@node scripts/checks/check-copy-standard.mjs
	@node scripts/checks/check-page-header-stacks.mjs
	@echo "$(COLOR_GREEN)Checking prose typography...$(COLOR_RESET)"
	@node scripts/checks/check-prose-typography.mjs
	@echo "$(COLOR_GREEN)Checking byte units...$(COLOR_RESET)"
	@node scripts/checks/check-byte-units.mjs
	@echo "$(COLOR_GREEN)Checking operator UI terminology...$(COLOR_RESET)"
	@node scripts/checks/check-ui-glossary.mjs
	@echo "$(COLOR_GREEN)Checking the documentation index...$(COLOR_RESET)"
	@node scripts/checks/check-doc-index.mjs
	@echo "$(COLOR_GREEN)Checking comments for change history...$(COLOR_RESET)"
	@node scripts/checks/check-comment-hygiene.mjs
	@echo "$(COLOR_GREEN)Checking for real host addresses...$(COLOR_RESET)"
	@node scripts/checks/check-ip-literals.mjs
	@echo "$(COLOR_GREEN)✓ Frontend checks passed$(COLOR_RESET)"

# Every job of .github/workflows/ci.yml on this machine; the gate before a direct push.
# STAGES picks a subset of commits guards backend frontend e2e, E2E_PROJECTS the browser
# projects, CI_LOCAL_BASE the commit range base. Environment scrubbed like test-backend.
ci-local: ## Run every CI job locally (the gate before a direct push)
	@$(ENV_SCRUB) bash scripts/ci-local.sh $(STAGES)

setup-hooks: ## Point git at the repo's hooks (run once per clone)
	@git config core.hooksPath .githooks
	@echo "$(COLOR_GREEN)✓ core.hooksPath set to .githooks$(COLOR_RESET)"
	@echo "  The commit-msg hook needs frontend deps: cd frontend && npm install"

# ruff with the E9,F rule set; scope and rationale in backend/ruff.toml.
lint-backend: ## Lint backend code
	@echo "$(COLOR_GREEN)Linting backend code...$(COLOR_RESET)"
	@cd backend && ruff check app/

# The local eslint binary: `next lint` is deprecated in Next 15 and passes eslintrc options
# newer ESLint rejects. Scope: frontend/.eslintrc.README.md.
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

# Default target
.DEFAULT_GOAL := help

##@ Orcastra Mini

# The Mini release; the CMP release under it is frontend/package.json (CMP's, unchanged).
MINI_VERSION ?= $(shell cat mini/VERSION 2>/dev/null)
MINI_REPO ?= svlct/orcastra-dashboard-mini
MINI_COMPOSE_FILE ?= docker-compose.mini.yml
MINI_ENV_FILE ?= .env.mini
MINI_PROJECT ?= orcastra-mini
MINI_COMPOSE = docker compose -p $(MINI_PROJECT) -f $(MINI_COMPOSE_FILE) --env-file $(MINI_ENV_FILE)

mini-build: ## Build the Mini images (MINI_REPO:backend-/frontend-MINI_VERSION) from this tree
	@test -n "$(MINI_VERSION)" || { echo "MINI_VERSION is empty"; exit 1; }
	docker build -t $(MINI_REPO):backend-$(MINI_VERSION) --build-arg APP_VERSION=$(MINI_VERSION) \
		--build-arg BUILD_COMMIT=$(BUILD_COMMIT) --build-arg BUILD_DATE=$(BUILD_DATE) backend
	docker build -t $(MINI_REPO):frontend-$(MINI_VERSION) --build-arg APP_VERSION=$(MINI_VERSION) \
		--build-arg BUILD_COMMIT=$(BUILD_COMMIT) --build-arg BUILD_DATE=$(BUILD_DATE) frontend

mini-init: ## Copy the Mini nginx and Vault templates into config/ (the deployment layout)
	@mkdir -p config/nginx/certs config/vault
	@cp -n mini/nginx/mini.conf config/nginx/mini.conf && echo "config/nginx/mini.conf written" || echo "config/nginx/mini.conf kept"
	@cp -n mini/vault/vault.hcl config/vault/vault.hcl && echo "config/vault/vault.hcl written" || echo "config/vault/vault.hcl kept"

mini-config: ## Validate the Mini compose file against MINI_ENV_FILE
	@$(MINI_COMPOSE) config --quiet && echo "Mini compose: valid"

mini-up: ## Start the Mini stack (MINI_PROJECT, MINI_ENV_FILE)
	@$(MINI_COMPOSE) up -d

mini-down: ## Stop the Mini stack, keeping its volumes
	@$(MINI_COMPOSE) down

mini-ps: ## Show the Mini stack
	@$(MINI_COMPOSE) ps

mini-logs: ## Follow the Mini backend and frontend logs
	@$(MINI_COMPOSE) logs -f --tail=100 backend frontend

# Optional local.mk for operator targets whose tooling lives outside this repository (releases,
# Vault PKI and similar). A plain clone has only the targets that work from the repository.
-include local.mk
