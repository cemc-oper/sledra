// sledra-retention exposes fixture-only confirmation and cleanup for SLD-09.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"sledra.local/recorder/retention"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: sledra-retention init-fixture|ack-fixture|clean-fixture")
	}
	f := flag.NewFlagSet(args[0], flag.ContinueOnError)
	root := f.String("root", "", "private fixture root")
	segment := f.String("segment", "", "sealed segment path")
	sourceDays := f.Int("source-days", 0, "source retention days")
	evidenceDays := f.Int("evidence-days", 0, "evidence retention days")
	backlogDays := f.Int("backlog-days", 0, "maximum backlog days")
	if err := f.Parse(args[1:]); err != nil || f.NArg() != 0 || *root == "" {
		return fmt.Errorf("usage")
	}
	if args[0] == "init-fixture" {
		return retention.InitFixture(*root)
	}
	if *segment == "" {
		return fmt.Errorf("segment_required")
	}
	m, err := retention.OpenFixture(*root, retention.Policy{SourceRetention: time.Duration(*sourceDays) * 24 * time.Hour, EvidenceRetention: time.Duration(*evidenceDays) * 24 * time.Hour, MaxBacklogAge: time.Duration(*backlogDays) * 24 * time.Hour})
	if err != nil {
		return err
	}
	switch args[0] {
	case "ack-fixture":
		r, e := m.Acknowledge(*segment)
		if e != nil {
			return e
		}
		return json.NewEncoder(os.Stdout).Encode(r)
	case "clean-fixture":
		return m.Cleanup(*segment)
	default:
		return fmt.Errorf("usage")
	}
}
