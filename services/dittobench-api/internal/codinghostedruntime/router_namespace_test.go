package codinghostedruntime

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
)

func rootlessHelper(t *testing.T) string {
	t.Helper()
	worker, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(filepath.Dir(worker), rootlessnetns.HelperExecutableName)
}

func rootlessExecutable(helper string) func(string) bool {
	return func(path string) bool {
		return testExecutable(path) || path == helper || path == rootlessnetns.NsenterExecutable
	}
}

func TestRouterNamespaceDefaultsToExistingHostListener(t *testing.T) {
	for _, mode := range []string{"", routerNamespaceHost} {
		wire, path := fixture(t)
		wire.RouterNamespace = mode
		writeConfig(t, path, wire)
		// Even with the rootless prerequisites installed, the default stays host.
		config, err := loadConfigChecked(path, rootlessExecutable(rootlessHelper(t)))
		if err != nil {
			t.Fatalf("mode %q rejected: %v", mode, err)
		}
		if config.router != nil || config.docker.HostGatewayIP != "172.21.0.1" || config.publicBase != "http://host.docker.internal:19010" {
			t.Fatalf("mode %q did not keep the host listener", mode)
		}
	}
	// An omitted field is not only accepted, it is the current wire shape.
	wire, path := fixture(t)
	writeConfig(t, path, wire)
	body, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(body), `"router_namespace":""`) {
		t.Fatal("fixture shape")
	}
	write(t, path, []byte(strings.Replace(string(body), `"router_namespace":"",`, "", 1)))
	if config, err := loadConfigChecked(path, testExecutable); err != nil || config.router != nil {
		t.Fatal("omitted router namespace changed behavior")
	}
}

func TestRootlessRouterNamespaceBindsGatewayHelperAndNsenter(t *testing.T) {
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace = routerNamespaceRootless
	wire.RouterListen = "172.17.0.1:18080"
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		t.Fatalf("rootless router namespace rejected: %v", err)
	}
	if config.router == nil || config.router.address != netip.MustParseAddrPort("172.17.0.1:18080") || config.router.helper != helper {
		t.Fatal("rootless router binding drift")
	}
	// Candidates resolve host.docker.internal to the in-namespace gateway, never
	// to discovered eth0 or Docker's host-gateway placeholder.
	if !config.docker.RequireRootless || config.docker.HostGatewayIP != "172.17.0.1" || config.publicBase != "http://host.docker.internal:18080" {
		t.Fatal("candidate gateway is not the in-namespace router address")
	}
	if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
		t.Fatal("validation consumed attempt")
	}
}

