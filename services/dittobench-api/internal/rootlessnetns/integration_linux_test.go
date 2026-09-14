//go:build rootless_router_integration

package rootlessnetns

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// This test needs a real rootless Docker daemon started by the test user with
// dockerd-rootless.sh, util-linux nsenter, and prebuilt helper/probe binaries.
// It is run only by the dedicated CI job; it never skips silently.

type recordingListener struct {
	net.Listener
	mu      sync.Mutex
	remotes []netip.Addr
}

func (listener *recordingListener) Accept() (net.Conn, error) {
	conn, err := listener.Listener.Accept()
	if err == nil {
		if address, ok := conn.RemoteAddr().(*net.TCPAddr); ok {
			listener.mu.Lock()
			listener.remotes = append(listener.remotes, address.AddrPort().Addr().Unmap())
			listener.mu.Unlock()
		}
	}
	return conn, err
}

func (listener *recordingListener) seen() []netip.Addr {
	listener.mu.Lock()
	defer listener.mu.Unlock()
	return append([]netip.Addr(nil), listener.remotes...)
}

func requiredEnv(t *testing.T, name string) string {
	t.Helper()
	value := os.Getenv(name)
	if value == "" {
		t.Fatalf("%s is required", name)
	}
	return value
}

func docker(t *testing.T, ctx context.Context, stdin []byte, args ...string) string {
	t.Helper()
	command := exec.CommandContext(ctx, "docker", args...)
	if stdin != nil {
		command.Stdin = bytes.NewReader(stdin)
	}
	out, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("docker %s: %v: %s", args[0], err, out)
	}
	return strings.TrimSpace(string(out))
}

func probeImage(t *testing.T, ctx context.Context, probe, name string) {
	t.Helper()
	body, err := os.ReadFile(probe)
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	if writer.WriteHeader(&tar.Header{Name: "probe", Mode: 0o555, Size: int64(len(body))}) != nil {
		t.Fatal("probe header")
	}
	if _, err := writer.Write(body); err != nil || writer.Close() != nil {
		t.Fatal("probe archive")
	}
	docker(t, ctx, archive.Bytes(), "import", "--change", `ENTRYPOINT ["/probe"]`, "-", name)
}

