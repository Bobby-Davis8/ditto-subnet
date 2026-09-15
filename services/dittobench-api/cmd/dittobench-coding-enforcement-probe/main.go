// Command dittobench-coding-enforcement-probe is the host-side native
// enforcement evidence runner. It drives only the production coding executor
// and sandbox launch code and assembles records in the catalog's v1 schema for
// the offline verifier to recompute.
//
// It is default-off and inert: it is never invoked from a host workflow, mints
// no approval, reads no custody path and reaches nothing but the local Docker
// daemon selected by DOCKER_HOST. Full multi-kind collection is wired by a
// later PR; this binary provides the assembly path the collector uses and the
// live executor observation the rootless-Docker CI job exercises.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/probe"
)

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "dittobench-coding-enforcement-probe:", err)
		os.Exit(1)
	}
}

func run(args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("a subcommand is required: assemble | resolve-images")
	}
	switch args[0] {
	case "assemble":
		return assemble(args[1:], stdout)
	case "resolve-images":
		return resolveImages(args[1:], stdout)
	default:
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

// envFile is the invariant record context the root collector supplies.
type envFile struct {
	Host struct {
		MachineIDSHA256      string `json:"machine_id_sha256"`
		BootID               string `json:"boot_id"`
		KernelRelease        string `json:"kernel_release"`
		DaemonIdentitySHA256 string `json:"daemon_identity_sha256"`
		RouterNamespace      string `json:"router_namespace"`
		SubordinateIDs       struct {
			UIDStart int64 `json:"uid_start"`
			UIDCount int64 `json:"uid_count"`
			GIDStart int64 `json:"gid_start"`
			GIDCount int64 `json:"gid_count"`
		} `json:"subordinate_ids"`
	} `json:"host"`
	Release struct {
		SourceRevision        string            `json:"source_revision"`
		ReleaseManifestSHA256 string            `json:"release_manifest_sha256"`
		RuntimeArchiveSHA256  string            `json:"runtime_archive_sha256"`
		ImageApprovalSHA256   map[string]string `json:"image_approval_sha256"`
	} `json:"release"`
	Tools                        map[string]string `json:"tools"`
	Inputs                       map[string]string `json:"inputs"`
	PreCollectionPreflightSHA256 string            `json:"pre_collection_preflight_sha256"`
	StartedAtUnix                int64             `json:"started_at_unix"`
	CompletedAtUnix              int64             `json:"completed_at_unix"`
}

type observationsFile struct {
	Phases []struct {
		Name            string `json:"name"`
		StartedAtUnix   int64  `json:"started_at_unix"`
		CompletedAtUnix int64  `json:"completed_at_unix"`
		Observations    []struct {
			ID             string          `json:"id"`
			Language       string          `json:"language"`
			EndpointSHA256 string          `json:"endpoint_sha256"`
			Observed       json.RawMessage `json:"observed"`
		} `json:"observations"`
	} `json:"phases"`
}

func assemble(args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("assemble", flag.ContinueOnError)
	kind := flags.String("kind", "", "evidence kind to assemble")
	envPath := flags.String("env", "", "collection env JSON")
	observationsPath := flags.String("observations", "", "collected observations JSON")
	store := flags.String("store", "", "write the canonical record into this directory, named by its sha256")
	testMode := flags.Bool("test", false, "print only passed/total, never a probe id or observed value")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *kind == "" || *envPath == "" || *observationsPath == "" {
		return errors.New("--kind, --env and --observations are required")
	}
	cat, err := catalog.Load()
	if err != nil {
		return err
	}
	env, err := readEnv(*envPath)
	if err != nil {
		return err
	}
	phases, err := readObservations(*observationsPath)
	if err != nil {
		return err
	}
	result, err := probe.AssembleRecord(cat, env, *kind, catalog.Endpoints{}, phases)
	if err != nil {
		return err
	}
	if *testMode {
		return json.NewEncoder(stdout).Encode(map[string]int{"passed": result.Passed, "total": result.Total})
	}
	if *store != "" {
		sum := sha256.Sum256(result.Canonical)
		name := hex.EncodeToString(sum[:])
		target := *store + string(os.PathSeparator) + name
		if err := os.WriteFile(target, result.Canonical, 0o400); err != nil {
			return err
		}
		return json.NewEncoder(stdout).Encode(map[string]string{"record_sha256": name})
	}
	if _, err := stdout.Write(result.Canonical); err != nil {
		return err
	}
	return nil
}

func readEnv(path string) (probe.Env, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return probe.Env{}, err
	}
	var file envFile
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&file); err != nil {
		return probe.Env{}, err
	}
	env := probe.Env{
		Host: probe.HostBinding{
			MachineIDSHA256:      file.Host.MachineIDSHA256,
			BootID:               file.Host.BootID,
			KernelRelease:        file.Host.KernelRelease,
			DaemonIdentitySHA256: file.Host.DaemonIdentitySHA256,
			RouterNamespace:      file.Host.RouterNamespace,
			Subordinate: catalog.SubordinateIDs{
				UIDStart: file.Host.SubordinateIDs.UIDStart, UIDCount: file.Host.SubordinateIDs.UIDCount,
				GIDStart: file.Host.SubordinateIDs.GIDStart, GIDCount: file.Host.SubordinateIDs.GIDCount,
			},
		},
		Release: probe.ReleaseBinding{
			SourceRevision:        file.Release.SourceRevision,
			ReleaseManifestSHA256: file.Release.ReleaseManifestSHA256,
			RuntimeArchiveSHA256:  file.Release.RuntimeArchiveSHA256,
			ImageApprovalSHA256:   file.Release.ImageApprovalSHA256,
		},
		Tools:                        file.Tools,
		ProfileInputs:                file.Inputs,
		PreCollectionPreflightSHA256: file.PreCollectionPreflightSHA256,
		StartedAtUnix:                file.StartedAtUnix,
		CompletedAtUnix:              file.CompletedAtUnix,
	}
	return env, nil
}

