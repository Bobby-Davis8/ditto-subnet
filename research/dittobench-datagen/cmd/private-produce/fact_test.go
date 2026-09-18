package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
)

func TestFactProducerPreflightNeverDispatches(t *testing.T) {
	t.Setenv("OPENROUTER_API_KEY", "")
	p := privatesurface.Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
	for _, size := range []string{"invalid", "small"} {
		out := filepath.Join(t.TempDir(), "candidate")
		if err := runFactProducer(42, size, out, p, 1, ""); err == nil {
			t.Fatal("missing key or invalid size accepted")
		}
		if _, err := os.Stat(filepath.Join(out, "dataset.json")); !os.IsNotExist(err) {
			t.Fatal("failed preflight emitted dataset")
		}
	}
	if err := runFactProducer(42, "small", t.TempDir(), p, 1, ""); err == nil {
		t.Fatal("existing directory accepted")
	}
}

func TestReservedFactEntropy(t *testing.T) {
	bytesFor := func(world, presentation uint64) []byte {
		return binary.BigEndian.AppendUint64(binary.BigEndian.AppendUint64(nil, world), presentation)
	}
	for _, tc := range []struct {
		name  string
		data  []byte
		mode  os.FileMode
		valid bool
	}{
		{"valid", bytesFor(731, 92713), 0600, true},
		{"signed", bytesFor(1<<63|731, 92713), 0600, true},
		{"legacy-salt", bytesFor(731, 92713)[:8], 0600, false},
		{"oversized", append(bytesFor(731, 92713), 0), 0600, false},
		{"zero-world", bytesFor(0, 92713), 0600, false},
		{"public-world", bytesFor(42, 92713), 0600, false},
		{"zero-presentation", bytesFor(731, 0), 0600, false},
		{"same-draw", bytesFor(731, 731), 0600, false},
		{"public-file", bytesFor(731, 92713), 0644, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "entropy")
			if err := os.WriteFile(path, tc.data, 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(path, tc.mode); err != nil {
				t.Fatal(err)
			}
			world, presentation, err := factEntropy(42, path)
			if !tc.valid {
				if err == nil {
					t.Fatal("invalid entropy accepted")
				}
				return
			}
			if err != nil || world != int64(binary.BigEndian.Uint64(tc.data[:8])) || presentation != 92713 {
				t.Fatal("reserved draws changed", err)
			}
			again, second, err := factEntropy(42, path)
			if err != nil || again != world || second != presentation {
				t.Fatal("retry changed world")
			}
			link := filepath.Join(filepath.Dir(path), "link")
			if err := os.Symlink(path, link); err != nil {
				t.Fatal(err)
			}
			if _, _, err := factEntropy(42, link); err == nil {
				t.Fatal("symlink accepted")
			}
		})
	}
	for _, path := range []string{t.TempDir(), filepath.Join(t.TempDir(), "missing")} {
		if _, _, err := factEntropy(42, path); err == nil {
			t.Fatal("non-file accepted")
		}
	}
}

func TestFactProfileDistinctFromLegacyRewrite(t *testing.T) {
	p := privatesurface.Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
	legacy, err := p.Digest()
	if err != nil {
		t.Fatal(err)
	}
	fact, err := privatesurface.FactProfileDigest(p)
	if err != nil {
		t.Fatal(err)
	}
	if fact == legacy || len(fact) != 64 {
		t.Fatal("fact and rewrite provenance conflated")
	}
	if _, err := privatesurface.FactProfileDigest(privatesurface.Profile{}); err == nil {
		t.Fatal("invalid profile accepted")
	}
}
