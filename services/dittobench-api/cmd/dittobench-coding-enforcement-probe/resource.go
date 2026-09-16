package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/probe"
)

const maxProfileBytes = 64 << 10

// resourceAgent serves the resource line protocol on stdin/stdout for the root
// collector. SIGTERM cancels every run; the agent waits for production cleanup
// before it exits (the cleanup_recovery runner_sigterm scenario).
func resourceAgent(ctx context.Context, args []string, stdin io.Reader, stdout io.Writer) error {
	flags := flag.NewFlagSet("resource-agent", flag.ContinueOnError)
	executionPath := flags.String("execution-profile", "", "exact approved execution profile")
	gradingPath := flags.String("grading-profile", "", "exact approved grading profile")
	imagesPath := flags.String("enforcement-images", "", "pinned per-language enforcement image set")
	runner := flags.String("runner", "", "host path of this probe runner, mounted into harness workloads")
	workDirectory := flags.String("work-dir", "", "private exec-permitted directory for probe workspaces")
	seccomp := flags.String("seccomp-profile", "", "host seccomp profile name, as the hosted runtime passes it")
	apparmor := flags.String("apparmor-profile", "", "host AppArmor profile name, as the hosted runtime passes it")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *executionPath == "" || *gradingPath == "" || *imagesPath == "" || *runner == "" || *workDirectory == "" || flags.NArg() != 0 {
		return errors.New("resource-agent needs --execution-profile, --grading-profile, --enforcement-images, --runner and --work-dir")
	}
	inputs := map[string][]byte{}
	for name, path := range map[string]string{"execution": *executionPath, "grading": *gradingPath, "images": *imagesPath} {
		raw, err := readBounded(path, maxProfileBytes)
		if err != nil {
			return err
		}
		inputs[name] = raw
	}
	ctx, stop := signal.NotifyContext(ctx, syscall.SIGTERM)
	defer stop()
	agent, err := probe.NewResourceAgent(ctx, probe.ResourceAgentConfig{
		ExecutionProfile: inputs["execution"], GradingProfile: inputs["grading"], EnforcementImages: inputs["images"],
		Runner: *runner, WorkDirectory: *workDirectory, SeccompProfile: *seccomp, AppArmorProfile: *apparmor,
		Backend: probe.ProductionResourceBackend{Docker: probe.ExecDocker{}},
	})
	if err != nil {
		return err
	}
	return probe.ServeResourceAgent(ctx, agent, stdin, stdout)
}

func readBounded(path string, maximum int64) ([]byte, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > maximum {
		return nil, errors.New("input exceeds its bound")
	}
	return raw, nil
}
