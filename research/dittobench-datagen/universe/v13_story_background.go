package universe

import (
	"crypto/sha256"
	"fmt"
)

// These record-local observations are source facts, not renderer inventions.
// Their independent stream never consumes timeline RNG or reads graded state.
type v13StoryBackgroundFact struct {
	relation string
	values   map[string]string
}

func v13StoryBackground(seed int64, pairID string) []v13StoryBackgroundFact {
	pick := func(domain string, choices ...string) string {
		d := sha256.Sum256([]byte(fmt.Sprintf("v13-background-v1:%d:%s:%s", seed, pairID, domain)))
		return choices[int(d[0])%len(choices)]
	}
	return []v13StoryBackgroundFact{
		{"In this note's local surroundings, a notebook has the stated cover and page pattern. It is not an account, project record or source of contact details.", map[string]string{"cover": pick("cover", "plain kraft paper", "dark fabric", "soft cork", "smooth recycled card"), "pages": pick("pages", "faint square ruling", "small dotted guides", "wide horizontal ruling", "unruled cream paper")}},
		{"A drinking cup rests on the stated surface and has the stated visible pattern. No ownership or connection to the thread is implied.", map[string]string{"surface": pick("surface", "a woven coaster", "a small ceramic saucer", "a folded linen square", "a round felt mat"), "pattern": pick("pattern", "a narrow band around its rim", "an uneven speckled glaze", "a simple leaf outline", "a row of shallow vertical grooves")}},
		{"The nearby window has the stated covering, and the visible light has the stated quality. This is a local observation, not a timestamp or evidence about the thread.", map[string]string{"covering": pick("covering", "a partly drawn cotton curtain", "tilted wooden blinds", "a loose translucent panel", "a rolled fabric shade"), "light": pick("light", "soft and diffuse", "pale with blurred edges", "muted by textured glass", "broken into narrow stripes")}},
		{"A small tray contains the stated stationery. Its contents are ordinary objects, not codes, quantities or records relevant to the thread.", map[string]string{"contents": pick("stationery", "a capped pen beside loose paper clips", "a blunt pencil beside blank index cards", "an eraser beside a folded paper bookmark", "a capped highlighter beside binder clips")}},
		{"The chair has the stated back and seat texture. Do not infer who used it or any action involving it.", map[string]string{"back": pick("back", "a gently curved wooden back", "a plain mesh back", "a narrow slatted back", "a padded rectangular back"), "seat": pick("seat", "a coarse woven texture", "a smooth matte surface", "a subtle stitched texture", "a lightly ribbed fabric")}},
		{"An unrelated decorative print shows the stated motif in the stated medium. It contains no text, identifiers or clues about the thread.", map[string]string{"motif": pick("motif", "overlapping leaf shapes", "abstract curved lines", "an outline of distant hills", "interlocking geometric forms"), "medium": pick("medium", "a faint pencil sketch", "a muted watercolor wash", "a simple ink outline", "a flat block-print style")}},
	}
}
