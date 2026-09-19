package privatesurface

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestFactDocumentLayoutStrictAssembly(t *testing.T) {
	for _, raw := range []string{
		`{}`, `{"opening":"a","evidence":"b"}`,
		`{"opening":"a","evidence":"b","closing":" "}`,
		`{"opening":"a","evidence":"b","closing":"c","extra":1}`,
		`{"opening":"a","evidence":"b","closing":"c"} {}`,
		`{"opening":"{{x}}","evidence":"b","closing":"c"}`,
		`{"opening":"a","evidence":"b","closing":"}}"}`,
	} {
		if got, err := assembleFactDocumentLayout(json.RawMessage(raw)); got != "" || !errors.Is(err, errFactStructure) {
			t.Fatalf("invalid sections accepted: %q %v", got, err)
		}
	}
	got, err := assembleFactDocumentLayout(json.RawMessage(`{"opening":" a ","evidence":"{{x}}","closing":" c "}`))
	if err != nil || got != " a \n\n{{x}}\n\n c " {
		t.Fatalf("assembly altered section bytes: %q %v", got, err)
	}
}

func TestFactDocumentLayoutStillRequiresBoundLengthAndInterior(t *testing.T) {
	request := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "story", Bindings: map[string]string{"{{owner0}}": "Ada"}, Records: []universe.V13FactDocumentRecord{{MinBytes: 1800, MaxBytes: 4600, InteriorFacts: true, Assertions: []universe.V13DocumentAssertion{{Kind: "owner", Relation: "initial", Arguments: map[string]string{"person": "{{owner0}}"}}}}}}
	for _, tc := range []struct {
		name, opening, closing string
		valid                  bool
	}{
		{"valid", strings.Repeat("a", 1000), strings.Repeat("b", 1000), true},
		{"short", "a", "b", false},
		{"outside-interior", "a", strings.Repeat("b", 2000), false},
		{"long", strings.Repeat("a", 3000), strings.Repeat("b", 3000), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			raw, _ := json.Marshal(map[string]string{"opening": tc.opening, "evidence": "Initial owner: {{owner0}}.", "closing": tc.closing})
			record, err := assembleFactDocumentLayout(raw)
			if err != nil {
				t.Fatal(err)
			}
			_, err = universe.BindV13FactDocument(request, universe.V13FactDocumentPlan{Records: []string{record}})
			if (err == nil) != tc.valid {
				t.Fatalf("unexpected binder result: %v", err)
			}
		})
	}
}
