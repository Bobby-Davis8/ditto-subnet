package probe

import (
	"context"
	"fmt"
	"sort"
)

// AppliedResourcePolicy is one container class+language's applied resource
// policy, read back through the production launch code. Every value is in the
// approved profile's own units.
type AppliedResourcePolicy struct {
	MemoryLimitBytes  int64
	MemorySwapBytes   int64
	CPUQuotaMillis    int64
	PidsLimit         int64
	ScratchLimitBytes int64
	ReadonlyRootfs    bool
}

// ExpectedResourcePolicy is the approved profile a class+language's container
// must equal. The runner records cgroup==profile only when the applied value
// equals the expected one; it never copies the profile into the observation.
type ExpectedResourcePolicy struct {
	MemoryLimitBytes int64
	CPUQuotaMillis   int64
	PidsLimit        int64
}

// ResourceObserver observes one class+language container's applied resource
// policy by launching it through the production executor or sandbox code.
type ResourceObserver interface {
	Observe(ctx context.Context) (AppliedResourcePolicy, error)
	// Expected is the approved profile the applied policy must equal.
	Expected() ExpectedResourcePolicy
}

// ResourceCollector holds the observers the caller wired for each container
// class and language. Missing entries are simply not collected in this PR; the
// measured burner probes (OOM, CPU, pids, scratch, nofile, log) and the harness
// live cgroup read require the in-image helper that a later PR ships and are
// reported as pending, never fabricated.
type ResourceCollector struct {
	// Observers is class -> language -> observer.
	Observers map[string]map[string]ResourceObserver
}

// Kind implements Runner.
func (ResourceCollector) Kind() string { return "resource_enforcement" }

// Collect launches each wired class+language container through the production
// launch code and records its cgroup profile_equal, swap-zero and read-only
// rootfs observations. It returns one phase per catalog phase that has at least
// one collected probe; the record assembler still refuses an incomplete record,
// so a partial collection is visible rather than silently accepted.
func (r ResourceCollector) Collect(ctx context.Context, session *Session) ([]Phase, error) {
	cgroup := Phase{Name: "cgroup"}
	measured := Phase{Name: "measured"}
	classes := make([]string, 0, len(r.Observers))
	for class := range r.Observers {
		classes = append(classes, class)
	}
	sort.Strings(classes)
	for _, class := range classes {
		languages := make([]string, 0, len(r.Observers[class]))
		for language := range r.Observers[class] {
			languages = append(languages, language)
		}
		sort.Strings(languages)
		for _, language := range languages {
			observer := r.Observers[class][language]
			applied, err := observer.Observe(ctx)
			if err != nil {
				return nil, fmt.Errorf("probe: resource observation for %s/%s: %w", class, language, err)
			}
			expected := observer.Expected()
			cgroup.Observations = append(cgroup.Observations,
				resourceObservation(class, "memory_max", language, map[string]any{
					"cgroup": applied.MemoryLimitBytes, "profile": expected.MemoryLimitBytes,
				}),
				resourceObservation(class, "memory_swap_max", language, map[string]any{
					"swap_max_bytes": applied.MemorySwapBytes,
				}),
				resourceObservation(class, "cpu_quota", language, map[string]any{
					"cgroup": applied.CPUQuotaMillis, "profile": expected.CPUQuotaMillis,
				}),
				resourceObservation(class, "pids_max", language, map[string]any{
					"cgroup": applied.PidsLimit, "profile": expected.PidsLimit,
				}),
			)
			measured.Observations = append(measured.Observations,
				resourceObservation(class, "rootfs_read_only", language, map[string]any{
					"outcome": readonlyOutcome(applied.ReadonlyRootfs),
				}),
			)
		}
	}
	phases := make([]Phase, 0, 2)
	if len(cgroup.Observations) > 0 {
		phases = append(phases, cgroup)
	}
	if len(measured.Observations) > 0 {
		phases = append(phases, measured)
	}
	return phases, nil
}

func resourceObservation(class, probe, language string, observed map[string]any) Observation {
	return Observation{ID: class + "." + probe, Language: language, Observed: observed}
}

func readonlyOutcome(readonly bool) string {
	if readonly {
		return "read_only"
	}
	return "writable"
}
