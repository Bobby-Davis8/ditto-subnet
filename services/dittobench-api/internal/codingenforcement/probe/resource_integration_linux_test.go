//go:build native_probe_integration

package probe

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

// This test needs a real rootless, isolated Docker daemon (chosen by
// DOCKER_HOST) and an approved coding-supervisor image already loaded into it,
// registry@sha256 addressed via DITTOBENCH_NATIVE_PROBE_IMAGE. The dedicated CI
// job builds the image, pushes it to a throwaway in-daemon registry so it
// carries a repository digest, and provides these variables. It never skips
// silently: the CI job is what gates the daemon prerequisites.

type executorResourceObserver struct {
	executor *codingexecutor.Executor
	expected ExpectedResourcePolicy
}

func (o executorResourceObserver) Observe(ctx context.Context) (AppliedResourcePolicy, error) {
	observation, err := o.executor.ObserveResourceContainer(ctx)
	if err != nil {
		return AppliedResourcePolicy{}, err
	}
	return AppliedResourcePolicy{
		MemoryLimitBytes:  observation.MemoryLimitBytes,
		MemorySwapBytes:   observation.MemorySwapBytes,
		CPUQuotaMillis:    observation.NanoCPUs / 1_000_000,
		PidsLimit:         observation.PidsLimit,
		ScratchLimitBytes: observation.ScratchLimitBytes,
		ReadonlyRootfs:    observation.ReadonlyRootfs,
	}, nil
}

func (o executorResourceObserver) Expected() ExpectedResourcePolicy { return o.expected }

func gradingConfig(t *testing.T, imageRef string) codingexecutor.Config {
	t.Helper()
	at := strings.LastIndex(imageRef, "@sha256:")
	if at <= 0 {
		t.Fatalf("approved image must be registry@sha256 addressed: %q", imageRef)
	}
	digest := imageRef[at+1:]
	limits := codingrunner.DefaultLimits()
	policy := codinggrader.ResourcePolicy{
		CandidateLimits: limits, ProtectedLimits: limits,
		MaxCombinedDiskBytes: 4 << 30, MemoryLimitBytes: 1 << 30, ScratchLimitBytes: 1 << 30,
		PidsLimit: 256, CPUQuotaMillis: 1_000,
	}
	resourceSHA, err := codinggrader.ResourceProfileSHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	groups := make([]codinggrader.TestGroupSpec, 0, 5)
	for index, name := range []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"} {
		groups = append(groups, codinggrader.TestGroupSpec{
			Group: name,
			Command: codingrunner.CommandSpec{
				ID: "test-" + name, Argv: []string{"dittobench-test-driver", "grader/" + name + ".json"}, Timeout: time.Minute,
			},
			ExpectedTotal: uint32(index + 1),
		})
	}
	manifest := codinggrader.Manifest{
		CodingContractVersion: codingrunner.ContractVersion, CaseID: "native-probe-001", VariantID: "variant-v1",
		VisibleBundleSHA256: strings.Repeat("1", 64), BaseTreeSHA256: strings.Repeat("2", 64),
		GraderContractSHA256: codinggrader.GraderContractSHA256(), GraderBundleSHA256: strings.Repeat("3", 64),
		GraderImageDigest: digest, GraderPlatform: "linux/amd64", TestManifestSHA256: strings.Repeat("4", 64),
		ResourceProfileSHA256: resourceSHA, Deadline: time.Now().Add(time.Hour), ExecutionTimeout: 30 * time.Minute,
		ResourcePolicy: policy,
		Build: codinggrader.BuildSpec{Required: true, Command: codingrunner.CommandSpec{
			ID: "build-python", Argv: []string{"fixture-command", "noop"}, Timeout: time.Minute,
		}},
		TestGroups: groups,
	}
	planSHA, err := codinggrader.GraderPlanSHA256(manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.GraderPlanSHA256 = planSHA
	return codingexecutor.Config{
		Manifest: manifest, ImageRef: imageRef,
		CandidateUID: 10001, CandidateGID: 10001,
		RequireRootless: true, RequireIsolatedDaemon: true, AllowCertificationImage: true,
	}
}

func TestExecutorResourceEnforcementIsMeasuredThroughTheProductionLaunch(t *testing.T) {
	imageRef := os.Getenv("DITTOBENCH_NATIVE_PROBE_IMAGE")
	if imageRef == "" {
		t.Fatal("DITTOBENCH_NATIVE_PROBE_IMAGE is required")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 4*time.Minute)
	defer cancel()

	docker := ExecDocker{}
	// The approved image must resolve to a local content id without a pull.
	resolved, err := ResolveApprovedImage(ctx, docker, imageRef)
	if err != nil {
		t.Fatalf("resolve approved image: %v", err)
	}
	if resolved.ID == "" {
		t.Fatal("resolved image id is empty")
	}

	config := gradingConfig(t, imageRef)
	executor, err := codingexecutor.New(config)
	if err != nil {
		t.Fatalf("build grading executor: %v", err)
	}

	cat, err := catalog.Load()
	if err != nil {
		t.Fatal(err)
	}
	policy := config.Manifest.ResourcePolicy
	collector := ResourceCollector{Observers: map[string]map[string]ResourceObserver{
		"executor_grading": {"python": executorResourceObserver{
			executor: executor,
			expected: ExpectedResourcePolicy{
				MemoryLimitBytes: int64(policy.MemoryLimitBytes),
				CPUQuotaMillis:   int64(policy.CPUQuotaMillis),
				PidsLimit:        int64(policy.PidsLimit),
			},
		}},
	}}
	phases, err := collector.Collect(ctx, nil)
	if err != nil {
		t.Fatalf("collect executor resource evidence: %v", err)
	}
	outcomes := cat.OutcomeSet()
	expects := map[string]catalog.Expectation{}
	for _, probe := range cat.Kinds["resource_enforcement"].Probes {
		expects[probe.ID] = probe.Expect
	}
	collected := 0
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
				t.Fatalf("live executor observation %s did not match production enforcement: %v", observation.ID, observation.Observed)
			}
			collected++
		}
	}
	if collected == 0 {
		t.Fatal("no executor resource observations were collected")
	}
	t.Logf("measured %d executor_grading resource observations through the production launch code", collected)
}
