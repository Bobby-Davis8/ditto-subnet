package codingexecutor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

// ResourceObservation is the created executor container's applied resource
// policy, read back from the exact production launch code. It is consumed by
// the native-enforcement probe runner, which records it as evidence that the
// approved profile was enforced. It carries no candidate output, path or env.
type ResourceObservation struct {
	// GraderImageDigest and DriverProfile identify the approved runtime image
	// the container was launched from.
	GraderImageDigest string
	DriverProfile     string
	// The applied cgroup/host-config limits, in the profile's own units.
	MemoryLimitBytes  int64
	MemorySwapBytes   int64
	NanoCPUs          int64
	PidsLimit         int64
	ReadonlyRootfs    bool
	ScratchLimitBytes int64
}

// ObserveResourceContainer creates one fresh grading-mode container through the
// production create+inspect+cleanup path, verifies it satisfies the attested
// plan, and returns its applied resource policy. Nothing miner-controlled
// reaches this path; the container is removed by exact id before returning.
//
// It is the executor seam the native-enforcement resource probe runner drives:
// the runner reuses the real container spec builder (createArgs), limits,
// security options, uid/gid and mounts rather than reimplementing them.
func (executor *Executor) ObserveResourceContainer(ctx context.Context) (ResourceObservation, error) {
	if executor == nil || ctx == nil {
		return ResourceObservation{}, errors.New("coding executor resource observation context is required")
	}
	if executor.config.AuthoringOnly {
		return ResourceObservation{}, errors.New("coding executor resource observation requires a grading executor")
	}
	if err := executor.ensurePreflight(ctx); err != nil {
		return ResourceObservation{}, err
	}
	policy := executor.config.Manifest.ResourcePolicy
	observation := ResourceObservation{
		GraderImageDigest: executor.config.Manifest.GraderImageDigest,
		DriverProfile:     executor.driverProfile,
		ScratchLimitBytes: int64(executor.temporaryBytes()),
	}
	err := executor.withProbeContainer(ctx, func(container, workspace, protected, control string) error {
		if err := executor.inspectContainerPolicy(ctx, container, modeTest, workspace, protected, control); err != nil {
			return err
		}
		inspection, err := executor.inspectContainerConfig(ctx, container)
		if err != nil {
			return err
		}
		observation.MemoryLimitBytes = inspection.HostConfig.Memory
		observation.MemorySwapBytes = inspection.HostConfig.MemorySwap - inspection.HostConfig.Memory
		observation.NanoCPUs = inspection.HostConfig.NanoCPUs
		observation.PidsLimit = inspection.HostConfig.PidsLimit
		observation.ReadonlyRootfs = inspection.HostConfig.ReadonlyRootfs
		// The policy inspection already required these to equal the approved
		// profile; a residual mismatch here is a launch-code contract break.
		if observation.MemoryLimitBytes != int64(policy.MemoryLimitBytes) ||
			observation.NanoCPUs != int64(policy.CPUQuotaMillis)*1_000_000 ||
			observation.PidsLimit != int64(policy.PidsLimit) {
			return errors.New("coding executor resource observation disagrees with the attested policy")
		}
		return nil
	})
	if err != nil {
		return ResourceObservation{}, err
	}
	return observation, nil
}

func (executor *Executor) inspectContainerConfig(ctx context.Context, container string) (dockerContainerInspection, error) {
	raw, err := executor.docker.Output(ctx, "container", "inspect", container)
	if err != nil {
		return dockerContainerInspection{}, fmt.Errorf("inspect coding sandbox resource policy: %w", err)
	}
	var values []dockerContainerInspection
	if err := json.Unmarshal(bytes.TrimSpace(raw), &values); err != nil || len(values) != 1 {
		return dockerContainerInspection{}, errors.New("coding sandbox resource inspection is invalid")
	}
	return values[0], nil
}
