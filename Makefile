# ── Makefile for DispatchAgent ────────────────────────────────────────────────
#
# Usage:
#   make test                       Run the full test suite
#   make test-fast                  Run tests, stop on first failure
#   make test-verbose               Run tests with full output
#   make test-single TICKET=12345   Run the CLI dispatch test tool
#   make dry-run                    Start the portal in dry-run mode
#   make install                    Install Python dependencies
#   make install-dev                Install Python + dev dependencies
#   make lint                       Run ruff linter
#   make typecheck                  Run mypy type checks

.PHONY: test test-fast test-verbose test-single dry-run install install-dev lint typecheck help

# ── Python / venv detection ───────────────────────────────────────────────────
# On Linux/Mac use `python3`, on Windows use `python` or whatever's on PATH.
PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest

# ── Test targets ──────────────────────────────────────────────────────────────

## Run the full test suite
test:
	$(PYTEST) tests/ -v --tb=short

## Stop on first failure (-x) — fast feedback during development
test-fast:
	$(PYTEST) tests/ -x --tb=short

## Full verbose output with captured stdout
test-verbose:
	$(PYTEST) tests/ -v -s --tb=long

## Run the CLI dispatch tool against a live CW ticket (always dry-run)
## Usage: make test-single TICKET=12345
test-single:
ifndef TICKET
	$(error TICKET is not set. Usage: make test-single TICKET=12345)
endif
	$(PYTHON) scripts/test_dispatch.py --ticket-id $(TICKET)

## Run test-single with verbose tool I/O
test-single-verbose:
ifndef TICKET
	$(error TICKET is not set. Usage: make test-single-verbose TICKET=12345)
endif
	$(PYTHON) scripts/test_dispatch.py --ticket-id $(TICKET) --verbose

# ── App targets ───────────────────────────────────────────────────────────────

## Start the portal in dry-run mode (reads DRY_RUN from .env, overrides to true)
dry-run:
	DRY_RUN=true $(PYTHON) run.py

# ── Dependency management ─────────────────────────────────────────────────────

## Install production dependencies
install:
	$(PIP) install -r requirements.txt

## Install production + test/dev dependencies
install-dev:
	$(PIP) install -r requirements.txt -r requirements-dev.txt

# ── Code quality ──────────────────────────────────────────────────────────────

## Run ruff linter (fast, Rust-based)
lint:
	$(PYTHON) -m ruff check . --select=E,W,F,I

## Run mypy type checks on the src/ and app/ packages
typecheck:
	$(PYTHON) -m mypy src/ app/ --ignore-missing-imports --no-strict-optional

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "DispatchAgent — available make targets:"
	@echo ""
	@echo "  make test                     Run the full test suite"
	@echo "  make test-fast                Run tests, stop on first failure"
	@echo "  make test-verbose             Run tests with full stdout output"
	@echo "  make test-single TICKET=NNN   Test dispatcher against ticket NNN"
	@echo "  make dry-run                  Start portal in dry-run mode"
	@echo "  make install                  Install dependencies"
	@echo "  make install-dev              Install deps + dev/test extras"
	@echo "  make lint                     Run ruff linter"
	@echo "  make typecheck                Run mypy"
	@echo ""
