# Source-slices pilot: preregistered protocol

This is a bounded experimental follow-up to the
[graph-score diagnosis](ditto-graph-score-drop-diagnosis-2026-09-14.md), not a
new full-500 result. No experiment result was inspected before this protocol.

## Hypothesis and stale-PR provenance

Selected memories can contain the answer in raw source while their model-facing
summary omits it. Test whether presenting bounded verbatim source alongside the
same summaries improves answers. This tests **presentation**, not graph benefit.

Inspiration: backend [PR859](https://github.com/ditto-assistant/backend/pull/859),
`309817dcdafebbae10927661ed8df843a5704595`, subject-span annotations and excerpt
windows. That PR is data-layer-only and does not connect the chat/MCP readers.
Its builder overwrites multiple windows for one role, and its documented
unannotated-role fallback is not implemented in the inspected function. We do
not cherry-pick its old migration or claim to implement its subject annotations.

The prototype uses **query-anchored read-time slices**, only in the backend
LongMemEval QA seed serializer. Production, subject indexing, graph retrieval,
tool definitions, `AlreadyFoundPairIDs`, full fetch, and DB contents are unchanged.
PR880 compact serialization/top-3 is deliberately excluded from this ablation.

## Frozen arms

- Condition: isolated LongMemEval-S, previously seeded/dreamed private fixture,
  strict graph preflight and one-hop-v1 retrieval ON in both arms.
- Same clean backend commit and binary in both arms. Baseline code descends from
  merged fix `fa8b02f750bec4afeee15cd17e552fac72052601`.
- Reader `openai/gpt-5.6-luna`, reasoning `medium`; built-in judge
  `google/gemini-3.1-flash-lite`. Provider-resolved versions checked in receipts.
- `-sample 60`: native deterministic category round-robin; first 10 questions
  in input order per each of six categories. No accuracy/gold-based selection.
  Launch specs store all 60 IDs before inference; reports must match them.
- Control: `-seed-context summary` (stock `LongTermJSON`).
- Treatment: `-seed-context source-slices-v1` (retain summary and all seed IDs,
  timestamps, parents, plus bounded source for each user/assistant role).
- Roles <=1,200 Unicode runes retained verbatim. Longer roles: up to three
  windows around exact query-word matches, 200-rune radius, <=32-rune boundary
  expansion, prioritized by distinct query terms; overlap merged, source order
  retained. Fixed stop words; terms 3..64 runes. If no anchors, first/last 200
  runes. Explicit partial flags/ellipses; no gold-dependent processing.
  Unsummarized memories retain the full source already shown by the control;
  the experiment adds evidence and never clips existing baseline fields.
- `-prompt-clock question-date`, `-concurrency 8` per arm. No retry of an
  incorrect answer. One fresh attempt per arm; any transport failure is retained
  and reported before a separately documented bounded failure-only retry.

Backend `scripts/benchmarks/lme_slices_pilot.py` reuses the fixed campaign's
source/binary VCS checks, pinned input/config hashes, literal env parsing,
loopback private DB override and exclusive output guards. It permits only the
exact pilot extension. The existing full-500 launcher remains unchanged.
Dry run first; paid inference only with explicit `--execute`.

## Evidence and acceptance gates

Archive report, launch spec/receipt, logs, checkpoint, append-only provider usage
journal, source SHA, binary SHA, dataset/manifest hashes and selected case IDs.
Both arms must complete/judge all 60 with native hydration, no graph SQL failure,
unchanged prepared-fixture snapshots and identical model/prompt/tool/weight
identities. The condition hashes must differ to prevent cross-arm resume.

Both arms now record actual seed JSON, hash, byte count and truncation status
(512 KiB trace cap). Compare ordered seed IDs **and** baseline memory fields in
the source-slices payload. Report any retrieval drift, not just a pooled score.
Native recency still uses SQL server time, not the question-date prompt clock;
close launches plus observed parity are required, not assumed determinism.

Report paired wins/losses, category counts, aggregate accuracy, session recall,
actual context bytes, cumulative prompt tokens, tool use and receipt costs.
The balanced 60-case score is not the 500-case score or evidence of statistical
equivalence. One sample/run/judge is exploratory; do not declare a general win
from a small difference. The retrieval ranker has 474/500 question-ID/text
training overlap: this is not clean held-out generalization or a mem0 comparison.

## Cost boundaries

Reconcile captured reader and judge generation IDs against final OpenRouter
GET-generation receipts, including charged failed calls. Report reader BYOK
upstream estimate separately from OpenRouter charges; neither is silently an
invoice. Publish total, mean/median/p95 per question and missing-receipt counts.

No new seeding/dreaming is performed; this experiment reuses the prepared
fixture. Incremental seed/dream calls are zero. **Original preparation cost is
unknown**, not zero. Query-embedding cost is unmeasured unless separately
captured; disclose that the total covers captured reader/judge calls only.

No experimental merge, production activation/deployment or fixture mutation.
Results will be appended with exact run IDs and hashes after both arms finish.
