.DEFAULT_GOAL := help
PYTHON ?= python3

.PHONY: help setup build check test demo benchmark package test-package

help setup build check test demo benchmark package test-package:
	@$(PYTHON) scripts/dev.py $@
