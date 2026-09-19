package privatesurface

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"
)

const factDocumentLayoutPrompt = `For this long record, return plan with opening, evidence, and closing strings. The complete record is their concatenation with blank lines. Put ALL argument tokens and factual assertions in evidence. Opening and closing must each be roughly 700-900 bytes of natural, non-factual reflective prose, without tokens, invented events, schema labels, or commentary about this task. Evidence must express the supplied facts naturally, not copy relation instructions or argument labels. Keep the combined bound text within the supplied min_bytes/max_bytes; leave generous margins so every token is within the middle 15%-85%.`

const factDocumentCompactPrompt = `For this compact record, return plan with exactly records, an array of strings in input order. Write concise ordinary prose: usually a few sentences, not a reflective essay or schema explanation. Respect the supplied minimum and maximum byte lengths after binding, leaving ample space for token expansion. Express every assigned fact and token directly and once where possible. A relation's exclusions constrain what you write; do not narrate those exclusions or discuss information in other records. Use natural roles such as client or vendor where appropriate, but never call their values tokens or explain the schema. For a user request, preserve its complete requested scope without padding.`

func factDocumentLayoutSchema() map[string]any {
	return map[string]any{"type": "object", "additionalProperties": false, "required": []string{"opening", "evidence", "closing"}, "properties": map[string]any{"opening": map[string]any{"type": "string"}, "evidence": map[string]any{"type": "string"}, "closing": map[string]any{"type": "string"}}}
}

func assembleFactDocumentLayout(raw json.RawMessage) (string, error) {
	var sections struct{ Opening, Evidence, Closing string }
	d := json.NewDecoder(strings.NewReader(string(raw)))
	d.DisallowUnknownFields()
	if err := d.Decode(&sections); err != nil {
		return "", fmt.Errorf("%w: invalid long-record sections", errFactStructure)
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return "", fmt.Errorf("%w: trailing long-record data", errFactStructure)
	}
	for _, section := range []string{sections.Opening, sections.Evidence, sections.Closing} {
		if strings.TrimSpace(section) == "" {
			return "", fmt.Errorf("%w: opening, evidence and closing are all required", errFactStructure)
		}
	}
	for _, margin := range []string{sections.Opening, sections.Closing} {
		if strings.Contains(margin, "{{") || strings.Contains(margin, "}}") {
			return "", fmt.Errorf("%w: binding tokens belong only in evidence, never opening or closing", errFactStructure)
		}
	}
	// This is layout assembly, not a text rewrite. The existing binder checks
	// the resulting bytes and token positions before independent semantic QA.
	return sections.Opening + "\n\n" + sections.Evidence + "\n\n" + sections.Closing, nil
}
