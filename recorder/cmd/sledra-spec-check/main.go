package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/cemc-oper/sledra/recorder/internal/speccontract"
)

func main() {
	specDir := flag.String("spec-dir", "../spec/1.0.0", "local contract directory")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: sledra-spec-check [-spec-dir DIR] BUNDLE.json")
		os.Exit(2)
	}
	data, err := os.ReadFile(flag.Arg(0))
	if err != nil {
		report("schema_invalid", err.Error())
		return
	}
	value, err := speccontract.Decode(data)
	if err != nil {
		reportError(err)
		return
	}
	bundle, ok := value.(map[string]any)
	if !ok {
		report("schema_invalid", "bundle is not an object")
		return
	}
	checker, err := speccontract.NewChecker(*specDir)
	if err != nil {
		report("schema_invalid", err.Error())
		return
	}
	result, err := checker.CheckBundle(bundle)
	if err != nil {
		reportError(err)
		return
	}
	result["ok"] = true
	_ = json.NewEncoder(os.Stdout).Encode(result)
}

func reportError(err error) {
	if e, ok := err.(*speccontract.ContractError); ok {
		report(e.Code, e.Detail)
	} else {
		report("schema_invalid", err.Error())
	}
}

func report(code, detail string) {
	_ = json.NewEncoder(os.Stderr).Encode(map[string]any{"ok": false, "code": code, "detail": detail})
	os.Exit(1)
}
