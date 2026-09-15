package probe

import (
	"context"
	"errors"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

// ErrDeferredPR4 is returned by the network runner. Network enforcement needs
// nft/host-firewall authority and a live worker cgroup, so it is collected by a
// later PR; the interface exists here so it slots in with no runner change.
var ErrDeferredPR4 = errors.New("probe: network enforcement collection is deferred")

// ErrHelperUnavailable marks a measured probe whose in-image helper is not yet
// present in the approved runtime image. The runner never fabricates a limit
// event, so a missing helper is surfaced, never silently passed.
var ErrHelperUnavailable = errors.New("probe: in-image enforcement helper is unavailable")

// Fixed precondition and residue shapes. They mirror PRECONDITIONS and RESIDUE
// in the offline verifier exactly; a record is refused unless they are clear.
var (
	clearPreconditions = map[string]any{
		"custody_active":         false,
		"custody_socket_present": false,
		"daemon_containers":      int64(0),
		"daemon_job_networks":    int64(0),
		"worker_active":          false,
	}
	clearResidue = map[string]any{
		"containers":             int64(0),
		"custody_socket_present": false,
		"job_networks":           int64(0),
		"processes":              int64(0),
		"volumes":                int64(0),
		"worker_active":          false,
	}
)

// HostBinding is the record's host identity. The collector fills it from the
// post-collection preflight; the runner treats it as opaque.
type HostBinding struct {
	MachineIDSHA256      string
	BootID               string
	KernelRelease        string
	DaemonIdentitySHA256 string
	Subordinate          catalog.SubordinateIDs
	RouterNamespace      string
}

// ReleaseBinding is the record's release identity.
type ReleaseBinding struct {
	SourceRevision        string
	ReleaseManifestSHA256 string
	RuntimeArchiveSHA256  string
	ImageApprovalSHA256   map[string]string
}

// Env is the invariant context every record in one collection shares. It is
// supplied by the root collector, never invented by a runner.
type Env struct {
	Host                         HostBinding
	Release                      ReleaseBinding
	Tools                        map[string]string
	ProfileInputs                map[string]string
	PreCollectionPreflightSHA256 string
	StartedAtUnix                int64
	CompletedAtUnix              int64
}

// Observation is one probe occurrence's measured result. Observed carries only
// int64, bool, string, and []string leaves, exactly the closed shapes the
// verifier accepts; matched is recomputed from the catalog, never stored here.
type Observation struct {
	ID             string
	Language       string
	EndpointSHA256 string
	Observed       map[string]any
}

// key identifies the required catalog instance an observation satisfies.
func (o Observation) key() instanceKey {
	return instanceKey{ID: o.ID, Language: o.Language, EndpointSHA256: o.EndpointSHA256}
}

type instanceKey struct {
	ID             string
	Language       string
	EndpointSHA256 string
}

// Phase is one catalog phase's observations with its measured wall-clock bounds.
type Phase struct {
	Name            string
	StartedAtUnix   int64
	CompletedAtUnix int64
	Observations    []Observation
}

// Runner collects one evidence kind by driving the production launch code. It
// returns the phases in catalog order with one observation per required probe
// instance; AssembleRecord validates completeness and recomputes matched.
type Runner interface {
	// Kind is one of the catalog kinds this runner collects.
	Kind() string
	// Collect runs the kind's probes over the production executor and sandbox.
	Collect(ctx context.Context, session *Session) ([]Phase, error)
}
