package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Deliberately structural only. These bytes may test transport and replay,
// never semantic qualification. This renderer is absent from production builds.
type contractRenderer struct{}

func (contractRenderer) PlanDocument(_ context.Context, r universe.V13FactDocumentRequest) (universe.V13FactDocumentPlan, error) {
	p := universe.V13FactDocumentPlan{}
	for _, record := range r.Records {
		seen := map[string]bool{}
		for _, a := range record.Assertions {
			for _, token := range a.Arguments {
				seen[token] = true
			}
		}
		tokens := make([]string, 0, len(seen))
		for token := range seen {
			tokens = append(tokens, token)
		}
		sort.Strings(tokens)
		text := strings.Join(tokens, " ")
		if record.InteriorFacts {
			text = strings.Repeat(" Neutral texture.", 60) + text + strings.Repeat(" Neutral texture.", 60)
		} else if record.MinBytes > 1 {
			text += strings.Repeat(" Neutral texture.", 15)
		}
		p.Records = append(p.Records, text)
	}
	return p, nil
}
func (contractRenderer) CheckDocument(context.Context, universe.V13FactDocumentRequest, universe.V13FactDocumentPlan) error {
	return nil
}
func (contractRenderer) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
	var p universe.V13FactRenderPlan
	for i := range p.Records {
		p.Records[i] = strings.Join(r.Required[i], " ")
	}
	p.Question = "Question about " + r.Subject
	if r.QuestionTemplate != "" {
		p.Question = r.QuestionTemplate
	}
	for _, f := range r.Facts {
		if f.Mode == "history" && f.Sequence == 0 {
			p.Records[f.Record] = "Initial state: " + p.Records[f.Record]
		}
		if f.Mode == "history" && f.Sequence == 1 {
			p.Records[f.Record] = "Final update: " + p.Records[f.Record]
		}
	}
	return p, nil
}
func (contractRenderer) Check(context.Context, universe.V13FactRenderRequest, universe.V13FactRenderPlan) error {
	return nil
}

func TestFactProducerPlatformContract(t *testing.T) {
	root := os.Getenv("DITTO_FACT_CONTRACT_OUTPUT")
	if root == "" {
		root = t.TempDir()
	}
	for _, size := range []string{"small", "medium", "full"} {
		out := filepath.Join(root, size)
		if err := os.Mkdir(out, 0700); err != nil {
			t.Fatal(err)
		}
		// Negative world seed exercises signed big-endian entropy on both sides.
		artifact, err := gen.GenerateV13FactDataset(context.Background(), 42, -731, 92713, size, contractRenderer{})
		if err != nil {
			t.Fatal(err)
		}
		profile := strings.Repeat("a", 64)
		manifest, err := json.Marshal(map[string]any{"revision": gen.V13FactGenerationRevision, "seed": 42, "world_seed": -731, "presentation_seed": 92713, "run_size": size, "profile_sha256": profile, "qualified": false})
		if err != nil {
			t.Fatal(err)
		}
		if err := writePrivate(out, "generation.json", manifest); err != nil {
			t.Fatal(err)
		}
		if err := writeFactCandidate(out, 42, size, profile, manifest, artifact, 0); err != nil {
			t.Fatal(err)
		}
		for _, name := range []string{"generation.json", "dataset.json", "validation.json", "fact-candidate.json"} {
			info, err := os.Stat(filepath.Join(out, name))
			if err != nil || info.Mode().Perm() != 0600 {
				t.Fatal("output not private", err)
			}
		}
	}
}
