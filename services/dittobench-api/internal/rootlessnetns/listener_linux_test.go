package rootlessnetns

import (
	"errors"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"strconv"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// The test binary doubles as the helper process. spawnHelper clears the
// environment, so the mode is carried in argv before normal test flags.
const fakeHelperArg = "rootlessnetns-fake-helper"

func TestMain(m *testing.M) {
	if len(os.Args) >= 2 && os.Args[1] == fakeHelperArg {
		os.Exit(fakeHelper(os.Args[2:]))
	}
	if len(os.Args) >= 2 && os.Args[1] == "rootlessnetns-real-helper" {
		os.Exit(RunHelper(os.Args[2:]))
	}
	os.Exit(m.Run())
}

func must(fd int, err error) int {
	if err != nil {
		os.Exit(90)
	}
	return fd
}

func tcpListener(port int) int {
	fd := must(unix.Socket(unix.AF_INET, unix.SOCK_STREAM, 0))
	if unix.Bind(fd, &unix.SockaddrInet4{Port: port, Addr: [4]byte{127, 0, 0, 1}}) != nil || unix.Listen(fd, 1) != nil {
		os.Exit(91)
	}
	return fd
}

func send(payload string, fds ...int) {
	var rights []byte
	if len(fds) > 0 {
		rights = unix.UnixRights(fds...)
	}
	if unix.Sendmsg(helperSocketFD, []byte(payload), rights, nil, 0) != nil {
		os.Exit(92)
	}
}

func fakeHelper(args []string) int {
	if len(args) != 2 {
		return 93
	}
	port, _ := strconv.Atoi(args[1])
	switch args[0] {
	case "valid":
		send(Protocol, tcpListener(port))
	case "exit_after_send":
		send(Protocol, tcpListener(port))
		return 3
	case "silent_exit":
	case "no_fd":
		send(Protocol)
	case "wrong_payload":
		send(Protocol+"x", tcpListener(port))
	case "short_payload":
		send("dittobench", tcpListener(port))
	case "two_fds":
		send(Protocol, tcpListener(port), tcpListener(port+1))
	case "two_messages":
		send(Protocol, tcpListener(port))
		send(Protocol, tcpListener(port+1))
	case "not_listening":
		fd := must(unix.Socket(unix.AF_INET, unix.SOCK_STREAM, 0))
		if unix.Bind(fd, &unix.SockaddrInet4{Port: port, Addr: [4]byte{127, 0, 0, 1}}) != nil {
			return 94
		}
		send(Protocol, fd)
	case "udp":
		fd := must(unix.Socket(unix.AF_INET, unix.SOCK_DGRAM, 0))
		if unix.Bind(fd, &unix.SockaddrInet4{Port: port, Addr: [4]byte{127, 0, 0, 1}}) != nil {
			return 94
		}
		send(Protocol, fd)
	case "ipv6":
		fd := must(unix.Socket(unix.AF_INET6, unix.SOCK_STREAM, 0))
		if unix.Bind(fd, &unix.SockaddrInet6{Port: port, Addr: [16]byte{15: 1}}) != nil || unix.Listen(fd, 1) != nil {
			return 94
		}
		send(Protocol, fd)
	case "unix_listener":
		fd := must(unix.Socket(unix.AF_UNIX, unix.SOCK_STREAM, 0))
		if unix.Bind(fd, &unix.SockaddrUnix{Name: "@rootlessnetns-test-" + args[1]}) != nil || unix.Listen(fd, 1) != nil {
			return 94
		}
		send(Protocol, fd)
	case "reuseport":
		fd := must(unix.Socket(unix.AF_INET, unix.SOCK_STREAM, 0))
		if unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_REUSEPORT, 1) != nil ||
			unix.Bind(fd, &unix.SockaddrInet4{Port: port, Addr: [4]byte{127, 0, 0, 1}}) != nil || unix.Listen(fd, 1) != nil {
			return 94
		}
		send(Protocol, fd)
	case "regular_file":
		send(Protocol, must(unix.Open("/proc/self/status", unix.O_RDONLY, 0)))
	default:
		return 95
	}
	return 0
}