func TestRootlessRouterListenerPreservesContainerSource(t *testing.T) {
	socket := requiredEnv(t, "DITTOBENCH_ROOTLESS_IT_DOCKER_SOCKET")
	helper := requiredEnv(t, "DITTOBENCH_ROOTLESS_IT_HELPER")
	probe := requiredEnv(t, "DITTOBENCH_ROOTLESS_IT_PROBE")
	hostAddress, err := netip.ParseAddr(requiredEnv(t, "DITTOBENCH_ROOTLESS_IT_HOST_ADDRESS"))
	if err != nil || !hostAddress.Is4() || !hostAddress.IsPrivate() {
		t.Fatal("host address must be a private IPv4 address")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 4*time.Minute)
	defer cancel()

	gateway, err := (&sandbox.LocalDocker{}).DefaultBridgeGateway(ctx)
	if err != nil {
		t.Fatalf("default bridge gateway: %v", err)
	}
	inAddress := netip.AddrPortFrom(gateway, 18080)
	hostRouterAddress := netip.AddrPortFrom(hostAddress, 18081)

	// A socket created in the host namespace must never pass the real
	// namespace check, even when it listens on the expected address shape.
	pid, err := readChildPID(runUserRoot, os.Geteuid())
	if err != nil {
		t.Fatalf("RootlessKit child pid: %v", err)
	}
	child, err := inspectProcess("/proc", pid)
	if err != nil {
		t.Fatalf("RootlessKit child: %v", err)
	}
	self, err := inspectSelf("/proc")
	if err != nil || child.facts.net == self.net || child.facts.user == self.user {
		t.Fatal("RootlessKit child is not in a separate user and network namespace")
	}
	hostProbe := netip.AddrPortFrom(hostAddress, 18082)
	hostFD, err := createListener(hostProbe)
	if err != nil {
		t.Fatalf("host namespace probe listener: %v", err)
	}
	if listener, err := adoptListener(hostFD, hostProbe, socketNetns, child.facts.net); err == nil {
		_ = listener.Close()
		t.Fatal("host namespace listener accepted as RootlessKit listener")
	}
	child.Close()

	rawIn, err := Listen(ctx, Config{Address: inAddress, DockerSocket: socket, HelperExecutable: helper})
	if err != nil {
		t.Fatalf("in-namespace listener: %v", err)
	}
	in := &recordingListener{Listener: rawIn}
	rawHost, err := net.Listen("tcp4", hostRouterAddress.String())
	if err != nil {
		t.Fatalf("host namespace listener: %v", err)
	}
	host := &recordingListener{Listener: rawHost}

	registry := codingsource.NewRegistry(nil)
	token := strings.Repeat("r", 43)
	fixed := func() string { return token }
	inRouter, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: in, PublicBaseURL: "http://host.docker.internal:18080", Registry: registry, NewToken: fixed, MaxRoutes: 2})
	if err != nil {
		t.Fatal(err)
	}
	hostRouter, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: host, PublicBaseURL: "http://host.docker.internal:18081", Registry: registry, NewToken: fixed, MaxRoutes: 2})
	if err != nil {
		t.Fatal(err)
	}

	var suffix [6]byte
	if _, err := rand.Read(suffix[:]); err != nil {
		t.Fatal(err)
	}
	id := hex.EncodeToString(suffix[:])
	image, network, container := "ditto-router-probe:"+id, "ditto-job-"+id, "ditto-router-probe-"+id
	probeImage(t, ctx, probe, image)
	t.Cleanup(func() {
		cleanup, stop := context.WithTimeout(context.Background(), time.Minute)
		defer stop()
		for _, args := range [][]string{{"rm", "-f", container}, {"network", "rm", network}, {"image", "rm", "-f", image}} {
			_ = exec.CommandContext(cleanup, "docker", args...).Run()
		}
	})
	// Match the sandbox's per-run network and container confinement.
	docker(t, ctx, nil, "network", "create", "--driver", "bridge",
		"--opt", "com.docker.network.bridge.name=dtj"+id[:10],
		"--opt", "com.docker.network.bridge.enable_icc=false", network)
	inURL := "http://host.docker.internal:18080/v1/coding/workspace/" + token + "/tool"
	hostURL := "http://host.docker.internal:18081/v1/coding/workspace/" + token + "/tool"
	docker(t, ctx, nil, "run", "-d", "--name", container, "--network", network,
		"--user", "65532:65532", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--add-host", "host.docker.internal:"+gateway.String(), image, inURL, hostURL, hostRouterAddress.String())

	var containerIP netip.Addr
	for stop := time.Now().Add(time.Minute); time.Now().Before(stop) && !containerIP.IsValid(); time.Sleep(200 * time.Millisecond) {
		out, err := exec.CommandContext(ctx, "docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container).Output()
		if err == nil {
			containerIP, _ = netip.ParseAddr(strings.TrimSpace(string(out)))
		}
	}
	if !containerIP.IsValid() || containerIP == hostAddress {
		t.Fatal("container address unavailable")
	}
	binding := codingsource.HarnessBinding{
		HarnessInstanceID: "rootless-router-it", AgentArtifactSHA256: strings.Repeat("a", 64),
		TicketID: "33333333-3333-4333-8333-333333333333", CaseID: "rootless-router-it", ProfileCapabilityID: "rootless-router-it",
		Deadline: time.Now().Add(time.Hour),
	}
	lease, err := registry.Register(binding, containerIP.String())
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	admitted := map[string][]string{}
	handler := func(name string) http.Handler {
		return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
			mu.Lock()
			admitted[name] = append(admitted[name], request.RemoteAddr)
			mu.Unlock()
			response.WriteHeader(http.StatusOK)
		})
	}
	capability := codingcertifier.CapabilityBinding{
		HarnessInstanceID: binding.HarnessInstanceID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		TicketID: binding.TicketID, CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
	}
	inRoute, err := inRouter.WorkspacePublisher().Publish(ctx, capability, handler("in"))
	if err != nil || inRoute.URL() != inURL {
		t.Fatal("in-namespace route publication")
	}
	hostRoute, err := hostRouter.WorkspacePublisher().Publish(ctx, capability, handler("host"))
	if err != nil || hostRoute.URL() != hostURL {
		t.Fatal("host namespace route publication")
	}

	if code := docker(t, ctx, nil, "wait", container); code != "0" {
		t.Fatalf("probe exited %s: %s", code, docker(t, ctx, nil, "logs", container))
	}
	var result map[string]int
	if err := json.Unmarshal([]byte(docker(t, ctx, nil, "logs", container)), &result); err != nil {
		t.Fatalf("probe result: %v", err)
	}
	mu.Lock()
	inAdmitted, hostAdmitted := append([]string(nil), admitted["in"]...), len(admitted["host"])
	mu.Unlock()
	t.Logf("container=%s gateway=%s host=%s result=%v in_remotes=%v host_remotes=%v", containerIP, gateway, hostAddress, result, in.seen(), host.seen())

	if result["in_namespace"] != http.StatusOK || len(inAdmitted) == 0 {
		t.Fatal("in-namespace router did not admit the registered container")
	}
	for _, remote := range inAdmitted {
		address, err := netip.ParseAddrPort(remote)
		if err != nil || address.Addr().Unmap() != containerIP {
			t.Fatalf("in-namespace router admitted %s, want container %s", remote, containerIP)
		}
	}
	for _, remote := range in.seen() {
		if remote != containerIP {
			t.Fatalf("in-namespace listener saw %s, want only container %s", remote, containerIP)
		}
	}
	// The failure mode being fixed: through slirp4netns the host listener sees
	// the host's own address, so container-source admission must refuse it.
	if result["host_namespace"] != http.StatusNotFound || hostAdmitted != 0 {
		t.Fatalf("host namespace router result %d with %d admissions", result["host_namespace"], hostAdmitted)
	}
	hostSeen := host.seen()
	if len(hostSeen) == 0 {
		t.Fatal("host namespace listener saw no connection")
	}
	for _, remote := range hostSeen {
		if remote != hostAddress {
			t.Fatalf("host namespace listener saw %s, want host address %s", remote, hostAddress)
		}
	}

	for _, route := range []interface {
		Revoke(context.Context) error
		Close() error
	}{inRoute, hostRoute} {
		if route.Revoke(ctx) != nil || route.Close() != nil {
			t.Fatal("route cleanup")
		}
	}
	if lease.Close() != nil || inRouter.Close(ctx) != nil || hostRouter.Close(ctx) != nil {
		t.Fatal("router cleanup")
	}
}
