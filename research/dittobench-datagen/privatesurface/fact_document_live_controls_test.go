package privatesurface

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Explicitly paid semantic controls; not candidate or honest-agent qualification.
func TestFactDocumentLiveRestrictionControls(t *testing.T) {
	out := os.Getenv("FACT_RESTRICTION_CONTROLS_OUTPUT")
	if out == "" {
		t.Skip("requires explicit paid opt-in")
	}
	if err := os.Mkdir(out, 0700); err != nil {
		t.Fatal(err)
	}
	write := func(name string, value any) error {
		b, err := json.Marshal(value)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(out, name), b, 0600)
	}
	calls := 0
	var last FactRenderAudit
	p := Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "openai/gpt-5.4-mini", ValidatorProvider: "azure", ValidatorReasoning: "medium"}
	r, err := NewFactRenderer(p, os.Getenv("OPENROUTER_API_KEY"), 4, func(s BudgetSnapshot) error { return write("spend.json", s) }, func(a FactRenderAudit) error {
		calls++
		last = a
		return write(fmt.Sprintf("call-%02d.json", calls), a)
	})
	if err != nil {
		t.Fatal(err)
	}
	request := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "tool_decision", Bindings: map[string]string{"{{subject0}}": "the James Webb telescope"}, Records: []universe.V13FactDocumentRecord{{MinBytes: 1, MaxBytes: 1400, Assertions: []universe.V13DocumentAssertion{{Kind: "stale_background", Relation: "The narrator saved notes about subject last year. These are historical background and may be out of date; they do not establish current facts or recent developments. Do not invent the contents of the notes or a current answer.", Arguments: map[string]string{"subject": "{{subject0}}"}}}}}}
	for _, c := range []struct {
		name, text string
		want       bool
	}{
		{"faithful", "Last year, the narrator saved notes about the James Webb telescope. These serve as historical background and may now be out of date; they do not establish any current facts or recent developments.", true},
		{"reversed_freshness", "Last year, the narrator saved notes about the James Webb telescope. These notes are up to date and establish the current facts and latest developments.", false},
		{"invented_answer", "Last year, the narrator saved notes about the James Webb telescope. They are historical background and may be out of date, not establishing current facts. The notes say it discovered exactly 42 new planets yesterday.", false},
	} {
		bound := universe.V13FactDocumentPlan{Records: []string{c.text}}
		if err := write(c.name+".json", map[string]any{"request": request, "bound": bound, "expected_accepted": c.want}); err != nil {
			t.Fatal(err)
		}
		before := calls
		err := r.CheckDocument(context.Background(), request, bound)
		if calls != before+1 || last.Reason == "" || (!c.want && (err == nil || err.Error() != "fact document: independent semantic rejection")) || (c.want && err != nil) {
			t.Errorf("%s: expected semantic accepted=%t, got %v", c.name, c.want, err)
		}
		t.Logf("%s accepted=%t expected=%t", c.name, err == nil, c.want)
	}
}
