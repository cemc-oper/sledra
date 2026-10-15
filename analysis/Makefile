.DEFAULT_GOAL := help
PYTHON ?= python3
UV ?= uv
UV_PYTHON ?= $(PYTHON)
ANALYSIS_PYTHON := $(shell command -v "$(UV_PYTHON)")
OUTPUT_DIR ?= ../build
BUILD_ROOT := $(shell realpath -m -- "$(OUTPUT_DIR)")
UV_PY := $(UV) run --project .. --locked --offline --no-python-downloads --python "$(UV_PYTHON)" python

.PHONY: help setup build check test
help:
	@printf '%s\n' 'make setup  Prepare pinned analysis build backend' 'make build  Build analysis wheel and sdist in OUTPUT_DIR/analysis' 'make check  Check analysis lock, formatting and lint' 'make test   Run ingestion, recovery and causal projection and timing regressions'
setup:
	UV_OFFLINE=0 $(UV) sync --locked --no-python-downloads --python "$(ANALYSIS_PYTHON)"
build:
	$(UV) lock --check --offline --no-python-downloads
	$(UV) build --offline --no-python-downloads --python "$(ANALYSIS_PYTHON)" --out-dir "$(BUILD_ROOT)/analysis"
check:
	$(UV) lock --check --offline --no-python-downloads
	$(UV_PY) -m ruff format --check src tests
	$(UV_PY) -m ruff check src tests
test:
	$(UV) run --locked --offline --no-python-downloads --python "$(ANALYSIS_PYTHON)" python tests/verify.py
