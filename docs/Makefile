.DEFAULT_GOAL := help
PYTHON ?= python3
UV ?= uv
UV_PYTHON ?= $(PYTHON)
OUTPUT_DIR ?= ../build
BUILD_ROOT := $(shell realpath -m -- "$(OUTPUT_DIR)")

export UV_OFFLINE := 1
export UV_PYTHON_DOWNLOADS := never
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: help check docs

help:
	@printf '%s\n' 'make check  Check documentation links' 'make docs  Build strict HTML in OUTPUT_DIR/docs/html'

check:
	cd .. && $(UV) run --locked --offline --no-python-downloads --python "$(UV_PYTHON)" python scripts/check_docs.py

docs: check
	cd .. && $(UV) run --locked --offline --no-python-downloads --python "$(UV_PYTHON)" python -m sphinx -n -W --keep-going -E -a -b html docs/source "$(BUILD_ROOT)/docs/html"
