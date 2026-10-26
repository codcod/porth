# Porth SMS Gateway - Development Makefile
.PHONY: help install dev-install clean test test-unit test-integration test-integration-sqlite test-coverage lint format check fmt perf perf-demo perf-demo-auth perf-demo-advanced docker-build image-smoke run config-check deps-update deps-lock build docs-check docs-build db-up migrate migration downgrade

# Default target
.DEFAULT_GOAL := help

# Variables
PYTHON := python
UV := uv
PROJECT_NAME := porth
SRC_DIR := src
TEST_DIR := tests
CONFIG_DIR := config

help: ## Show this help message
	@echo "Porth SMS Gateway - Development Commands"
	@echo "========================================"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Installation and Setup
install: check-uv ## Install dependencies
	@echo "Installing dependencies with uv..."
	$(UV) sync

dev-install: check-uv ## Install development dependencies and setup environment
	@echo "Installing development dependencies..."
	$(UV) sync --dev
	@echo "Development environment setup complete!"
	@echo ""
	@echo "To activate the virtual environment:"
	@echo "source .venv/bin/activate"

check-uv: ## Check if uv is installed
	@if ! command -v $(UV) >/dev/null 2>&1; then \
		echo "Error: uv is not installed. Please install uv first:"; \
		echo "curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		exit 1; \
	fi

# Cleaning
clean: ## Clean up build artifacts and cache
	@echo "Cleaning up..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ .coverage htmlcov/ .pytest_cache/ .ruff_cache/
	@echo "Cleanup complete"

# Testing
test: ## Run all tests
	@echo "Running all tests..."
	$(UV) run pytest $(TEST_DIR) -v

test-unit: ## Run unit and e2e tests (no database, no SMSC)
	@echo "Running unit tests..."
	$(UV) run pytest $(TEST_DIR)/unit $(TEST_DIR)/e2e -v

test-integration: ## Run integration tests only (PostgreSQL ones need make db-up; they use their own porth_test and porth_migrations_test databases)
	@echo "Running integration tests..."
	RUN_INTEGRATION_TESTS=1 $(UV) run pytest $(TEST_DIR)/integration -v

test-integration-sqlite: ## Run integration tests on a throwaway SQLite file
	@echo "Running integration tests on SQLite..."
	RUN_INTEGRATION_TESTS=1 TEST_PORTH_DB=sqlite+aiosqlite:///$$(mktemp -d)/porth_test.db $(UV) run pytest $(TEST_DIR)/integration -v

test-coverage: ## Run tests with coverage report
	@echo "Running tests with coverage..."
	$(UV) run pytest $(TEST_DIR) --cov=$(SRC_DIR) --cov-report=html --cov-report=term-missing -v
	@echo "Coverage report generated in htmlcov/"

# Code Quality
lint: ## Run ruff, the ruff format check and ty
	@echo "Running linter..."
	$(UV) run ruff check $(SRC_DIR) $(TEST_DIR) bin
	$(UV) run ruff format --check $(SRC_DIR) $(TEST_DIR) bin
	$(UV) run ty check $(SRC_DIR)

format: ## Format code with ruff
	@echo "Formatting code..."
	$(UV) run ruff format $(SRC_DIR) $(TEST_DIR) bin

fmt: ## Fix lint findings and format code with ruff
	$(UV) run ruff check --fix $(SRC_DIR) $(TEST_DIR) bin
	$(UV) run ruff format $(SRC_DIR) $(TEST_DIR) bin

format-check: ## Check code formatting without making changes
	@echo "Checking code formatting..."
	$(UV) run ruff format --check $(SRC_DIR) $(TEST_DIR) bin

type-check: ## Run type checking with ty
	@echo "Running type checks..."
	$(UV) run ty check $(SRC_DIR)

check: lint ## Run all code quality checks (lint runs them all)

# Running the application
run: ## Run the application on config/config.toml
	@echo "Starting Porth SMS Gateway..."
	$(UV) run bin/porth $(CONFIG_DIR)/config.toml

perf: ## Run the k6 /health check against a running porth (BASE_URL, default localhost:8080)
	k6 run $(TEST_DIR)/performance/k6/health.js

