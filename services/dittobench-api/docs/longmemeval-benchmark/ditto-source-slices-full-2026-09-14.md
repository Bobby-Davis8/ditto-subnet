# Full-500 source-slices experiment

## Preregistered protocol

User authorized proceeding after the [60-case pilot](ditto-source-slices-pilot-2026-09-14.md).
Run fresh control and treatment on **all 500 isolated LongMemEval-S questions**,
not a resume of the pilot or a comparison against a differently compiled old run.

- Same clean source and binary, `openai/gpt-5.6-luna` medium reader,
  `google/gemini-3.1-flash-lite` built-in judge, question-date prompt clock.
- Graph ON (`-require-graph -subject-graph`), same prepared private fixture and
  retrieval weights. `-concurrency 8` each, launches close in time.
- Control `-seed-context summary`; treatment `-seed-context source-slices-v1`.
  Slice algorithm unchanged from backend PR2736, no production tool changes.
- Full500 launcher rejects sampling, case filters, wrong models/config/input
  hashes, dirty/unproven binaries and reused outputs. Source derives from
  pilot `1cd1cf15245ea39123ea421e9bb2b7bf51d204c1`; only QA artifact IDs and
  launcher change. UUID suffix prevents same-second remote upload collisions.
- One attempt/arm. No incorrect-answer retries or regrading. If transport fails,
  preserve original report/journal and permit at most one failure-only resume
  after documenting exact failed IDs, same source and checkpoint compatibility.
- Prepared snapshot/hydration and zero graph failure gates must pass; compare
  actual ordered seed IDs and baseline JSON fields for all500, disclose drift.
- Report all500 plus previously-piloted60 and remaining440 separately. Remaining
  440 is not clean held-out: the retrieval ranker has 474/500 question-ID/text
  training overlap; the full corpus has also been evaluated previously.
- Report category counts, paired wins/losses, Wilson intervals, source-context
  bytes, cumulative prompt tokens, tool calls and final provider receipt costs.
  One stochastic run/judge is not a general efficacy guarantee.
- Cost: include all captured reader/judge calls from every attempt, separate
  OpenRouter charges and provider-reported BYOK estimates (not invoices), total
  and mean/median/p95 per question. Original seed/dream and query-embedding
  cost remains unknown unless independently captured; do not treat it as zero.
  New seed/dream calls are zero because preparation is reused.

No reseeding, dreaming, fixture writes/cleanup, merge, deployment or activation.
Both code and protocol published before inference. Result artifacts retained
under the new task workspace, not written into earlier pilot workspaces.

## Reproduction

Backend: `scripts/benchmarks/lme_slices_full.py --spec <new-arm-spec.json>`
verifies without inference; append `--execute` only for an approved paid run.
Specs include exact500 ordered IDs, source/binary/input hashes and distinct
task-local output paths. Existing fixed campaign guards remain unchanged.

Subnet: `analyze_slices_full.py` checks both native reports, full launch spec
and prior pilot evidence. It exports whitelisted public-fixture reports and
replays them to verify unchanged paired metrics. Costs use the existing
`audit_resumed_openrouter_costs.py` against closed journals and GET-generation
receipts, excluding private receipt IDs from the committed summary.

Results and exact execution identities will be appended after completion.

## Failure-only resume decision (before retry)

Control first invocation saved500 rows but only496 judged: three reader TLS
`bad record MAC` failures (`gpt4_e414231e`, `2ebe6c92`, `gpt4_68e94287`), and
one judge TLS failure (`gpt4_7ca326fa`). Native graph/hydration and unchanged
prepared snapshot gates passed. Progress counters/logs did not expose these
case failures until the final report; they are not500 valid scores.

Apply the preregistered single failure-only native resume. The wrapper pins the
exact original report/checkpoint/journal hashes, source and binary; copies all
496 judged rows unchanged, including wrong answers; and claims retry4 once.
It refuses any different failure, retry complement, altered result or dirty
source. The native harness re-prepares all500 contexts but queries only four.
For the one judge-failed case, native resume regenerates its answer as well as
retrying judging: it is an unjudged case, not a known-wrong answer retry. Its
original answer is retained, the final outcome is not cherry-picked, and all
original/new costs are counted. This is a protocol limitation relative to a
judge-only retry, explicitly preserved in the record. No third attempt allowed.
