.DEFAULT_GOAL := help
PYTHON ?= python3

.PHONY: help setup build check test demo benchmark package test-package test-platform

help setup build check test demo benchmark package test-package test-platform:
	@$(PYTHON) scripts/dev.py $@