func TestRootlessRouterNamespaceRejectsDriftBeforeConsumingAttempt(t *testing.T) {
	helper := rootlessHelper(t)
	for name, tc := range map[string]struct {
		mode, listen string
		executable   func(string) bool
	}{
		"unknown_mode":       {mode: "rootless", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"case_mode":          {mode: "Rootless-Netns", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"padded_mode":        {mode: " rootless-netns", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"slirp_mode":         {mode: "slirp4netns", listen: "172.17.0.1:18080", executable: rootlessExecutable(helper)},
		"unprotected_helper": {mode: routerNamespaceRootless, listen: "172.17.0.1:18080", executable: rootlessExecutable("")},
		"unprotected_nsenter": {mode: routerNamespaceRootless, listen: "172.17.0.1:18080", executable: func(path string) bool {
			return testExecutable(path) || path == helper
		}},
		"loopback":  {mode: routerNamespaceRootless, listen: "127.0.0.1:18080", executable: rootlessExecutable(helper)},
		"wildcard":  {mode: routerNamespaceRootless, listen: "0.0.0.0:18080", executable: rootlessExecutable(helper)},
		"public":    {mode: routerNamespaceRootless, listen: "8.8.8.8:18080", executable: rootlessExecutable(helper)},
		"low_port":  {mode: routerNamespaceRootless, listen: "172.17.0.1:80", executable: rootlessExecutable(helper)},
		"ipv6":      {mode: routerNamespaceRootless, listen: "[fd00::1]:18080", executable: rootlessExecutable(helper)},
		"noncanon":  {mode: routerNamespaceRootless, listen: "172.17.0.1:018080", executable: rootlessExecutable(helper)},
		"no_port":   {mode: routerNamespaceRootless, listen: "172.17.0.1", executable: rootlessExecutable(helper)},
		"hostname":  {mode: routerNamespaceRootless, listen: "gateway:18080", executable: rootlessExecutable(helper)},
		"four_in_6": {mode: routerNamespaceRootless, listen: "[::ffff:172.17.0.1]:18080", executable: rootlessExecutable(helper)},
	} {
		t.Run(name, func(t *testing.T) {
			wire, path := fixture(t)
			wire.RouterNamespace, wire.RouterListen = tc.mode, tc.listen
			writeConfig(t, path, wire)
			if _, err := loadConfigChecked(path, tc.executable); err != ErrConfig {
				t.Fatal("rootless router drift accepted")
			}
			if _, err := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(err) {
				t.Fatal("invalid config consumed attempt")
			}
		})
	}
}

func fakeDocker(t *testing.T, gateway string) {
	t.Helper()
	dir := privateTemp(t)
	script := "#!/bin/sh\n[ \"$1 $2 $5\" = \"network inspect bridge\" ] || exit 9\n" +
		"printf '%s\\n' '{\"Name\":\"bridge\",\"Driver\":\"bridge\",\"Internal\":false,\"IPAM\":{\"Config\":[{\"Subnet\":\"" +
		netip.PrefixFrom(netip.MustParseAddr(gateway), 16).Masked().String() + "\",\"Gateway\":\"" + gateway + "\"}]},\"Options\":{\"com.docker.network.bridge.default_bridge\":\"true\"}}'\n"
	if os.WriteFile(filepath.Join(dir, "docker"), []byte(script), 0o700) != nil {
		t.Fatal("docker fixture")
	}
	t.Setenv("PATH", dir)
}

// The rootless mode must never fall back to a host-namespace listener, even
// when the configured gateway happens to be a local host address.
func TestRootlessRouterNeverFallsBackToHostListener(t *testing.T) {
	var local netip.Addr
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		t.Fatal(err)
	}
	for _, address := range addresses {
		prefix, err := netip.ParsePrefix(address.String())
		if err == nil && prefix.Addr().Is4() && prefix.Addr().IsPrivate() && !prefix.Addr().IsLoopback() {
			local = prefix.Addr()
			break
		}
	}
	if !local.IsValid() {
		t.Skip("no local private IPv4 address")
	}
	probe, err := net.Listen("tcp4", netip.AddrPortFrom(local, 0).String())
	if err != nil {
		t.Fatal(err)
	}
	address := netip.MustParseAddrPort(probe.Addr().String())
	_ = probe.Close()
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace, wire.RouterListen = routerNamespaceRootless, address.String()
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		t.Fatalf("fixture rejected: %v", err)
	}
	previous := rootlessListen
	t.Cleanup(func() { rootlessListen = previous })
	for name, gateway := range map[string]string{"gateway_mismatch": "172.31.255.254", "no_rootlesskit": local.String()} {
		t.Run(name, func(t *testing.T) {
			fakeDocker(t, gateway)
			called := false
			// Delegate to the real RootlessKit listener; only record the call.
			rootlessListen = func(ctx context.Context, got rootlessnetns.Config) (net.Listener, error) {
				called = true
				return rootlessnetns.Listen(ctx, got)
			}
			if listener, err := listenRouter(t.Context(), config); err == nil {
				_ = listener.Close()
				t.Fatal("rootless mode produced a listener without RootlessKit")
			}
			if called != (name == "no_rootlesskit") {
				t.Fatalf("real namespace listener called=%v", called)
			}
			free, err := net.Listen("tcp4", address.String())
			if err != nil {
				t.Fatal("rootless mode bound the configured address in the host namespace")
			}
			_ = free.Close()
		})
	}
	t.Run("docker_unavailable", func(t *testing.T) {
		t.Setenv("PATH", privateTemp(t))
		if listener, err := listenRouter(t.Context(), config); err == nil {
			_ = listener.Close()
			t.Fatal("rootless mode listened without the daemon bridge")
		}
	})
}

func TestRootlessRouterRequiresDaemonGatewayBeforeNamespaceListener(t *testing.T) {
	helper := rootlessHelper(t)
	wire, path := fixture(t)
	wire.RouterNamespace, wire.RouterListen = routerNamespaceRootless, "172.17.0.1:18080"
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, rootlessExecutable(helper))
	if err != nil {
		t.Fatalf("fixture rejected: %v", err)
	}
	var calls []rootlessnetns.Config
	sentinel, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer sentinel.Close()
	previous := rootlessListen
	t.Cleanup(func() { rootlessListen = previous })
	rootlessListen = func(_ context.Context, got rootlessnetns.Config) (net.Listener, error) {
		calls = append(calls, got)
		return sentinel, nil
	}
	for _, gateway := range []string{"172.17.0.2", "172.18.0.1"} {
		fakeDocker(t, gateway)
		if _, err := listenRouter(t.Context(), config); err == nil {
			t.Fatalf("gateway %s accepted for router 172.17.0.1", gateway)
		}
	}
	if len(calls) != 0 {
		t.Fatal("namespace listener requested before the daemon gateway matched")
	}
	fakeDocker(t, "172.17.0.1")
	listener, err := listenRouter(t.Context(), config)
	if err != nil || listener != sentinel {
		t.Fatalf("matching gateway refused: %v", err)
	}
	want := rootlessnetns.Config{Address: netip.MustParseAddrPort("172.17.0.1:18080"), DockerSocket: wire.DockerSocket, HelperExecutable: helper}
	if len(calls) != 1 || calls[0] != want {
		t.Fatalf("namespace listener config drift: %+v", calls)
	}
	rootlessListen = func(context.Context, rootlessnetns.Config) (net.Listener, error) { return nil, errors.New("refused") }
	if listener, err := listenRouter(t.Context(), config); err == nil || listener != nil {
		t.Fatal("namespace refusal became a listener")
	}
}
