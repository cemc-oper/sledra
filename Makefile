.DEFAULT_GOAL := help
PYTHON ?= python3
UV ?= uv
UV_PYTHON ?= $(PYTHON)
OUTPUT_DIR ?= build
DIST_DIR ?= dist
CONFORMANCE_REPORT_DIR ?=
BUILD_ROOT := $(shell realpath -m -- "$(OUTPUT_DIR)")
DIST_ROOT := $(shell realpath -m -- "$(DIST_DIR)")
UV_PY := $(UV) run --locked --offline --no-python-downloads --python "$(UV_PYTHON)" python
GUARD := bash scripts/guard_protected.sh

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
.PHONY: _check _test _demo _benchmark _test-platform _test-package test-conformance _test-conformance

help:
	@printf '%s\n' \
	  'make setup         Prepare locked Python, Go and CI tools online' \
	  'make build         Build recorder tools and the analysis Python package' \
	  'make check         Check lock, formatting, lint, contracts, docs and CI' \
	  'make test          Build, then run Go, analysis and source regressions' \
	  'make test-conformance Run the local M2 core gate and V01-V22 scope matrix' \
	  'make docs          Check links and build strict HTML in OUTPUT_DIR/docs/html' \
	  'make demo          Run M1 normal/failure/retry and replay in a temporary directory' \
	  'make benchmark     Run M1 timing probe; set SLEDRA_BENCHMARK_OUT to save samples' \
	  'make test-platform Run local filesystem and cross-node simulation probes' \
	  'make package       Build Linux trial in OUTPUT_DIR/package and archive in DIST_DIR' \
	  'make test-package Accept an existing trial archive in isolation'

setup:
	UV_OFFLINE=0 $(UV) sync --locked --no-python-downloads --python "$(UV_PYTHON)"
	$(MAKE) -C analysis setup
	$(MAKE) -C recorder setup
	$(PYTHON) scripts/check_ci.py --setup

build:
	@$(GUARD) $(MAKE) -C recorder OUTPUT_DIR="$(BUILD_ROOT)" build
	@$(GUARD) $(MAKE) -C analysis OUTPUT_DIR="$(BUILD_ROOT)" build

check:
	@$(GUARD) $(MAKE) _check

_check:
	$(UV) lock --check --offline --no-python-downloads
	$(UV_PY) -m ruff format --check scripts
	$(UV_PY) -m ruff check .
	$(MAKE) -C analysis check
	$(UV_PY) -m ruff format --check tests/conformance
	$(MAKE) -C recorder check
	@find . -name '*.sh' -not -path './build/*' -not -path './dist/*' -not -path './test-results/*' -not -path './.venv/*' -exec bash -n '{}' +
	$(UV_PY) scripts/check_contracts.py
	$(MAKE) -C docs OUTPUT_DIR="$(BUILD_ROOT)" check
	$(PYTHON) scripts/check_ci.py

docs:
	@$(GUARD) $(MAKE) -C docs OUTPUT_DIR="$(BUILD_ROOT)" docs

test: build
	@$(GUARD) $(MAKE) _test

_test:
	$(MAKE) _test-conformance
	$(UV_PY) -m unittest discover -s tests/engineering -v
	$(UV_PY) -m unittest discover -s tests/package -v
	$(UV_PY) -m unittest discover -s tests/platform -v
	$(UV_PY) spec/verify.py
	$(UV_PY) spec/verify_event.py
	$(UV_PY) spec/verify_context.py
	$(UV_PY) tests/cli/verify.py
	$(UV_PY) tests/m1/verify.py
	$(UV_PY) tests/m1/replay.py tests/m1/results/handoff.json

test-conformance: build
	@$(GUARD) $(MAKE) _test-conformance

_test-conformance:
	$(UV) run --project analysis --locked --offline --no-python-downloads --python "$(UV_PYTHON)" python tests/conformance/verify.py $(if $(CONFORMANCE_REPORT_DIR),--report-dir "$(CONFORMANCE_REPORT_DIR)",)

demo: build
	@$(GUARD) $(MAKE) _demo

_demo:
	@set -eu; work=$$(mktemp -d); trap 'status=$$?; if [ "$$status" -ne 0 ]; then printf "Demo work retained: %s\n" "$$work" >&2; fi' EXIT; \
	  $(UV_PY) tests/m1/verify.py --handoff-out "$$work/handoff.json" && \
	  $(UV_PY) tests/m1/replay.py "$$work/handoff.json" && \
	  rm -rf "$$work"

benchmark: build
	@$(GUARD) $(MAKE) _benchmark

_benchmark:
	@set -eu; if [ -n "$${SLEDRA_BENCHMARK_OUT:-}" ]; then \
	  $(UV_PY) tests/m1/benchmark.py --samples "$${SLEDRA_BENCHMARK_SAMPLES:-20}" --out "$$SLEDRA_BENCHMARK_OUT"; \
	  else $(UV_PY) tests/m1/benchmark.py --samples "$${SLEDRA_BENCHMARK_SAMPLES:-20}"; fi

test-platform: build
	@$(GUARD) $(MAKE) _test-platform

_test-platform:
	@set -eu; work=$$(mktemp -d); trap 'status=$$?; if [ "$$status" -ne 0 ]; then printf "Platform work retained: %s\n" "$$work" >&2; fi' EXIT; \
	  $(UV_PY) tests/platform/probe.py --bin "$(BUILD_ROOT)/bin/sledra" --work-root "$$work" --samples 10 && \
	  $(UV_PY) tests/platform/cross_node.py simulate --bin "$(BUILD_ROOT)/bin/sledra" --work-root "$$work" --samples 8 && \
	  rm -rf "$$work"

package:
	@$(GUARD) env SLEDRA_BUILD_DIR="$(BUILD_ROOT)" SLEDRA_DIST_DIR="$(DIST_ROOT)" $(PYTHON) scripts/package_trial.py

test-package:
	@$(GUARD) $(MAKE) _test-package

_test-package:
	@set -eu; work=$$(mktemp -d); trap 'status=$$?; if [ "$$status" -ne 0 ]; then printf "Package acceptance work retained: %s\n" "$$work" >&2; fi' EXIT; \
	  SLEDRA_DIST_DIR="$(DIST_ROOT)" $(PYTHON) tests/package/verify.py --report-dir "$$work/acceptance" && \
	  chmod -R u+w "$$work" && rm -rf "$$work"
