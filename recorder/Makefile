.DEFAULT_GOAL := help
OUTPUT_DIR ?= ../build
BUILD_ROOT := $(shell realpath -m -- "$(OUTPUT_DIR)")
TOOLS ?= sledra,sledra-spec-check,sledra-event,sledra-spool,sledra-retention

export GOPROXY := off
export GOSUMDB := off
export GONOPROXY := none
export GOTOOLCHAIN := local
export CGO_ENABLED := 0

.PHONY: help setup build check test

help:
	@printf '%s\n' 'make build  Build recorder tools in OUTPUT_DIR/bin' 'make check  Check Go formatting and vet' 'make test   Run Go tests without test cache' 'make setup  Download pinned Go modules online'

setup:
	GOPROXY=$${GOPROXY_SETUP:-https://proxy.golang.org,direct} GOSUMDB=$${GOSUMDB_SETUP:-sum.golang.org} go mod download

build:
	SLEDRA_TARGET_GOOS="$$(go env GOOS)" SLEDRA_TARGET_GOARCH="$$(go env GOARCH)" GOOS="$$(go env GOHOSTOS)" GOARCH="$$(go env GOHOSTARCH)" go run ../scripts/build_recorder.go --output "$(BUILD_ROOT)" --tools "$(TOOLS)"

check:
	@test -z "$$(gofmt -l ../scripts/build_recorder.go $$(find . -name '*.go' -type f))" || { gofmt -l ../scripts/build_recorder.go $$(find . -name '*.go' -type f); exit 1; }
	go vet ./...

test:
	go test -count=1 ./...
