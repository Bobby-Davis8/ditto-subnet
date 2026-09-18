package universe

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"
)

func TestV13StoryBackgroundIndependentSeededFacts(t *testing.T) {
	a := v13StoryBackground(42, "record-a")
	if len(a) != 6 || !reflect.DeepEqual(a, v13StoryBackground(42, "record-a")) {
		t.Fatal("background is incomplete or nondeterministic")
	}
	if reflect.DeepEqual(a, v13StoryBackground(43, "record-a")) || reflect.DeepEqual(a, v13StoryBackground(42, "record-b")) {
		t.Fatal("background ignores world or record identity")
	}
	w := v13World(42)
	before, err := json.Marshal(w.StoryArcs)
	if err != nil {
		t.Fatal(err)
	}
	questions := w.questionCandidates()
	r, _, err := w.V13StoryDocument(0)
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range r.Records {
		count := 0
		for _, assertion := range record.Assertions {
			if assertion.Kind == "local_background" {
				count++
			}
		}
		if count != 6 {
			t.Fatal("background missing from semantic contract")
		}
	}
	if err := w.RenderV13FactStories(context.Background(), &storyDocumentFixture{}); err != nil {
		t.Fatal(err)
	}
	after, err := json.Marshal(w.StoryArcs)
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(after) || !reflect.DeepEqual(questions, w.questionCandidates()) {
		t.Fatal("background changed scored timeline")
	}
}
