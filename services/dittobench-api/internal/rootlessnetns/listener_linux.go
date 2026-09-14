package rootlessnetns

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

var ErrListener = errors.New("rootless router listener rejected")

const (
	// Protocol is the only payload accepted alongside the passed descriptor.
	Protocol = "dittobench-rootless-router-listener-v1"
	// NsenterExecutable is util-linux nsenter on Debian 13 and Ubuntu 24.04.
	NsenterExecutable = "/usr/bin/nsenter"
	// HelperExecutableName is installed beside the hosted worker in the bundle.
	HelperExecutableName = "dittobench-coding-router-listener"

	helperSocketFD = 3
	helperUserFD   = 4
	helperNetFD    = 5
	helperTimeout  = 15 * time.Second
)

// Config names the exact in-namespace address and the local trusted inputs.
// Callers must verify both executables' installation protection first.
type Config struct {
	Address          netip.AddrPort
	DockerSocket     string
	HelperExecutable string
}

type system struct {
	runRoot  string
	procRoot string
	nsenter  string
}

func productionSystem() system {
	return system{runRoot: runUserRoot, procRoot: "/proc", nsenter: NsenterExecutable}
}

// Listen returns a listener created inside the configured rootless daemon's
// RootlessKit network namespace, or ErrListener. It never falls back to the
// host network namespace.
func Listen(ctx context.Context, config Config) (net.Listener, error) {
	return listen(ctx, config, productionSystem())
}

// ValidAddress is the shared router address policy: explicit private IPv4,
// never loopback, and an unprivileged port.
func ValidAddress(address netip.AddrPort) bool {
	ip := address.Addr()
	return address.IsValid() && ip.Is4() && ip.IsPrivate() && !ip.IsLoopback() && address.Port() >= 1024
}

func listen(ctx context.Context, config Config, sys system) (net.Listener, error) {
	if ctx == nil || ctx.Err() != nil || !ValidAddress(config.Address) || !cleanAbsolute(config.HelperExecutable) ||
		!cleanAbsolute(config.DockerSocket) || !cleanAbsolute(sys.nsenter) {
		return nil, ErrListener
	}
	euid := os.Geteuid()
	pid, err := readChildPID(sys.runRoot, euid)
	if err != nil {
		return nil, ErrListener
	}
	self, err := inspectSelf(sys.procRoot)
	if err != nil {
		return nil, ErrListener
	}
	child, err := inspectProcess(sys.procRoot, pid)
	if err != nil {
		return nil, ErrListener
	}
	defer child.Close()
	peerPID, peerUID, err := daemonPeer(ctx, config.DockerSocket)
	if err != nil {
		return nil, ErrListener
	}
	daemon, err := inspectProcess(sys.procRoot, peerPID)
	if err != nil {
		return nil, ErrListener
	}
	daemon.Close()
	if !validTopology(uint32(euid), self, child.facts, daemon.facts, peerUID) {
		return nil, ErrListener
	}
	argv := []string{
		sys.nsenter,
		// The descriptors below are the inspected namespaces, inherited at
		// fixed numbers. Credentials are preserved: no setuid/setgid/setgroups.
		"--user=/proc/self/fd/" + strconv.Itoa(helperUserFD), "--net=/proc/self/fd/" + strconv.Itoa(helperNetFD),
		"--preserve-credentials",
		"--", config.HelperExecutable, "--listen", config.Address.String(),
	}
	// ExtraFiles start at helperSocketFD, then helperUserFD and helperNetFD.
	fd, err := spawnHelper(ctx, argv, child.user, child.net)
	if err != nil {
		return nil, ErrListener
	}
	return adoptListener(fd, config.Address, socketNetns, child.facts.net)
}

func cleanAbsolute(path string) bool {
	return filepath.IsAbs(path) && filepath.Clean(path) == path
}

