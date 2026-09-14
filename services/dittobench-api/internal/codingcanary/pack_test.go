package codingcanary

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestLoadPublicPackMatchesThePinnedLeaseIdentity(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	if pack.CanaryManifestSHA256 != "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903" {
		t.Fatalf("canary manifest sha=%s", pack.CanaryManifestSHA256)
	}
	if pack.InferencePolicySHA256 != lockedInferencePolicySHA256 || pack.TaskID != publicCanaryTaskID {
		t.Fatalf("pack identity=%+v", pack)
	}
	if pack.CPUQuotaMillis != 2000 || pack.MemoryLimitBytes != 1024*1024*1024 || pack.PidsLimit != 256 {
		t.Fatalf("resource envelope=%+v", pack)
	}
}

func TestPublicPackExecutionPlansValidate(t *testing.T) {
	pack, err := LoadPublicPack(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC)
	plans, err := pack.executionPlans(
		now, now.Add(20*time.Minute), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		"sha256:"+repeatHex("2"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if plans.runner.CaseID != publicCanaryTaskID || plans.grader.CaseID != publicCanaryTaskID {
		t.Fatalf("case ids runner=%s grader=%s", plans.runner.CaseID, plans.grader.CaseID)
	}
	if len(plans.visible) == 0 || len(plans.graderBundle) == 0 {
		t.Fatal("execution plans omitted capsule bytes")
	}
}

// The scorer image carries only the files pinned in the Dockerfile's
// coding-certification-pack stage. Loading exactly that set must yield the same
// lease identity and the same execution bytes as the full repository.
func TestLoadPublicPackFromTheScorerImageFileSet(t *testing.T) {
	repo := repoRoot(t)
	files := scorerImageCertificationFiles(t)
	root := t.TempDir()
	for path, digest := range files {
		body, err := os.ReadFile(filepath.Join(repo, filepath.FromSlash(path)))
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(body)
		if hex.EncodeToString(sum[:]) != digest {
			t.Fatalf("scorer image pin for %s is stale", path)
		}
		target := filepath.Join(root, filepath.FromSlash(path))
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, body, 0o444); err != nil {
			t.Fatal(err)
		}
	}
	imagePack, err := LoadPublicPack(root)
	if err != nil {
		t.Fatalf("image-shaped certification root does not load: %v", err)
	}
	repoPack, err := LoadPublicPack(repo)
	if err != nil {
		t.Fatal(err)
	}
	if imagePack.CanaryManifestSHA256 != "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903" ||
		imagePack.CanaryManifestSHA256 != repoPack.CanaryManifestSHA256 ||
		imagePack.RunnerPlanSHA256 != repoPack.RunnerPlanSHA256 ||
		imagePack.GraderPlanSHA256 != repoPack.GraderPlanSHA256 ||
		imagePack.ResourceProfileSHA256 != repoPack.ResourceProfileSHA256 ||
		imagePack.InferencePolicySHA256 != repoPack.InferencePolicySHA256 {
		t.Fatalf("image pack identity=%+v repo pack identity=%+v", imagePack, repoPack)
	}
	now := time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC)
	lease := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	digest := "sha256:" + repeatHex("2")
	imagePlans, err := imagePack.executionPlans(now, now.Add(20*time.Minute), lease, digest)
	if err != nil {
		t.Fatal(err)
	}
	repoPlans, err := repoPack.executionPlans(now, now.Add(20*time.Minute), lease, digest)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(imagePlans.visible, repoPlans.visible) ||
		!bytes.Equal(imagePlans.graderBundle, repoPlans.graderBundle) ||
		!reflect.DeepEqual(imagePlans.runner, repoPlans.runner) ||
		!reflect.DeepEqual(imagePlans.grader, repoPlans.grader) {
		t.Fatal("image-shaped certification root changes the execution plans")
	}
}

func scorerImageCertificationFiles(t *testing.T) map[string]string {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(repoRoot(t), "services", "dittobench-api", "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	start := strings.Index(text, " AS coding-certification-pack\n")
	if start < 0 {
		t.Fatal("scorer Dockerfile has no coding-certification-pack stage")
	}
	stage := text[start:]
	if end := strings.Index(stage, "\nFROM "); end >= 0 {
		stage = stage[:end]
	}
	pins := regexp.MustCompile(`"([0-9a-f]{64})  ([^"\s]+)"`).FindAllStringSubmatch(stage, -1)
	files := make(map[string]string, len(pins))
	for _, pin := range pins {
		if _, duplicate := files[pin[2]]; duplicate {
			t.Fatalf("duplicate scorer image pin for %s", pin[2])
		}
		files[pin[2]] = pin[1]
	}
	if len(files) == 0 {
		t.Fatal("scorer Dockerfile pins no certification files")
	}
	return files
}

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func repeatHex(value string) string {
	out := make([]byte, 0, 64)
	for len(out) < 64 {
		out = append(out, value...)
	}
	return string(out[:64])
}
