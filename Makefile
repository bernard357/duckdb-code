help:
	@echo "Common commands:"
	@echo "$$ make setup     - Check prerequisites, create the venv, install tooling"
	@echo "$$ make lint      - Run Ruff and Markdown lint checks"
	@echo "$$ make test      - Run the pytest suite"
	@echo "$$ make test-local - Self-parse: run duckdb-codegraph against its own source"
	@echo "$$ make format    - Format Python code with Ruff"
	@echo "$$ make build     - Build the wheel and sdist with uv"
	@echo "$$ make clean     - Remove build artifacts and caches"
	@echo "$$ make push      - Rebase onto main and push commits to git remote"
	@echo " ... and you have access to git, uv, ruff and pytest directly as well."

# combine lines of each target
.ONESHELL:
# ensure child processes get variables set in this Makefile
.EXPORT_ALL_VARIABLES:

# default DuckDB database path
DUCKDB_CODEGRAPH_DB_PATH ?= codegraph.duckdb
# default Python path for imports
PYTHONPATH ?= .
# source locations for static analysis
PYTHON_PATHS := src/ tests/ scripts/
# markdown files for static analysis
MARKDOWN_FILES := README.md AGENTS.md

setup: check-prerequisites setup-python setup-markdownlint

check-prerequisites:
	@echo "Checking prerequisites..."
	@command -v python3 >/dev/null || { echo "ERROR: python3 not found — install Python 3.12+"; exit 1; }
	@command -v uv >/dev/null || { echo "ERROR: uv not found — see https://docs.astral.sh/uv/"; exit 1; }
	@command -v npm >/dev/null || echo "WARNING: npm not found — needed for setup-markdownlint"

setup-python:
	@echo "Creating virtual environment with uv..."
	uv venv --clear $(if $(PYTHON_VERSION),--python $(PYTHON_VERSION),)
	@echo "Installing the package with the dev extra..."
	uv pip install --python .venv/bin/python -e ".[dev]"

setup-markdownlint:
	@command -v markdownlint >/dev/null && markdownlint --version || { \
		echo "Installing markdownlint-cli via npm..."; \
		npm install markdownlint-cli -g || { \
			echo "npm global install failed (often a permissions issue)."; \
			echo "Install markdownlint-cli yourself, e.g. 'npm install -g"; \
			echo "markdownlint-cli' with a user-writable npm prefix. Needed"; \
			echo "only for 'make lint-markdown'."; \
		}; \
	}

lint: lint-python lint-markdown

lint-python:
	@echo "Running Ruff linter..."
	.venv/bin/ruff check ${PYTHON_PATHS}
	@echo "Running Ruff format check..."
	.venv/bin/ruff format --check ${PYTHON_PATHS}
	@echo "Running mypy type check..."
	.venv/bin/mypy

lint-markdown:
	@echo "Validating Markdown files..."
	markdownlint ${MARKDOWN_FILES}

format:
	@echo "Formatting Python code with Ruff..."
	.venv/bin/ruff format ${PYTHON_PATHS}

test:
	@echo "Running tests..."
	.venv/bin/pytest -q

test-local:
	@echo "Self-parsing the package source with duckdb-codegraph..."
	.venv/bin/python scripts/test_local.py

build:
	@echo "Building wheel and sdist..."
	uv build

clean:
	@echo "Removing build artifacts and caches..."
	rm -rf dist build *.egg-info src/*.egg-info *.out
	rm -rf .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

rebase:
	git pull --rebase origin main

push: rebase
	git push
