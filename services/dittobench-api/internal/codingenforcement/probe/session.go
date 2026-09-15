package probe

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

// runLabelKey is the label every container, network and volume this run creates
// carries, so cleanup and residue checks only ever touch this run's resources.
// A host-wide prune is never used.
const runLabelKey = "io.heyditto.dittobench.native-enforcement-probe"

// Session is one collection's shared context: the daemon handle, catalog,
// approved images, the invariant record env and a unique run id used to label
// and later reclaim only this run's resources.
type Session struct {
	Docker  DockerCLI
	Catalog *catalog.Catalog
	Env     Env
	// Images maps each container class to its resolved approved image.
	Images map[string]ResolvedImage
	// RunID labels this run's resources; residue checks assert none survive.
	RunID string
}

// NewSession pins the daemon posture and a unique run id. It does not create any
// container; each runner does that through the production launch code.
func NewSession(ctx context.Context, docker DockerCLI, cat *catalog.Catalog, env Env) (*Session, error) {
	if docker == nil || cat == nil {
		return nil, fmt.Errorf("probe: docker and catalog are required")
	}
	if err := requireRootlessIsolatedDaemon(ctx, docker); err != nil {
		return nil, err
	}
	id, err := randomHex(8)
	if err != nil {
		return nil, err
	}
	return &Session{Docker: docker, Catalog: cat, Env: env, Images: map[string]ResolvedImage{}, RunID: id}, nil
}

// RunLabel is the exact label=value this run owns.
func (s *Session) RunLabel() string { return runLabelKey + "=" + s.RunID }

// residueClear reports zero surviving containers, job networks and volumes that
// carry this run's label. It is what the collector records as a clear residue.
func (s *Session) residueClear(ctx context.Context) error {
	for _, target := range [][]string{
		{"ps", "-aq", "--filter", "label=" + s.RunLabel()},
		{"network", "ls", "-q", "--filter", "label=" + s.RunLabel()},
		{"volume", "ls", "-q", "--filter", "label=" + s.RunLabel()},
	} {
		out, err := s.Docker.Output(ctx, target...)
		if err != nil {
			return fmt.Errorf("probe: residue check failed: %s", strings.TrimSpace(string(out)))
		}
		if strings.TrimSpace(string(out)) != "" {
			return fmt.Errorf("probe: run %s left %s residue", s.RunID, target[0])
		}
	}
	return nil
}

// removeOwned force-removes only resources carrying this run's label. It never
// prunes and never touches another run's ids.
func (s *Session) removeOwned(ctx context.Context) {
	if out, err := s.Docker.Output(ctx, "ps", "-aq", "--filter", "label="+s.RunLabel()); err == nil {
		for _, id := range strings.Fields(string(out)) {
			_, _ = s.Docker.Output(ctx, "rm", "-f", "-v", id)
		}
	}
	if out, err := s.Docker.Output(ctx, "network", "ls", "-q", "--filter", "label="+s.RunLabel()); err == nil {
		for _, id := range strings.Fields(string(out)) {
			_, _ = s.Docker.Output(ctx, "network", "rm", id)
		}
	}
}

func randomHex(size int) (string, error) {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		return "", fmt.Errorf("probe: identity: %w", err)
	}
	return hex.EncodeToString(value), nil
}
