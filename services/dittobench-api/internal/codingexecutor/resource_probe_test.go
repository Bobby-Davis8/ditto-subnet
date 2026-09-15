package codingexecutor

import "testing"

func TestObserveResourceContainerReadsTheAppliedProductionPolicy(t *testing.T) {
	config := testConfig(t)
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	observation, err := executor.ObserveResourceContainer(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	policy := config.Manifest.ResourcePolicy
	if observation.MemoryLimitBytes != int64(policy.MemoryLimitBytes) ||
		observation.MemorySwapBytes != 0 ||
		observation.NanoCPUs != int64(policy.CPUQuotaMillis)*1_000_000 ||
		observation.PidsLimit != int64(policy.PidsLimit) ||
		!observation.ReadonlyRootfs ||
		observation.ScratchLimitBytes != int64(policy.ScratchLimitBytes) ||
		observation.GraderImageDigest != config.Manifest.GraderImageDigest {
		t.Fatalf("observation=%#v", observation)
	}
	// The observation must be created and removed through the production launch
	// code: a create followed by a policy inspect and an exact-id removal.
	var created, removed bool
	for _, run := range docker.runs {
		if len(run) > 0 && run[0] == "create" {
			created = true
		}
	}
	docker.mu.Lock()
	remaining := len(docker.active)
	docker.mu.Unlock()
	removed = remaining == 0
	if !created || !removed {
		t.Fatalf("resource observation did not create+remove through the launch code (created=%v removed=%v)", created, removed)
	}
}

func TestObserveResourceContainerRefusesAnAuthoringOnlyExecutor(t *testing.T) {
	config := testConfig(t)
	config.AuthoringOnly = true
	config.Manifest.GraderPlanSHA256 = ""
	docker := newFakeDocker(config)
	executor, err := newWithDocker(config, docker)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.ObserveResourceContainer(t.Context()); err == nil {
		t.Fatal("an authoring-only executor must not expose a grading resource observation")
	}
}
