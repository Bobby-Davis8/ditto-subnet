package gen

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// An offline answer-key consistency check, never an honest-agent score.
func TestFactCandidateOracleConsistency(t *testing.T) {
	path := os.Getenv("FACT_ORACLE_ARTIFACT")
	if path == "" {
		t.Skip("explicit local artifact required")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != os.Getenv("FACT_ORACLE_SHA256") {
		t.Fatal("artifact pin mismatch")
	}
	a, err := DecodePrivateArtifact(raw, hex.EncodeToString(sum[:]), 731, "small")
	if err != nil {
		t.Fatal("native artifact rejected")
	}
	if a.FactGeneration == nil {
		t.Fatal("not a fact-world artifact")
	}
	for i, c := range a.MemoryCases {
		answer := c.ExpectedAnswer
		want := 1.0
		// Money answer keys store integer minor units; bare prose integers
		// default to major units unless the question explicitly says otherwise.
		if c.AnswerKind == protocol.AnswerMoney {
			answer += " cents"
		}
		// Chitchat has no canonical fact to echo. Exercise its response contract
		// separately, without mistaking an empty answer key for an empty reply.
		if c.AnswerKind == protocol.AnswerChitchat {
			answer = "That sounds nice."
			want = 0.5 // V13's deliberately capped chitchat credit.
		}
		result := grade.Memory(c.MemoryCase, protocol.RunResponse{Answer: answer, FinalText: answer})
		if result.Score != want {
			t.Errorf("case index %d type %s: canonical answer score %.4f notes=%v", i, c.QuestionType, result.Score, result.Notes)
		}
	}
	t.Logf("checked %d canonical memory answers; not honest-agent qualification", len(a.MemoryCases))
}
