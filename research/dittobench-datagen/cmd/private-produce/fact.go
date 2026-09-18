package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
)

func runFactProducer(seed int64, size, out string, profile privatesurface.Profile, limit float64, entropyFile string) error {
	if _, ok := gen.ProfileForVersion(size, 13); !ok {
		return errors.New("fact producer: invalid run size")
	}
	profileSHA, err := privatesurface.FactProfileDigest(profile)
	if err != nil {
		return err
	}
	world, presentation, err := factEntropy(seed, entropyFile)
	if err != nil {
		return err
	}
	if err := os.Mkdir(out, 0700); err != nil {
		return errors.New("fact producer: output must be a new directory")
	}
	call := 0
	renderer, err := privatesurface.NewFactRenderer(profile, os.Getenv("OPENROUTER_API_KEY"), limit, func(s privatesurface.BudgetSnapshot) error { return writeBudgetCheckpoint(out, s) }, func(a privatesurface.FactRenderAudit) error {
		raw, err := json.Marshal(a)
		if err != nil {
			return err
		}
		call++
		return writePrivate(out, fmt.Sprintf("call-%04d.json", call), raw)
	})
	if err != nil {
		return err
	}
	identity, _ := json.Marshal(map[string]any{"revision": gen.V13FactGenerationRevision, "seed": seed, "world_seed": world, "presentation_seed": presentation, "run_size": size, "profile_sha256": profileSHA, "budget_usd": limit, "qualified": false})
	if err := writePrivate(out, "generation.json", identity); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 2*time.Hour)
	defer cancel()
	artifact, err := gen.GenerateV13FactDataset(ctx, seed, world, presentation, size, renderer)
	if err != nil {
		return errors.New("fact producer: generation failed; inspect private call receipts and budget checkpoint")
	}
	pin, raw, err := artifact.SHA256Hex()
	if err != nil {
		return err
	}
	if _, err := gen.DecodePrivateArtifact(raw, pin, seed, size); err != nil {
		return errors.New("fact producer: reconstruction failed")
	}
	if err := writePrivate(out, "dataset.json", raw); err != nil {
		return err
	}
	// Rendering and replay are not semantic qualification. In particular this
	// candidate receipt must never be accepted as a rollout validation receipt.
	receipt, _ := json.Marshal(map[string]any{"revision": "v13-fact-candidate-v2", "generation_revision": gen.V13FactGenerationRevision, "dataset_sha256": pin, "profile_sha256": profileSHA, "run_size": size, "calls": call, "qualified": false, "semantic_coverage": "not-qualified", "remaining_surface_qualification_required": true})
	if err := writePrivate(out, "fact-candidate.json", receipt); err != nil {
		return err
	}
	// This receipt attests checked generation and exact native replay, NOT
	// rollout qualification. Platform accepts it only from its approved local
	// producer process and separately verifies the reserved entropy binding.
	manifestHash := sha256.Sum256(identity)
	validation, err := json.Marshal(map[string]any{
		"schema": "private-fact-generation-validation-v1", "accepted": true,
		"qualified": false, "replay_verified": true,
		"generation_revision": gen.V13FactGenerationRevision,
		"generation_sha256":   fmt.Sprintf("%x", manifestHash),
		"dataset_sha256":      pin, "transform_profile_sha256": profileSHA,
		"run_size": size, "render_event_count": len(artifact.FactGeneration.Events),
	})
	if err != nil {
		return err
	}
	if err := writePrivate(out, "validation.json", validation); err != nil {
		return err
	}
	fmt.Println("fact candidate produced privately; NOT qualified, pinned, leased or activated")
	return nil
}

// Reserved entropy is private producer input, never a public lease identifier.
// It pins both independent draws across a worker retry; it is not the legacy
// eight-byte rewriting salt. Reject invalid files before allocating an output
// directory or creating a provider client.
func factEntropy(leaseSeed int64, path string) (int64, int64, error) {
	var raw [16]byte
	if path == "" {
		if _, err := rand.Read(raw[:]); err != nil {
			return 0, 0, errors.New("fact producer: entropy unavailable")
		}
	} else {
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() != 16 {
			return 0, 0, errors.New("fact producer: invalid entropy file")
		}
		file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if err != nil {
			return 0, 0, errors.New("fact producer: entropy unavailable")
		}
		defer file.Close()
		actual, err := file.Stat()
		if err != nil || !os.SameFile(info, actual) || actual.Mode().Perm()&0077 != 0 || actual.Size() != 16 {
			return 0, 0, errors.New("fact producer: entropy file changed")
		}
		data, err := io.ReadAll(io.LimitReader(file, 17))
		if err != nil || len(data) != 16 {
			return 0, 0, errors.New("fact producer: invalid entropy length")
		}
		copy(raw[:], data)
	}
	world := int64(binary.BigEndian.Uint64(raw[:8]))
	presentation := int64(binary.BigEndian.Uint64(raw[8:]))
	if world == 0 || world == leaseSeed || presentation == 0 || world == presentation {
		return 0, 0, errors.New("fact producer: invalid independent entropy")
	}
	return world, presentation, nil
}