perf-demo: ## Run the k6 load through compose-demo.yaml (REST and Kannel sends; POST=1 for Kannel's POST form)
	k6 run $(TEST_DIR)/performance/k6/demo.js

perf-demo-auth: ## Run the k6 load through compose-demo.yaml with the compose-demo-auth.yaml overlay
	AUTH=1 k6 run $(TEST_DIR)/performance/k6/demo.js

perf-demo-advanced: ## Run the k6 load through compose-demo-advanced.yaml (all three SMSCs, REST, Kannel, MO)
	k6 run $(TEST_DIR)/performance/k6/demo-advanced.js

docker-build: ## Build the porth container image
	docker build -t $(PROJECT_NAME) .

image-smoke: ## Build the image and check it boots on SQLite
	docker build -t porth:smoke .
	@docker rm -f porth-smoke >/dev/null 2>&1 || true
	docker run -d --name porth-smoke -p 18080:8080 \
		-v $(CURDIR)/tests/fixtures/container.toml:/opt/porth/config/config.toml:ro \
		--tmpfs /var/lib/porth:uid=65532,mode=0700 porth:smoke
	@ok=; for i in $$(seq 30); do \
		curl -fs localhost:18080/health >/dev/null && ok=1 && break; \
		[ "$$(docker inspect -f '{{.State.Running}}' porth-smoke)" = true ] || break; \
		sleep 1; \
	done; \
	if [ -n "$$ok" ] && curl -fs localhost:18080/ready; then echo; status=0; \
	else echo "porth-smoke failed; its log:"; docker logs porth-smoke; status=1; fi; \
	docker rm -f porth-smoke >/dev/null; exit $$status

# Database (the DSN is the `db` setting in $(CONFIG_DIR)/config.toml, read as the
# gateway reads it)

db-up: ## Start porth's PostgreSQL (docker compose)
	docker compose up -d --wait postgres

migrate: ## Apply database migrations (a one-off step, before every start after an upgrade)
	$(UV) run alembic -x config=$(CONFIG_DIR)/config.toml upgrade head

migration: ## Generate a migration: make migration name="..."
	$(UV) run alembic -x config=$(CONFIG_DIR)/config.toml revision --autogenerate -m "$(name)"

downgrade: ## Revert migrations: make downgrade [rev=-1]
	$(UV) run alembic -x config=$(CONFIG_DIR)/config.toml downgrade $(or $(rev),-1)

# Configuration
config-check: ## Validate config/config.toml the way the gateway loads it
	$(UV) run bin/porth --check $(CONFIG_DIR)/config.toml
	@echo "$(CONFIG_DIR)/config.toml is valid"

# Dependencies
deps-update: ## Update dependencies to latest versions
	@echo "Updating dependencies..."
	$(UV) sync --upgrade

deps-lock: ## Update lock file
	@echo "Updating lock file..."
	$(UV) lock

deps-list: ## List installed dependencies
	@echo "Installed dependencies:"
	$(UV) pip list

shell: ## Start Python shell with project context
	@echo "Starting Python shell..."
	$(UV) run $(PYTHON) -c "import sys; sys.path.insert(0, '$(SRC_DIR)'); import $(PROJECT_NAME); print('Porth SMS Gateway shell ready')"

# Build and Package
build: clean ## Build the package
	@echo "Building package..."
	$(UV) build

# Documentation
docs-check: ## Validate the AsciiDoc user manual (broken includes/xrefs fail)
	snowball check

docs-build: ## Render the user manual to PDF + EPUB into dist/docs/
	snowball build -o dist/docs

# Monitoring and Health
health-check: ## Check application health
	@echo "Checking application health..."
	@curl -f http://localhost:8080/health 2>/dev/null && echo "✓ Application is healthy" || echo "✗ Application is not responding"

# Development workflow shortcuts
dev: dev-install ## Quick development setup
	@echo ""
	@echo "✓ Development environment ready!"
	@echo "Run 'make run' to start the application"

ci: install check test ## CI pipeline simulation
	@echo "✓ CI pipeline completed successfully"

pre-commit: format lint test-unit ## Pre-commit checks
	@echo "✓ Pre-commit checks passed"
