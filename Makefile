# Makefile for Orcastra Dashboard
# This Makefile provides convenient commands for managing the development and production environment

.PHONY: help build build-nc up down restart logs logs-backend logs-frontend logs-redis ps clean prune status health test-backend test-frontend install-backend install-frontend dev-backend dev-frontend shell-backend shell-frontend redis-cli backup restore config version version-sync version-check version-set env-check pull update quick-start quick-restart quick-rebuild

# Variables
COMPOSE_FILE := docker-compose.yml
DOCKER_COMPOSE := $(shell which docker-compose 2>/dev/null)
ifeq ($(DOCKER_COMPOSE),)
	DOCKER_COMPOSE := docker compose
endif

# Load version from .env if exists, otherwise use default
ifneq (,$(wildcard .env))
    include .env
    export
endif
APP_VERSION ?= unknown
CONTAINER_PREFIX ?= dev-orcastra-dashboard

# Colors for output
COLOR_RESET = \033[0m
COLOR_BOLD = \033[1m
COLOR_GREEN = \033[32m
COLOR_YELLOW = \033[33m
COLOR_BLUE = \033[34m
COLOR_CYAN = \033[36m

##@ Help

help: ## Display this help message
	@echo "$(COLOR_BOLD)Orcastra Dashboard - Available Commands$(COLOR_RESET)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf ""} /^[a-zA-Z_-]+:.*?##/ { printf "  $(COLOR_CYAN)%-20s$(COLOR_RESET) %s\n", $$1, $$2 } /^##@/ { printf "\n$(COLOR_BOLD)%s$(COLOR_RESET)\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Build Commands

build: ## Build all Docker images
	@echo "$(COLOR_GREEN)Building Docker images...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build
	@echo "$(COLOR_GREEN)✓ Build complete$(COLOR_RESET)"

build-nc: ## Build all Docker images without cache
	@echo "$(COLOR_YELLOW)Building Docker images without cache...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build --no-cache
	@echo "$(COLOR_GREEN)✓ Build complete$(COLOR_RESET)"

build-backend: ## Build backend image only
	@echo "$(COLOR_GREEN)Building backend image...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build backend
	@echo "$(COLOR_GREEN)✓ Backend build complete$(COLOR_RESET)"

build-backend-nc: ## Build backend image without cache
	@echo "$(COLOR_YELLOW)Building backend image without cache...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build --no-cache backend
	@echo "$(COLOR_GREEN)✓ Backend build complete$(COLOR_RESET)"

build-frontend: ## Build frontend image only
	@echo "$(COLOR_GREEN)Building frontend image...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build frontend
	@echo "$(COLOR_GREEN)✓ Frontend build complete$(COLOR_RESET)"

build-frontend-nc: ## Build frontend image without cache
	@echo "$(COLOR_YELLOW)Building frontend image without cache...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) build --no-cache frontend
	@echo "$(COLOR_GREEN)✓ Frontend build complete$(COLOR_RESET)"

##@ Docker Compose Commands

up: ## Start all services
	@echo "$(COLOR_GREEN)Starting all services...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) up -d
	@echo "$(COLOR_GREEN)✓ All services started$(COLOR_RESET)"
	@make status

up-build: ## Start all services with build
	@echo "$(COLOR_GREEN)Starting all services with build...$(COLOR_RESET)"
	@APP_VERSION=$(APP_VERSION) $(DOCKER_COMPOSE) -f $(COMPOSE_FILE) up -d --build
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

test-backend: ## Run backend tests
	@echo "$(COLOR_GREEN)Running backend tests...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) exec backend pytest

test-frontend: ## Run frontend tests
	@echo "$(COLOR_GREEN)Running frontend tests...$(COLOR_RESET)"
	@$(DOCKER_COMPOSE) -f $(COMPOSE_FILE) exec frontend npm test

lint-backend: ## Lint backend code
	@echo "$(COLOR_GREEN)Linting backend code...$(COLOR_RESET)"
	@cd backend && pylint app/

lint-frontend: ## Lint frontend code
	@echo "$(COLOR_GREEN)Linting frontend code...$(COLOR_RESET)"
	@cd frontend && npm run lint

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

# Default target
.DEFAULT_GOAL := help