func freePort(t *testing.T) int {
	t.Helper()
	for attempt := 0; attempt < 20; attempt++ {
		first, err := net.Listen("tcp4", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		port := first.Addr().(*net.TCPAddr).Port
		_ = first.Close()
		second, err := net.Listen("tcp4", "127.0.0.1:"+strconv.Itoa(port+1))
		if err == nil {
			_ = second.Close()
			return port
		}
	}
	t.Fatal("no free port pair")
	return 0
}

func testBinary(t *testing.T) string {
	t.Helper()
	path, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func openFDs(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	return len(entries)
}

var pinned = nsID{dev: 4, ino: 4026532822}

func pinnedNetns(int) (nsID, error) { return pinned, nil }

func TestReceivedListenerIsVerifiedAndServed(t *testing.T) {
	port := freePort(t)
	expected := netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port))
	fd, err := spawnHelper(t.Context(), []string{testBinary(t), fakeHelperArg, "valid", strconv.Itoa(port)})
	if err != nil {
		t.Fatalf("valid helper refused: %v", err)
	}
	listener, err := adoptListener(fd, expected, pinnedNetns, pinned)
	if err != nil {
		t.Fatalf("valid listener refused: %v", err)
	}
	defer listener.Close()
	accepted := make(chan netip.Addr, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			accepted <- netip.Addr{}
			return
		}
		defer conn.Close()
		accepted <- conn.RemoteAddr().(*net.TCPAddr).AddrPort().Addr().Unmap()
	}()
	client, err := net.DialTimeout("tcp4", expected.String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if got := <-accepted; got != expected.Addr() {
		t.Fatalf("accepted remote %s", got)
	}
}

func TestSpawnedHelperDescriptorAbuseIsRefusedWithoutLeaks(t *testing.T) {
	binary := testBinary(t)
	// Warm the runtime poller before counting descriptors.
	warm := freePort(t)
	if fd, err := spawnHelper(t.Context(), []string{binary, fakeHelperArg, "valid", strconv.Itoa(warm)}); err == nil {
		_ = unix.Close(fd)
	}
	for _, mode := range []string{
		"exit_after_send", "silent_exit", "no_fd", "wrong_payload", "short_payload", "two_fds", "two_messages",
		"not_listening", "udp", "ipv6", "unix_listener", "reuseport", "regular_file",
	} {
		t.Run(mode, func(t *testing.T) {
			port := freePort(t)
			expected := netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port))
			if mode == "ipv6" {
				expected = netip.AddrPortFrom(netip.IPv6Loopback(), uint16(port))
			}
			before := openFDs(t)
			fd, err := spawnHelper(t.Context(), []string{binary, fakeHelperArg, mode, strconv.Itoa(port)})
			if err == nil {
				listener, adoptErr := adoptListener(fd, expected, pinnedNetns, pinned)
				if adoptErr == nil {
					_ = listener.Close()
					t.Fatal("abusive helper descriptor accepted")
				}
			}
			if after := openFDs(t); after != before {
				t.Fatalf("descriptor leak: before %d after %d", before, after)
			}
		})
	}
}

func TestVerifyListenerRefusesAddressAndNamespaceDrift(t *testing.T) {
	port := freePort(t)
	expected := netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port))
	for name, check := range map[string]func(fd int) error{
		"other_port": func(fd int) error {
			return verifyListener(fd, netip.AddrPortFrom(expected.Addr(), expected.Port()+1), pinnedNetns, pinned)
		},
		"other_address": func(fd int) error {
			return verifyListener(fd, netip.AddrPortFrom(netip.MustParseAddr("127.0.0.2"), expected.Port()), pinnedNetns, pinned)
		},
		"other_namespace": func(fd int) error {
			return verifyListener(fd, expected, func(int) (nsID, error) { return nsID{dev: 4, ino: 1}, nil }, pinned)
		},
		"namespace_error": func(fd int) error {
			return verifyListener(fd, expected, func(int) (nsID, error) { return pinned, errors.New("denied") }, pinned)
		},
		"zero_pinned": func(fd int) error {
			return verifyListener(fd, expected, func(int) (nsID, error) { return nsID{}, nil }, nsID{})
		},
		"no_checker": func(fd int) error { return verifyListener(fd, expected, nil, pinned) },
		"invalid":    func(fd int) error { return verifyListener(fd, netip.AddrPort{}, pinnedNetns, pinned) },
	} {
		t.Run(name, func(t *testing.T) {
			fd, err := spawnHelper(t.Context(), []string{testBinary(t), fakeHelperArg, "valid", strconv.Itoa(port)})
			if err != nil {
				t.Fatal(err)
			}
			defer unix.Close(fd)
			if verifyListener(fd, expected, pinnedNetns, pinned) != nil {
				t.Fatal("baseline refused")
			}
			if check(fd) == nil {
				t.Fatal("drift accepted")
			}
		})
	}
	before := openFDs(t)
	fd, err := spawnHelper(t.Context(), []string{testBinary(t), fakeHelperArg, "valid", strconv.Itoa(port)})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := adoptListener(fd, expected, func(int) (nsID, error) { return nsID{dev: 4, ino: 1}, nil }, pinned); err == nil {
		t.Fatal("foreign namespace listener adopted")
	}
	if openFDs(t) != before {
		t.Fatal("refused listener descriptor was not closed")
	}
}