func readObservations(path string) ([]probe.Phase, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var file observationsFile
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&file); err != nil {
		return nil, err
	}
	phases := make([]probe.Phase, 0, len(file.Phases))
	for _, phase := range file.Phases {
		out := probe.Phase{Name: phase.Name, StartedAtUnix: phase.StartedAtUnix, CompletedAtUnix: phase.CompletedAtUnix}
		for _, observation := range phase.Observations {
			observed, err := decodeObservedLeaves(observation.Observed)
			if err != nil {
				return nil, fmt.Errorf("observed for %q: %w", observation.ID, err)
			}
			out.Observations = append(out.Observations, probe.Observation{
				ID: observation.ID, Language: observation.Language, EndpointSHA256: observation.EndpointSHA256, Observed: observed,
			})
		}
		phases = append(phases, out)
	}
	return phases, nil
}

// decodeObservedLeaves converts an observed object's JSON into the int64/bool/
// string/[]string leaves the record assembler and verifier accept. Numbers are
// decoded as integers so no float ever reaches a record.
func decodeObservedLeaves(raw json.RawMessage) (map[string]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var object map[string]any
	if err := decoder.Decode(&object); err != nil {
		return nil, err
	}
	result := make(map[string]any, len(object))
	for key, value := range object {
		leaf, err := observedLeaf(value)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", key, err)
		}
		result[key] = leaf
	}
	return result, nil
}

func observedLeaf(value any) (any, error) {
	switch typed := value.(type) {
	case bool:
		return typed, nil
	case string:
		return typed, nil
	case json.Number:
		integer, err := typed.Int64()
		if err != nil {
			return nil, fmt.Errorf("only integer observations are allowed: %q", typed.String())
		}
		return integer, nil
	case []any:
		list := make([]string, 0, len(typed))
		for _, item := range typed {
			text, ok := item.(string)
			if !ok {
				return nil, errors.New("observed list entries must be strings")
			}
			list = append(list, text)
		}
		return list, nil
	default:
		return nil, fmt.Errorf("unsupported observed value %T", value)
	}
}

func resolveImages(args []string, stdout io.Writer) error {
	flags := flag.NewFlagSet("resolve-images", flag.ContinueOnError)
	if err := flags.Parse(args); err != nil {
		return err
	}
	references := flags.Args()
	if len(references) == 0 {
		return errors.New("at least one approved registry@sha256 image reference is required")
	}
	docker := probe.ExecDocker{}
	resolved := make(map[string]string, len(references))
	for _, reference := range references {
		image, err := probe.ResolveApprovedImage(context.Background(), docker, reference)
		if err != nil {
			return err
		}
		resolved[reference] = image.ID
	}
	return json.NewEncoder(stdout).Encode(resolved)
}
