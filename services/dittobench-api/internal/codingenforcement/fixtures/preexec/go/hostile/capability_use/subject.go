package subject

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

var (
	_ = errors.Is
	_ = exec.Command
	_ = os.Getpid
	_ = syscall.EPERM
)

func init() {
}

func Add(a, b int) int {
	if data, _ := os.ReadFile("/proc/self/status"); len(data) == 0 {
		panic("no status")
	}
	return a + b
}

func Length(values []string) int { return len(values) }
