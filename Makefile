.DEFAULT_GOAL := help
PYTHON ?= python3

.PHONY: help setup build check test demo benchmark package test-package test-platform docs

help setup build check test demo benchmark package test-package test-platform docs:
	@$(PYTHON) scripts/dev.py $@
