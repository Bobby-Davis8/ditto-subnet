package probe

import "context"

// NetworkCollector is the placeholder for network_enforcement collection. The
// network kind proves per-cgroup connectivity accept rules keyed to the worker
// service, which needs nft/host-firewall authority and a live worker cgroup, so
// it is collected by a later PR. It satisfies the Runner interface today so it
// slots in with no change to the record assembler or the collector.
type NetworkCollector struct{}

// Kind implements Runner.
func (NetworkCollector) Kind() string { return "network_enforcement" }

// Collect always defers. It never returns a partial network record, so no
// network evidence can be minted before the host-firewall collection lands.
func (NetworkCollector) Collect(context.Context, *Session) ([]Phase, error) {
	return nil, ErrDeferredPR4
}

var _ Runner = NetworkCollector{}
var _ Runner = ResourceCollector{}
