.DEFAULT_GOAL := help
PYTHON ?= python3

.PHONY: help setup build check test demo benchmark

help setup build check test demo benchmark:
	@$(PYTHON) scripts/dev.py $@