// spawnHelper starts argv with fd 3 as the child end of a SOCK_SEQPACKET
// socketpair and optional descriptors at 4 and above. It returns exactly one
// received descriptor only when the helper sent exactly one protocol message
// with exactly one SCM_RIGHTS descriptor and exited successfully.
func spawnHelper(ctx context.Context, argv []string, extra ...*os.File) (int, error) {
	if len(argv) == 0 || !cleanAbsolute(argv[0]) {
		return -1, ErrListener
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_SEQPACKET|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return -1, ErrListener
	}
	parentFile := os.NewFile(uintptr(pair[0]), "router-listener-parent")
	childFile := os.NewFile(uintptr(pair[1]), "router-listener-child")
	defer childFile.Close()
	conn, err := net.FileConn(parentFile)
	_ = parentFile.Close()
	if err != nil {
		return -1, ErrListener
	}
	defer conn.Close()
	parent, ok := conn.(*net.UnixConn)
	if !ok {
		return -1, ErrListener
	}
	call, cancel := context.WithTimeout(ctx, helperTimeout)
	defer cancel()
	command := exec.CommandContext(call, argv[0], argv[1:]...)
	command.Env = []string{}
	command.Dir = "/"
	command.ExtraFiles = append([]*os.File{childFile}, extra...)
	command.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
	command.WaitDelay = time.Second
	if command.Start() != nil {
		return -1, ErrListener
	}
	_ = childFile.Close()
	deadline := time.Now().Add(helperTimeout)
	if parent.SetReadDeadline(deadline) != nil {
		_ = command.Process.Kill()
		_ = command.Wait()
		return -1, ErrListener
	}
	payload := make([]byte, len(Protocol)+1)
	oob := make([]byte, unix.CmsgSpace(8*4))
	n, oobn, flags, _, readErr := parent.ReadMsgUnix(payload, oob)
	received := receivedRights(oob[:oobn])
	waitErr := command.Wait()
	// After exit no helper copy of fd 3 remains, so a second message is visible
	// before end of stream. Its descriptors are closed and the result refused.
	extraN, extraOOBN, _, _, extraErr := parent.ReadMsgUnix(make([]byte, 1), oob)
	closeAll(receivedRights(oob[:extraOOBN]))
	if readErr != nil || waitErr != nil || flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0 || string(payload[:n]) != Protocol ||
		len(received) != 1 || extraN != 0 || extraOOBN != 0 || (extraErr != nil && !errors.Is(extraErr, io.EOF)) {
		closeAll(received)
		return -1, ErrListener
	}
	return received[0], nil
}

// receivedRights returns every descriptor received, including descriptors in
// malformed or unexpected control messages, so the caller can close them.
// Parsing failures after a descriptor is installed cannot be distinguished, so
// any message that is not exactly SCM_RIGHTS contributes a sentinel that makes
// the receive count invalid.
func receivedRights(oob []byte) []int {
	if len(oob) == 0 {
		return nil
	}
	messages, err := unix.ParseSocketControlMessage(oob)
	if err != nil {
		return []int{-1, -1}
	}
	var fds []int
	for i := range messages {
		rights, err := unix.ParseUnixRights(&messages[i])
		if err != nil {
			fds = append(fds, -1)
			continue
		}
		fds = append(fds, rights...)
	}
	return fds
}

func closeAll(fds []int) {
	for _, fd := range fds {
		if fd >= 0 {
			_ = unix.Close(fd)
		}
	}
}

// adoptListener takes ownership of fd. It verifies the descriptor before any
// Go wrapper is created and closes it on every refusal.
func adoptListener(fd int, expected netip.AddrPort, netnsOf func(int) (nsID, error), want nsID) (net.Listener, error) {
	if fd < 0 {
		return nil, ErrListener
	}
	if verifyListener(fd, expected, netnsOf, want) != nil {
		_ = unix.Close(fd)
		return nil, ErrListener
	}
	file := os.NewFile(uintptr(fd), "rootless-router-listener")
	listener, err := net.FileListener(file)
	_ = file.Close()
	if err != nil {
		return nil, ErrListener
	}
	address, ok := listener.Addr().(*net.TCPAddr)
	if !ok || netip.AddrPortFrom(address.AddrPort().Addr().Unmap(), address.AddrPort().Port()) != expected {
		_ = listener.Close()
		return nil, ErrListener
	}
	return listener, nil
}

// verifyListener requires a listening IPv4 TCP socket bound exactly to the
// expected address, without SO_REUSEPORT (so no other socket can share the
// port), whose network namespace is the pinned RootlessKit namespace.
func verifyListener(fd int, expected netip.AddrPort, netnsOf func(int) (nsID, error), want nsID) error {
	var stat unix.Stat_t
	if !expected.IsValid() || want == (nsID{}) || netnsOf == nil ||
		unix.Fstat(fd, &stat) != nil || stat.Mode&unix.S_IFMT != unix.S_IFSOCK {
		return ErrListener
	}
	for _, option := range []struct{ name, want int }{
		{unix.SO_DOMAIN, unix.AF_INET},
		{unix.SO_TYPE, unix.SOCK_STREAM},
		{unix.SO_PROTOCOL, unix.IPPROTO_TCP},
		{unix.SO_ACCEPTCONN, 1},
		{unix.SO_REUSEPORT, 0},
	} {
		if got, err := unix.GetsockoptInt(fd, unix.SOL_SOCKET, option.name); err != nil || got != option.want {
			return ErrListener
		}
	}
	name, err := unix.Getsockname(fd)
	local, ok := name.(*unix.SockaddrInet4)
	if err != nil || !ok || netip.AddrFrom4(local.Addr) != expected.Addr() || local.Port != int(expected.Port()) {
		return ErrListener
	}
	if got, err := netnsOf(fd); err != nil || got != want {
		return ErrListener
	}
	return nil
}

// socketNetns asks the kernel for the socket's network namespace. SIOCGSKNS
// requires CAP_NET_ADMIN over that namespace's owner, which the owning UID
// holds for RootlessKit's user namespace; anything else refuses.
func socketNetns(fd int) (nsID, error) {
	return relatedIdentity(fd, unix.SIOCGSKNS, unix.CLONE_NEWNET)
}
