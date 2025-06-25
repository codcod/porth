# Porth SMS Gateway - Development Makefile
.PHONY: help install dev-install clean test test-unit test-integration test-e2e test-coverage lint format check run dev-run config-check deps-update deps-lock build docker-build docker-run logs

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

dev-install: check-uv create-env ## Install development dependencies and setup environment
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

create-env: ## Create .env file if it doesn't exist
	@if [ ! -f .env ]; then \
		echo "Creating .env file..."; \
		echo "# Development environment variables" > .env; \
		echo "PORTH_CONFIG_FILE=config/development.yml" >> .env; \
		echo "PORTH_LOG_LEVEL=DEBUG" >> .env; \
		echo ".env file created"; \
	else \
		echo ".env file already exists"; \
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

test-unit: ## Run unit tests only
	@echo "Running unit tests..."
	$(UV) run pytest $(TEST_DIR)/unit -v

test-integration: ## Run integration tests only
	@echo "Running integration tests..."
	$(UV) run pytest $(TEST_DIR)/integration -v

test-e2e: ## Run end-to-end tests only
	@echo "Running end-to-end tests..."
	$(UV) run pytest $(TEST_DIR)/e2e -v

test-coverage: ## Run tests with coverage report
	@echo "Running tests with coverage..."
	$(UV) run pytest $(TEST_DIR) --cov=$(SRC_DIR) --cov-report=html --cov-report=term-missing -v
	@echo "Coverage report generated in htmlcov/"

# Code Quality
lint: ## Run linting with ruff
	@echo "Running linter..."
	$(UV) run ruff check $(SRC_DIR) $(TEST_DIR)

format: ## Format code with ruff
	@echo "Formatting code..."
	$(UV) run ruff format $(SRC_DIR) $(TEST_DIR)

format-check: ## Check code formatting without making changes
	@echo "Checking code formatting..."
	$(UV) run ruff format --check $(SRC_DIR) $(TEST_DIR)

type-check: ## Run type checking with mypy
	@echo "Running type checks..."
	$(UV) run mypy $(SRC_DIR)

check: lint format-check type-check ## Run all code quality checks

# Running the application
run: ## Run the application
	@echo "Starting Porth SMS Gateway..."
	$(UV) run $(PYTHON) -m $(PROJECT_NAME).main

dev-run: create-env ## Run the application in development mode
	@echo "Starting Porth SMS Gateway in development mode..."
	PORTH_CONFIG_FILE=config/development.yml $(UV) run $(PYTHON) -m $(PROJECT_NAME).main

# Configuration
config-check: ## Validate configuration files
	@echo "Checking configuration files..."
	@for config in $(CONFIG_DIR)/*.yml; do \
		echo "Validating $$config..."; \
		$(UV) run $(PYTHON) -c "import yaml; yaml.safe_load(open('$$config'))" || exit 1; \
	done
	@echo "All configuration files are valid"

config-dev: ## Use development configuration
	@echo "PORTH_CONFIG_FILE=config/development.yml" > .env.local
	@echo "PORTH_LOG_LEVEL=DEBUG" >> .env.local
	@echo "Development configuration set in .env.local"

config-test: ## Use test configuration
	@echo "PORTH_CONFIG_FILE=config/test.yml" > .env.local
	@echo "PORTH_LOG_LEVEL=DEBUG" >> .env.local
	@echo "Test configuration set in .env.local"

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

# Development utilities
logs: ## Show application logs (if running)
	@echo "Showing recent logs..."
	@if [ -f logs/porth.log ]; then \
		tail -f logs/porth.log; \
	else \
		echo "No log file found. Run 'make dev-run' first."; \
	fi

shell: ## Start Python shell with project context
	@echo "Starting Python shell..."
	$(UV) run $(PYTHON) -c "import sys; sys.path.insert(0, '$(SRC_DIR)'); import $(PROJECT_NAME); print('Porth SMS Gateway shell ready')"

# Build and Package
build: clean ## Build the package
	@echo "Building package..."
	$(UV) build

# Docker (optional - for future use)
docker-build: ## Build Docker image
	@echo "Building Docker image..."
	docker build -t $(PROJECT_NAME):latest .

docker-run: ## Run application in Docker
	@echo "Running application in Docker..."
	docker run -p 8080:8080 --env-file .env $(PROJECT_NAME):latest

# Database/Storage (for future use)
db-migrate: ## Run database migrations (placeholder)
	@echo "Database migrations not implemented yet"

# Monitoring and Health
health-check: ## Check application health
	@echo "Checking application health..."
	@curl -f http://localhost:8080/health 2>/dev/null && echo "✓ Application is healthy" || echo "✗ Application is not responding"

# Development workflow shortcuts
dev: dev-install config-dev ## Quick development setup
	@echo ""
	@echo "✓ Development environment ready!"
	@echo "Run 'make dev-run' to start the application"

ci: install check test ## CI pipeline simulation
	@echo "✓ CI pipeline completed successfully"

pre-commit: format lint test-unit ## Pre-commit checks
	@echo "✓ Pre-commit checks passed"