// SIOCGSKNS must fail closed for a caller without CAP_NET_ADMIN over the
// socket's namespace; a privileged caller must get this test's own namespace.
func TestSocketNamespaceUsesKernelIdentity(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	file, err := listener.(*net.TCPListener).File()
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	self, err := inspectSelf("/proc")
	if err != nil {
		t.Fatal(err)
	}
	got, err := socketNetns(int(file.Fd()))
	if err != nil {
		return // refused without namespace authority
	}
	if got != self.net {
		t.Fatalf("socket namespace %v differs from own namespace %v", got, self.net)
	}
}

func TestRealHelperArgumentsAndControlSocket(t *testing.T) {
	binary := testBinary(t)
	for _, args := range [][]string{
		{},
		{"--listen"},
		{"--listen", "127.0.0.1:18080"},
		{"--listen", "8.8.8.8:18080"},
		{"--listen", "172.17.0.1:80"},
		{"--listen", "172.17.0.01:18080"},
		{"--listen", "[fd00::1]:18080"},
		{"--listen", "172.17.0.1:18080", "extra"},
		{"--address", "172.17.0.1:18080"},
	} {
		command := exec.Command(binary, append([]string{"rootlessnetns-real-helper"}, args...)...)
		command.Env = []string{}
		out, err := command.CombinedOutput()
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != helperUsage || len(out) != 0 {
			t.Fatalf("helper args %q: err=%v out=%q", args, err, out)
		}
	}
	// A valid address with fd 3 that is not a SOCK_SEQPACKET Unix socket.
	regular, err := os.Open("/proc/self/status")
	if err != nil {
		t.Fatal(err)
	}
	defer regular.Close()
	command := exec.Command(binary, "rootlessnetns-real-helper", "--listen", "172.17.0.1:18080")
	command.Env = []string{}
	command.ExtraFiles = []*os.File{regular}
	var exit *exec.ExitError
	if err := command.Run(); !errors.As(err, &exit) || exit.ExitCode() != helperFailure {
		t.Fatalf("helper accepted a non-socket control descriptor: %v", err)
	}
}

func localPrivateIPv4(t *testing.T) netip.Addr {
	t.Helper()
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		t.Fatal(err)
	}
	for _, address := range addresses {
		prefix, err := netip.ParsePrefix(address.String())
		if err == nil && prefix.Addr().Is4() && prefix.Addr().IsPrivate() && !prefix.Addr().IsLoopback() {
			return prefix.Addr()
		}
	}
	t.Skip("no local private IPv4 address")
	return netip.Addr{}
}

func TestRealHelperSendsOnlyTheExactListener(t *testing.T) {
	local := localPrivateIPv4(t)
	listener, err := net.Listen("tcp4", netip.AddrPortFrom(local, 0).String())
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	expected := netip.AddrPortFrom(local, uint16(port))
	fd, err := spawnHelper(t.Context(), []string{testBinary(t), "rootlessnetns-real-helper", "--listen", expected.String()})
	if err != nil {
		t.Fatalf("real helper refused: %v", err)
	}
	adopted, err := adoptListener(fd, expected, pinnedNetns, pinned)
	if err != nil {
		t.Fatalf("real helper listener refused: %v", err)
	}
	defer adopted.Close()
	if freebind, err := unix.GetsockoptInt(mustFD(t, adopted), unix.IPPROTO_IP, unix.IP_FREEBIND); err != nil || freebind != 0 {
		t.Fatal("helper listener uses IP_FREEBIND")
	}
	// The bound address is exclusive: no second listener can share it.
	if second, err := net.Listen("tcp4", expected.String()); err == nil {
		_ = second.Close()
		t.Fatal("helper listener did not own its address")
	}
	// A non-local address cannot be bound without IP_FREEBIND.
	if _, err := spawnHelper(t.Context(), []string{testBinary(t), "rootlessnetns-real-helper", "--listen", "10.255.255.254:18080"}); err == nil {
		t.Fatal("helper bound a non-local address")
	}
}

func mustFD(t *testing.T, listener net.Listener) int {
	t.Helper()
	raw, err := listener.(*net.TCPListener).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var fd int
	if raw.Control(func(value uintptr) { fd = int(value) }) != nil {
		t.Fatal("control")
	}
	return fd
}

func TestSpawnHelperRequiresAbsoluteExecutable(t *testing.T) {
	for _, argv := range [][]string{nil, {""}, {"nsenter"}, {"/usr/bin/../bin/nsenter"}} {
		if _, err := spawnHelper(t.Context(), argv); err == nil {
			t.Fatalf("argv %q accepted", argv)
		}
	}
}
