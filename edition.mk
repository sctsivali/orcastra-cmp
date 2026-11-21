# Orcastra Mini make targets, loaded by the Makefile's `-include edition.mk`. Kept out of the
# Makefile so a port of CMP's Makefile never meets them. Variables and colours the Makefile
# defines (BUILD_COMMIT, BUILD_DATE) are available here.

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
