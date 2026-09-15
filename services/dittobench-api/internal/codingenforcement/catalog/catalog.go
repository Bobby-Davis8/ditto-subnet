// Package catalog embeds the native enforcement probe catalog, its canonical
// JSON encoding and the expectation semantics that decide a probe's matched
// value.
//
// catalog-v1.json is the single source of truth. The offline Python evidence
// tool (infra/scripts/coding-native-evidence.py) reads the same file, and both
// sides pin the same golden vectors. Nothing here runs a probe, contacts a
// host or mints approval.
package catalog

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"slices"
	"strings"
)

//go:embed catalog-v1.json
var catalogBytes []byte

// Schemas and versioned constants. A change to any of these is a new version
// in the catalog file, this package and the Python verifier together.
const (
	CatalogSchema       = "dittobench-coding-native-enforcement-catalog-v1"
	RecordSchema        = "dittobench-coding-native-enforcement-evidence-v1"
	ReviewSchema        = "dittobench-coding-native-evidence-review-v1"
	Coverage            = "same_boot"
	FreshnessMaxSeconds = 21600

	TolerancesVersion                   = "dittobench-coding-native-enforcement-tolerances-v1"
	CPUUsageMaxPermilleOfQuota          = 1150
	MemoryPeakMaxPermilleOfLimit        = 1000
	PidsMaxPermilleOfLimit              = 1000
	NofileMaxPermilleOfLimit            = 1000
	ScratchMaxPermilleOfLimit           = 1000
	LogMaxPermilleOfLimit               = 1000
	TimeoutElapsedMaxPermilleOfDeadline = 1100
)

// Fixed catalog vocabularies.
var (
	Languages        = []string{"go", "node", "python", "rust"}
	RouterNamespaces = []string{"host", "rootless-netns"}
	NotCovered       = []string{"daemon_restart_recovery", "reboot_recovery"}
	Kinds            = []string{"network_enforcement", "resource_enforcement", "preexec_confinement", "cleanup_recovery"}
)

// Probe scopes: once per record, once per approved language image, or once
// per trusted endpoint listed in the record.
const (
	ScopeHost            = "host"
	ScopeLanguage        = "language"
	ScopeTrustedEndpoint = "trusted_endpoint"
)

