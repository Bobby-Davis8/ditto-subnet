package probe

import (
	"context"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

type fakeObserver struct {
	applied  AppliedResourcePolicy
	expected ExpectedResourcePolicy
}

func (f fakeObserver) Observe(context.Context) (AppliedResourcePolicy, error) { return f.applied, nil }
func (f fakeObserver) Expected() ExpectedResourcePolicy                       { return f.expected }

func wiredResourceCollector() ResourceCollector {
	observers := map[string]map[string]ResourceObserver{}
	for _, class := range []string{"harness", "executor_authoring", "executor_grading"} {
		observers[class] = map[string]ResourceObserver{}
		for _, language := range catalog.Languages {
			observers[class][language] = fakeObserver{
				applied:  AppliedResourcePolicy{MemoryLimitBytes: 2 << 30, MemorySwapBytes: 0, CPUQuotaMillis: 2000, PidsLimit: 256, ScratchLimitBytes: 1 << 30, ReadonlyRootfs: true},
				expected: ExpectedResourcePolicy{MemoryLimitBytes: 2 << 30, CPUQuotaMillis: 2000, PidsLimit: 256},
			}
		}
	}
	return ResourceCollector{Observers: observers}
}

func TestResourceCollectorProducesMatchingCgroupObservations(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	phases, err := wiredResourceCollector().Collect(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	outcomes := cat.OutcomeSet()
	expects := map[string]catalog.Expectation{}
	for _, probe := range cat.Kinds["resource_enforcement"].Probes {
		expects[probe.ID] = probe.Expect
	}
	seen := 0
	for _, phase := range phases {
		for _, observation := range phase.Observations {
			decoded, err := decodeObserved(observation.Observed)
			if err != nil {
				t.Fatal(err)
			}
			matched, err := catalog.Evaluate(expects[observation.ID], decoded, catalog.SubordinateIDs{}, outcomes, cat.Tolerances)
			if err != nil {
				t.Fatalf("%s: %v", observation.ID, err)
			}
			if !matched {
				t.Fatalf("collected observation %s/%s did not match: %v", observation.ID, observation.Language, observation.Observed)
			}
			seen++
		}
	}
	// 3 classes x 4 languages x (4 cgroup + 1 rootfs) probes.
	if seen != 3*4*5 {
		t.Fatalf("collected %d observations, want %d", seen, 3*4*5)
	}
}

func TestResourceCollectorSwapObservationCatchesADisabledSwapCap(t *testing.T) {
	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	collector := wiredResourceCollector()
	// A container whose swap cap was not pinned to zero must be recomputed as
	// unmatched at the memory_swap_max probe.
	collector.Observers["harness"]["go"] = fakeObserver{
		applied:  AppliedResourcePolicy{MemoryLimitBytes: 2 << 30, MemorySwapBytes: 2 << 30, CPUQuotaMillis: 2000, PidsLimit: 256, ReadonlyRootfs: true},
		expected: ExpectedResourcePolicy{MemoryLimitBytes: 2 << 30, CPUQuotaMillis: 2000, PidsLimit: 256},
	}
	phases, err := collector.Collect(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	outcomes := cat.OutcomeSet()
	expects := map[string]catalog.Expectation{}
	for _, probe := range cat.Kinds["resource_enforcement"].Probes {
		expects[probe.ID] = probe.Expect
	}
	var swap map[string]any
	for _, phase := range phases {
		for _, observation := range phase.Observations {
			if observation.ID == "harness.memory_swap_max" && observation.Language == "go" {
				swap = observation.Observed
			}
		}
	}
	decoded, err := decodeObserved(swap)
	if err != nil {
		t.Fatal(err)
	}
	matched, err := catalog.Evaluate(expects["harness.memory_swap_max"], decoded, catalog.SubordinateIDs{}, outcomes, cat.Tolerances)
	if err != nil {
		t.Fatal(err)
	}
	if matched {
		t.Fatal("a non-zero swap cap must not match memory_swap_max")
	}
}
