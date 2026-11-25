# NexusCall developer commands. Requires: docker, uv, pnpm.
SHELL := /bin/bash
PROFILE ?= core
PROFILES := $(foreach p,$(PROFILE),--profile $(p))
COMPOSE := docker compose
ALL_PROFILES := --profile core --profile crm --profile calls --profile email --profile platform --profile fakes

.PHONY: help env generate build up up-all down logs ps migrate seed lint typecheck test test-unit test-integration \
        test-web e2e openapi clean

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | sort

env: ## create .env with random dev-only secrets
	@python3 scripts/dev_env.py

generate: ## regenerate compose, traefik routes, keycloak realm
	uv run python scripts/gen_compose.py
	uv run python scripts/gen_traefik.py
	uv run python scripts/gen_keycloak_realm.py

build: ## build images for the selected PROFILE(s)
	$(COMPOSE) $(PROFILES) build

up: env ## start infra + PROFILE(s), e.g. make up PROFILE="core crm"
	$(COMPOSE) $(PROFILES) up -d --build

up-all: env ## start everything (needs ~8 GB RAM for Docker)
	$(COMPOSE) $(ALL_PROFILES) --profile obs up -d --build

down: ## stop all containers (keeps volumes)
	$(COMPOSE) $(ALL_PROFILES) --profile obs down

clean: ## stop and delete volumes (DESTROYS local data)
	$(COMPOSE) $(ALL_PROFILES) --profile obs down -v

logs: ## follow logs (SERVICE=name to filter)
	$(COMPOSE) $(ALL_PROFILES) logs -f $(SERVICE)

ps:
	$(COMPOSE) $(ALL_PROFILES) --profile obs ps

migrate: ## run all service migrations as owner roles (one-off job)
	$(COMPOSE) run --rm migrate

seed: ## load safe demo data (two tenants, dev users)
	$(COMPOSE) run --rm --entrypoint python migrate scripts/seed_dev.py

lint: ## ruff + eslint
	uv run ruff check packages services scripts tests
	pnpm --filter @nexuscall/web lint

typecheck: ## mypy + tsc
	uv run mypy packages/nexus_core/nexus_core packages/event_schemas/nexus_events
	pnpm --filter @nexuscall/web typecheck

test-unit: ## fast tests (no Docker)
	uv run pytest packages services tests -m "not integration" -q

test-integration: ## tests against real Postgres/RabbitMQ via testcontainers
	NEXUS_REQUIRE_DOCKER=1 uv run pytest packages services tests -m integration -q

test-web:
	pnpm --filter @nexuscall/web test

test: test-unit test-integration test-web ## all automated tests

e2e: ## Playwright end-to-end tests against a running stack
	pnpm --filter @nexuscall/web e2e

openapi: ## export OpenAPI snapshots to contracts/openapi
	uv run python scripts/export_openapi.py