var (
	probeID   = regexp.MustCompile(`^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}$`)
	name      = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// Tolerances are integer per-mille bounds, so no float enters a record.
type Tolerances struct {
	Version                             string `json:"version"`
	CPUUsageMaxPermilleOfQuota          int64  `json:"cpu_usage_max_permille_of_quota"`
	MemoryPeakMaxPermilleOfLimit        int64  `json:"memory_peak_max_permille_of_limit"`
	PidsMaxPermilleOfLimit              int64  `json:"pids_max_permille_of_limit"`
	NofileMaxPermilleOfLimit            int64  `json:"nofile_max_permille_of_limit"`
	ScratchMaxPermilleOfLimit           int64  `json:"scratch_max_permille_of_limit"`
	LogMaxPermilleOfLimit               int64  `json:"log_max_permille_of_limit"`
	TimeoutElapsedMaxPermilleOfDeadline int64  `json:"timeout_elapsed_max_permille_of_deadline"`
}

// Permille returns the named tolerance.
func (t Tolerances) Permille(tolerance string) (int64, bool) {
	switch tolerance {
	case "cpu_usage_max_permille_of_quota":
		return t.CPUUsageMaxPermilleOfQuota, true
	case "memory_peak_max_permille_of_limit":
		return t.MemoryPeakMaxPermilleOfLimit, true
	case "pids_max_permille_of_limit":
		return t.PidsMaxPermilleOfLimit, true
	case "nofile_max_permille_of_limit":
		return t.NofileMaxPermilleOfLimit, true
	case "scratch_max_permille_of_limit":
		return t.ScratchMaxPermilleOfLimit, true
	case "log_max_permille_of_limit":
		return t.LogMaxPermilleOfLimit, true
	case "timeout_elapsed_max_permille_of_deadline":
		return t.TimeoutElapsedMaxPermilleOfDeadline, true
	}
	return 0, false
}

// VersionedTolerances are the constants every catalog must carry.
var VersionedTolerances = Tolerances{
	Version:                             TolerancesVersion,
	CPUUsageMaxPermilleOfQuota:          CPUUsageMaxPermilleOfQuota,
	MemoryPeakMaxPermilleOfLimit:        MemoryPeakMaxPermilleOfLimit,
	PidsMaxPermilleOfLimit:              PidsMaxPermilleOfLimit,
	NofileMaxPermilleOfLimit:            NofileMaxPermilleOfLimit,
	ScratchMaxPermilleOfLimit:           ScratchMaxPermilleOfLimit,
	LogMaxPermilleOfLimit:               LogMaxPermilleOfLimit,
	TimeoutElapsedMaxPermilleOfDeadline: TimeoutElapsedMaxPermilleOfDeadline,
}

// Bounds limits how many endpoints of one role a record lists.
type Bounds struct {
	Min int `json:"min"`
	Max int `json:"max"`
}

// Probe is one catalog entry.
type Probe struct {
	ID     string      `json:"id"`
	Phase  string      `json:"phase"`
	Scope  string      `json:"scope"`
	Expect Expectation `json:"expect"`
}

// Kind is the probe set of one evidence kind.
type Kind struct {
	Inputs        []string          `json:"inputs"`
	EndpointRoles map[string]Bounds `json:"endpoint_roles"`
	Phases        []string          `json:"phases"`
	Probes        []Probe           `json:"probes"`
}

// Catalog is the decoded catalog-v1.json.
type Catalog struct {
	Schema              string          `json:"schema"`
	RecordSchema        string          `json:"record_schema"`
	ReviewSchema        string          `json:"review_schema"`
	Languages           []string        `json:"languages"`
	RouterNamespaces    []string        `json:"router_namespaces"`
	Coverage            string          `json:"coverage"`
	NotCovered          []string        `json:"not_covered"`
	FreshnessMaxSeconds int64           `json:"freshness_max_seconds"`
	Tolerances          Tolerances      `json:"tolerances"`
	Outcomes            []string        `json:"outcomes"`
	Kinds               map[string]Kind `json:"kinds"`
}

// Instance is one required probe occurrence in a record. Language and
// EndpointSHA256 are empty when the record carries null.
type Instance struct {
	ID             string
	Language       string
	EndpointSHA256 string
	Phase          string
	Expect         Expectation
}

// Bytes returns a copy of the embedded catalog file.
func Bytes() []byte { return bytes.Clone(catalogBytes) }

// SHA256 is the hex digest of the embedded catalog file bytes, the value a
// record binds as tools.catalog_sha256.
func SHA256() string {
	sum := sha256.Sum256(catalogBytes)
	return hex.EncodeToString(sum[:])
}

// Load decodes and validates the embedded catalog.
func Load() (*Catalog, error) { return Parse(catalogBytes) }

// Parse decodes and validates catalog bytes with closed keys.
func Parse(raw []byte) (*Catalog, error) {
	// The strict decoder refuses duplicate keys and non-integer numbers first.
	if _, err := Decode(raw); err != nil {
		return nil, fmt.Errorf("catalog: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var value Catalog
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("catalog: decode: %w", err)
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("catalog: trailing data")
	}
	if err := value.validate(); err != nil {
		return nil, fmt.Errorf("catalog: %w", err)
	}
	return &value, nil
}

func (c *Catalog) validate() error {
	switch {
	case c.Schema != CatalogSchema, c.RecordSchema != RecordSchema, c.ReviewSchema != ReviewSchema:
		return errors.New("schema differs")
	case !slices.Equal(c.Languages, Languages):
		return errors.New("languages differ")
	case !slices.Equal(c.RouterNamespaces, RouterNamespaces):
		return errors.New("router namespaces differ")
	case c.Coverage != Coverage, !slices.Equal(c.NotCovered, NotCovered):
		return errors.New("coverage differs")
	case c.FreshnessMaxSeconds != FreshnessMaxSeconds:
		return errors.New("freshness differs")
	case c.Tolerances != VersionedTolerances:
		return errors.New("tolerances differ")
	}
	if len(c.Outcomes) == 0 || !slices.IsSorted(c.Outcomes) || len(slices.Compact(slices.Clone(c.Outcomes))) != len(c.Outcomes) {
		return errors.New("outcomes are malformed")
	}
	for _, outcome := range c.Outcomes {
		if !name.MatchString(outcome) {
			return errors.New("outcomes are malformed")
		}
	}
	if len(c.Kinds) != len(Kinds) {
		return errors.New("kinds differ")
	}
	outcomes := c.OutcomeSet()
	for _, kindName := range Kinds {
		kind, ok := c.Kinds[kindName]
		if !ok {
			return errors.New("kinds differ")
		}
		if err := kind.validate(outcomes); err != nil {
			return fmt.Errorf("%s: %w", kindName, err)
		}
	}
	return nil
}

func (k Kind) validate(outcomes map[string]bool) error {
	if k.Inputs == nil || !slices.IsSorted(k.Inputs) || len(slices.Compact(slices.Clone(k.Inputs))) != len(k.Inputs) {
		return errors.New("inputs are malformed")
	}
	for _, input := range k.Inputs {
		if !name.MatchString(input) || !strings.HasSuffix(input, "_sha256") {
			return errors.New("inputs are malformed")
		}
	}
	if k.EndpointRoles == nil {
		return errors.New("endpoint roles are malformed")
	}
	for role, bounds := range k.EndpointRoles {
		if !name.MatchString(role) || bounds.Min < 1 || bounds.Max < bounds.Min {
			return errors.New("endpoint role is malformed")
		}
	}
	if len(k.Phases) == 0 {
		return errors.New("phases are malformed")
	}
	phases := map[string]bool{} // phase -> used by a probe
	for _, phase := range k.Phases {
		if _, repeated := phases[phase]; repeated || !name.MatchString(phase) {
			return errors.New("phases are malformed")
		}
		phases[phase] = false
	}
	if len(k.Probes) == 0 {
		return errors.New("probes are empty")
	}
	seen := map[string]bool{}
	for _, probe := range k.Probes {
		if !probeID.MatchString(probe.ID) || seen[probe.ID] {
			return errors.New("probe id is malformed or repeated")
		}
		seen[probe.ID] = true
		if _, known := phases[probe.Phase]; !known {
			return fmt.Errorf("%s: phase is unknown", probe.ID)
		}
		phases[probe.Phase] = true
		switch probe.Scope {
		case ScopeHost, ScopeLanguage:
		case ScopeTrustedEndpoint:
			if _, ok := k.EndpointRoles["trusted"]; !ok {
				return fmt.Errorf("%s: needs trusted endpoints", probe.ID)
			}
		default:
			return fmt.Errorf("%s: scope is unknown", probe.ID)
		}
		if err := probe.Expect.validate(outcomes); err != nil {
			return fmt.Errorf("%s: %w", probe.ID, err)
		}
	}
	for _, used := range phases {
		if !used {
			return errors.New("a phase has no probes")
		}
	}
	return nil
}

// OutcomeSet is the observed outcome vocabulary.
func (c *Catalog) OutcomeSet() map[string]bool {
	result := make(map[string]bool, len(c.Outcomes))
	for _, outcome := range c.Outcomes {
		result[outcome] = true
	}
	return result
}

// RequiredInstances expands one kind's probes over every language image and
// every trusted endpoint hash, in record order: catalog phase order, then id,
// language and endpoint hash within a phase.
func (c *Catalog) RequiredInstances(kindName string, trustedEndpoints []string) ([]Instance, error) {
	kind, ok := c.Kinds[kindName]
	if !ok {
		return nil, errors.New("catalog: kind is unknown")
	}
	for _, endpoint := range trustedEndpoints {
		if !sha256Hex.MatchString(endpoint) {
			return nil, errors.New("catalog: trusted endpoint hash is malformed")
		}
	}
	var result []Instance
	for _, probe := range kind.Probes {
		base := Instance{ID: probe.ID, Phase: probe.Phase, Expect: probe.Expect}
		switch probe.Scope {
		case ScopeHost:
			result = append(result, base)
		case ScopeLanguage:
			for _, language := range Languages {
				instance := base
				instance.Language = language
				result = append(result, instance)
			}
		case ScopeTrustedEndpoint:
			for _, endpoint := range trustedEndpoints {
				instance := base
				instance.EndpointSHA256 = endpoint
				result = append(result, instance)
			}
		}
	}
	slices.SortFunc(result, func(left, right Instance) int {
		if order := slices.Index(kind.Phases, left.Phase) - slices.Index(kind.Phases, right.Phase); order != 0 {
			return order
		}
		return strings.Compare(left.ID+"\x00"+left.Language+"\x00"+left.EndpointSHA256, right.ID+"\x00"+right.Language+"\x00"+right.EndpointSHA256)
	})
	return result, nil
}
