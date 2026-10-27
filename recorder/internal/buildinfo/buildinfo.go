// Package buildinfo reports software provenance separately from wire contracts.
package buildinfo

import (
	"runtime"

	"github.com/cemc-oper/sledra/recorder/execution"
	"github.com/cemc-oper/sledra/recorder/internal/speccontract"
)

// Build tools set these strings with -ldflags -X. Plain go build remains usable
// and labels unavailable provenance explicitly rather than inventing a release.
var Version = "dev"
var Commit = "unknown"
var BuildTime = "unknown"
var SourceSHA256 = "unknown"

type Info struct {
	SoftwareVersion        string `json:"software_version"`
	Commit                 string `json:"commit"`
	BuildTime              string `json:"build_time"`
	SourceSHA256           string `json:"source_sha256"`
	GoVersion              string `json:"go_version"`
	GOOS                   string `json:"goos"`
	GOARCH                 string `json:"goarch"`
	ContractVersion        string `json:"contract_version"`
	ExecutionFormatVersion string `json:"execution_format_version"`
	ExecutionLayoutVersion int    `json:"execution_layout_version"`
}

func Current() Info {
	return Info{Version, Commit, BuildTime, SourceSHA256, runtime.Version(), runtime.GOOS, runtime.GOARCH, speccontract.ContractVersion, execution.FormatVersion, execution.LayoutVersion}
}
