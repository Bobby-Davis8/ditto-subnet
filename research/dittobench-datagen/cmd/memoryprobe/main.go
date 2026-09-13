// Command memoryprobe runs the memory-exposure audit (gen.AuditMemoryExposure)
// for one generated dataset: how many evidence-bound answers can be copied
// verbatim out of their declared evidence versus how many must be computed.
//
// Usage: memoryprobe -bench-version 13 -seed 41 -run-size full
//
// -bench-version defaults to the newest supported contract so the probe never
// silently re-pins to an old version.
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func main() {
	seed := flag.Int64("seed", 1, "dataset seed")
	runSize := flag.String("run-size", "full", "small, medium, or full")
	benchVersion := flag.Int("bench-version", protocol.NewestBenchVersion(), "benchmark contract to audit (v10 or later)")
	flag.Parse()

	if !protocol.SupportedBenchVersion(*benchVersion) {
		fmt.Fprintf(os.Stderr, "unsupported bench version %d\n", *benchVersion)
		os.Exit(1)
	}
	profile, ok := gen.ProfileForVersion(*runSize, *benchVersion)
	if !ok {
		fmt.Fprintf(os.Stderr, "unsupported run size %q\n", *runSize)
		os.Exit(1)
	}
	artifact, err := gen.GenerateDataset(*seed, profile, *benchVersion)
	if err != nil {
		fmt.Fprintln(os.Stderr, "memoryprobe:", err)
		os.Exit(1)
	}
	result, err := gen.AuditMemoryExposure(artifact)
	if err != nil {
		fmt.Fprintln(os.Stderr, "memoryprobe:", err)
		os.Exit(1)
	}
	fmt.Printf("bench v%d %s seed %d: transformed %d/%d = %.4f; verbatim %d/%d = %.4f\n",
		*benchVersion, *runSize, *seed, result.Transformed, result.Eligible, result.TransformedShare(), result.Verbatim, result.Eligible, result.VerbatimShare())
}
