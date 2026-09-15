package main

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinghost"
)

func TestCodingShadowHostIsDefaultOffAndLockedPolicyIsCanonical(t *testing.T) {
	t.Setenv("DITTOBENCH_CODING_SHADOW_ENABLED", "false")
	t.Setenv("DITTOBENCH_CODING_CANARY_ENABLED", "false")
	host, err := codingShadowHostFromEnvironment(8000, 11436)
	if err != nil || host != nil {
		t.Fatalf("default host=%v err=%v", host, err)
	}
	path := filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_inference_policy_locked_v1.json",
	)
	path, err = filepath.Abs(path)
	if err != nil {
		t.Fatal(err)
	}
	policy, err := loadCodingInferencePolicy(path)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil || digest != "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6" {
		t.Fatalf("policy digest=%s err=%v", digest, err)
	}
}

func TestCodingDockerHostMustBeADedicatedRootlessSocket(t *testing.T) {
	const sandboxDaemon = "tcp://127.0.0.1:2375"
	for _, endpoint := range []string{
		"",
		sandboxDaemon,
		"tcp://sandbox-docker:2375",
		"unix:///var/run/docker.sock",
		"unix:///run/docker.sock",
		"unix://relative/docker.sock",
		"unix:///run/ditto-coding-executor/../docker.sock",
	} {
		_, err := codingDockerHostFromEnvironment(func(name string) string {
			return map[string]string{codingDockerHostEnvironment: endpoint, "DOCKER_HOST": sandboxDaemon}[name]
		})
		if err == nil {
			t.Errorf("coding docker endpoint %q was accepted", endpoint)
		}
	}
	dedicated := "unix:///run/ditto-coding-executor/docker.sock"
	if _, err := codingDockerHostFromEnvironment(func(name string) string {
		return map[string]string{codingDockerHostEnvironment: dedicated, "DOCKER_HOST": dedicated}[name]
	}); err == nil {
		t.Error("coding endpoint equal to the ordinary scoring daemon was accepted")
	}
	endpoint, err := codingDockerHostFromEnvironment(func(name string) string {
		return map[string]string{codingDockerHostEnvironment: " " + dedicated + " ", "DOCKER_HOST": sandboxDaemon}[name]
	})
	if err != nil || endpoint != dedicated {
		t.Fatalf("endpoint=%q err=%v", endpoint, err)
	}
}

// On today's Compose stack the only daemon is the privileged rootful
// sandbox-docker. Enabling a coding gate there must refuse before creating
// private state or binding the source listener.
func TestEnabledCodingGateRefusesTheSandboxDaemonBeforeSideEffects(t *testing.T) {
	for _, gate := range []string{"DITTOBENCH_CODING_CANARY_ENABLED", "DITTOBENCH_CODING_SHADOW_ENABLED"} {
		for _, endpoint := range []string{"", "tcp://127.0.0.1:2375"} {
			t.Run(fmt.Sprintf("%s/%q", gate, endpoint), func(t *testing.T) {
				listener, err := net.Listen("tcp4", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				port := listener.Addr().(*net.TCPAddr).Port
				if err := listener.Close(); err != nil {
					t.Fatal(err)
				}
				privateRoot := filepath.Join(t.TempDir(), "coding-shadow-v1")
				t.Setenv("DITTOBENCH_CODING_SHADOW_ENABLED", "false")
				t.Setenv("DITTOBENCH_CODING_CANARY_ENABLED", "false")
				t.Setenv(gate, "true")
				t.Setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
				t.Setenv(codingDockerHostEnvironment, endpoint)
				t.Setenv("DITTOBENCH_CODING_PRIVATE_ROOT", privateRoot)
				t.Setenv("DITTOBENCH_CODING_SOURCE_PORT", strconv.Itoa(port))
				host, err := codingShadowHostFromEnvironment(8000, 11436)
				if err == nil || host != nil || !strings.Contains(err.Error(), codingDockerHostEnvironment) {
					t.Fatalf("host=%v err=%v", host, err)
				}
				if _, statErr := os.Stat(privateRoot); !errors.Is(statErr, os.ErrNotExist) {
					t.Fatalf("private root was created: %v", statErr)
				}
				rebound, err := net.Listen("tcp4", "0.0.0.0:"+strconv.Itoa(port))
				if err != nil {
					t.Fatalf("source port was left bound: %v", err)
				}
				_ = rebound.Close()
			})
		}
	}
}

func TestCodingHostFailureIsNonFatalForOrdinaryScoring(t *testing.T) {
	var logs []string
	logf := func(format string, args ...any) { logs = append(logs, fmt.Sprintf(format, args...)) }
	host := installCodingHost(func() (*codinghost.Host, error) {
		return nil, errors.New("coding shadow host configuration is invalid")
	}, logf)
	if host != nil || len(logs) != 1 || !strings.Contains(logs[0], "ordinary scoring continues") {
		t.Fatalf("host=%v logs=%q", host, logs)
	}
	logs = nil
	if host := installCodingHost(func() (*codinghost.Host, error) { return nil, nil }, logf); host != nil || len(logs) != 0 {
		t.Fatalf("disabled host=%v logs=%q", host, logs)
	}

	// With the coding host refused, every coding route (including the canary
	// readiness probe the validator requires) is absent while ordinary
	// control-plane routes keep serving.
	mux := (&server{broker: newInferenceBroker(1, 1)}).newControlPlaneMux()
	for _, route := range []struct{ method, path string }{
		{http.MethodPost, "/v1/coding/certifier/canary"},
		{http.MethodGet, "/v1/coding/certifier/canary/readiness"},
		{http.MethodPost, "/v1/coding/supervisor/prepare"},
		{http.MethodPost, "/v1/coding/publications/pending"},
	} {
		response := httptest.NewRecorder()
		mux.ServeHTTP(response, httptest.NewRequest(route.method, route.path, strings.NewReader("{}")))
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s %s status=%d", route.method, route.path, response.Code)
		}
	}
	health := httptest.NewRecorder()
	mux.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusOK {
		t.Fatalf("health status=%d", health.Code)
	}
}
