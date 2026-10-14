.DEFAULT_GOAL := help
PYTHON ?= python3
UV ?= uv
UV_PYTHON ?= $(PYTHON)
OUTPUT_DIR ?= build
DIST_DIR ?= dist
BUILD_ROOT := $(shell realpath -m -- "$(OUTPUT_DIR)")
DIST_ROOT := $(shell realpath -m -- "$(DIST_DIR)")
UV_PY := $(UV) run --locked --offline --no-python-downloads --python "$(UV_PYTHON)" python

export GOPROXY := off
export GOSUMDB := off
export GONOPROXY := none
export GOTOOLCHAIN := local
export UV_OFFLINE := 1
export UV_PYTHON_DOWNLOADS := never
export PYTHONDONTWRITEBYTECODE := 1
export CGO_ENABLED := 0
export SLEDRA_TEST_BIN_DIR := $(BUILD_ROOT)/bin

.PHONY: help setup build check test docs demo benchmark package test-package test-platform

help:
	@printf '%s\n' \
	  'make setup         Prepare locked Python, Go and CI tools online' \
	  'make build         Build five recorder tools in OUTPUT_DIR/bin' \
	  'make check         Check lock, formatting, lint, contracts, docs and CI' \
	  'make test          Build, then run Go and source regressions' \
	  'make docs          Check links and build strict HTML in OUTPUT_DIR/docs/html' \
	  'make demo          Run M1 normal/failure/retry and replay in a temporary directory' \
	  'make benchmark     Run M1 timing probe; set SLEDRA_BENCHMARK_OUT to save samples' \
	  'make test-platform Run local filesystem and cross-node simulation probes' \
	  'make package       Build Linux trial in OUTPUT_DIR/package and archive in DIST_DIR' \
	  'make test-package Accept an existing trial archive in isolation'

setup:
	UV_OFFLINE=0 $(UV) sync --locked --no-python-downloads --python "$(UV_PYTHON)"
	$(MAKE) -C recorder setup
	$(PYTHON) scripts/check_ci.py --setup

build:
	$(MAKE) -C recorder OUTPUT_DIR="$(BUILD_ROOT)" build

check:
	$(UV) lock --check --offline --no-python-downloads
	$(UV_PY) -m ruff format --check scripts
	$(UV_PY) -m ruff check .
	$(MAKE) -C recorder check
	@find . -name '*.sh' -not -path './build/*' -not -path './dist/*' -not -path './test-results/*' -not -path './.venv/*' -exec bash -n '{}' +
	$(UV_PY) scripts/check_contracts.py
	$(MAKE) -C docs OUTPUT_DIR="$(BUILD_ROOT)" check
	$(PYTHON) scripts/check_ci.py

docs:
	$(MAKE) -C docs OUTPUT_DIR="$(BUILD_ROOT)" docs

test: build
	$(MAKE) -C recorder test
	$(UV_PY) -m unittest discover -s tests/engineering -v
	$(UV_PY) -m unittest discover -s tests/package -v
	$(UV_PY) -m unittest discover -s tests/platform -v
	$(UV_PY) spec/verify.py
	$(UV_PY) spec/verify_event.py
	$(UV_PY) spec/verify_context.py
	$(UV_PY) tests/cli/verify.py
	$(UV_PY) tests/shell/verify.py
	$(UV_PY) tests/m1/verify.py
	$(UV_PY) tests/m1/replay.py tests/m1/results/handoff.json

demo: build
	@set -eu; work=$$(mktemp -d); \
	  $(UV_PY) tests/m1/verify.py --handoff-out "$$work/handoff.json" && \
	  $(UV_PY) tests/m1/replay.py "$$work/handoff.json" && \
	  rm -rf "$$work"

benchmark: build
	@set -eu; if [ -n "$${SLEDRA_BENCHMARK_OUT:-}" ]; then \
	  $(UV_PY) tests/m1/benchmark.py --samples "$${SLEDRA_BENCHMARK_SAMPLES:-20}" --out "$$SLEDRA_BENCHMARK_OUT"; \
	  else work=$$(mktemp -d); \
	  $(UV_PY) tests/m1/benchmark.py --samples "$${SLEDRA_BENCHMARK_SAMPLES:-20}" --out "$$work/benchmark.json" && rm -rf "$$work"; fi

test-platform: build
	@set -eu; work=$$(mktemp -d); \
	  $(UV_PY) tests/platform/probe.py --bin "$(BUILD_ROOT)/bin/sledra" --work-root "$$work" --report-dir "$$work/primitives" --samples 10 && \
	  $(UV_PY) tests/platform/cross_node.py simulate --bin "$(BUILD_ROOT)/bin/sledra" --work-root "$$work" --report-dir "$$work/cross-node" --samples 8 && \
	  rm -rf "$$work"

package:
	SLEDRA_BUILD_DIR="$(BUILD_ROOT)" SLEDRA_DIST_DIR="$(DIST_ROOT)" $(PYTHON) scripts/package_trial.py

test-package:
	@set -eu; work=$$(mktemp -d); \
	  SLEDRA_DIST_DIR="$(DIST_ROOT)" $(PYTHON) tests/package/verify.py --report-dir "$$work/acceptance" && \
	  chmod -R u+w "$$work" && rm -rf "$$work"
