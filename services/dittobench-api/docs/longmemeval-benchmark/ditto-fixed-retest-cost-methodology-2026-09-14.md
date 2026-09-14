# Fixed graph retest: complete cost accounting, with missing costs explicit

Status: accounting preparation only; no new full QA result or monetary total
is asserted here. The rerun reuses the prepared memories. That can make
**incremental seeding/dreaming inference zero**, but it does not make the
historical cost of constructing those memories zero.

| Phase | Evidence / accounting status before launch |
| --- | --- |
| New reader calls | Passive receipts planned; provider-measured charge pending |
| New judge calls, including retries | Passive receipts planned; provider-measured charge pending |
| New query/tool embeddings | Vertex; charge unknown unless separately measured, estimates labeled separately |
| Original fixture embeddings and seeding | Historical charge unknown; pre-embedded data reuse is not evidence of free creation |
| Original extraction/dreaming/refinement/labeling | Historical charge unknown; no receipts found in inspected logs or stored token-usage column |
| This rerun's reused seed/dream work | Zero incremental inference only if no extraction/dream/label work is replayed; verify the final phase journal |
| Graph refresh/local database operations | No OpenRouter inference charge if detached labeling is suppressed; local infrastructure excluded from provider-cost subtotal |
| Full lifecycle and preparation amortization | Unestablished while historical preparation and other categories are missing |

## Cost categories and units

Report each question's reader charges and judge charges separately, plus query
and tool embeddings where evidence is available. Also report original fixture
embedding/ingestion, extraction, refinement, cluster labeling and preparation
retries. Database copy/graph SQL and local compute have no OpenRouter charge;
that is not a claim that infrastructure or engineering time is free.

OpenRouter's [usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
returns `usage.cost` in responses, including the final streaming usage event.
Its [generation metadata endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)
allows historical lookup by saved generation ID. These are provider-reported
account charges, not the backend's price-table estimates. OpenRouter says its
[credit/API prices are USD-denominated](https://openrouter.ai/docs/faq).
Keep `usage.cost` and generation `total_cost` as alternative evidence for the
same request, not additive charges. Do not add upstream cost again. BYOK/vendor
invoices, taxes and credit-purchase fees require separate evidence; a provider
usage charge is not a complete cash-invoice reconciliation.

Use exact decimal arithmetic. Missing, malformed, inaccessible or delayed
cost records remain unknown. An explicit provider zero is distinct from no
receipt. Deduplicate cumulative stream events by generation ID; do not add
each update as another request. Retain retries and failed-case receipts, not
only the successful final checkpoint. Never use an account-wide balance delta
to attribute spend to this campaign on a shared key.
The passive journal does not prove a response's finality: a canceled stream can
leave a partial cumulative usage cost. Response-only amounts therefore remain
**provisional observed subtotals**, not finalized charges. After calls drain,
reconcile every response-only generation through GET metadata before certifying
saved-generation charge coverage. A final charge may differ from the earlier
observation; keep both without adding them or requiring equality.

## What historical evidence establishes

The [preparation inventory](results/2026-09-14-preparation-cost-inventory.json)
hashes **72 retained preparation logs, manifests and audit receipts** from the
original full-memory workspace. None contains a recognizable OpenRouter
generation ID or a JSON cost/token-usage field. These receipts establish
preparation state/provenance, not billed amounts. This bounded inventory does
not prove no other historical receipt exists. A separate read-only check of
the new private copy at `2026-09-14T12:34:14.654951Z` found **zero nonnull
`memory_pairs.token_usage` values among 124,366 rows**. The copy's pre-refresh
semantic snapshot still matched the original `01c6a7070cba4e1455894c6da07132dcbba1b5cbc37c2c58c9f26db4adb3d055`;
this column provides no historical preparation charge receipts either.

The historical reader reports retain generation IDs under
`per_case.data.provider_responses` (`ID`, `Model`, `Provider`). The old judge's
`chatCompletion` returns only response content, discarding the provider ID and
usage; therefore reader recovery alone cannot establish judge/preparation cost.
The backend's previous Luna price fallback was explicitly invalid and is not
reused as a dollar estimate. Persisted pair/subject/vector counts are not billable
token counts or call counts: retries, batching, caching and reused embeddings
prevent that conversion without further evidence.

On September 14, the approved legacy ADC and standard ADC paths both failed
direct refresh with `RefreshError`; gcloud CLI authentication also required
reauthentication. No provider key was retrieved and no historical generation
lookup succeeded. No credentials are included in artifacts. This is an
authentication blocker for recovery, not evidence of zero charges.
No full fixed-graph reader/judge run has launched at this accounting handoff.

## New capture and aggregation

The planned passive backend journal is
`<checkpoint>.provider-usage.jsonl`, with case/attempt/stage attribution and
generation ID, model/provider, tokens, explicit usage presence and
`cost_status` / `cost_credits`. Reader and judge prompts, routing, temperatures,
tools and answer selection must remain unchanged. The final per-case report
also retains receipts, while the journal preserves calls for failed cases.
Calls that fail before a generation ID arrives still have unknown charge
coverage; a saved-generation-complete report must not claim every attempt was
captured. Query/tool embeddings use Vertex and are a separate category, not
automatically covered by OpenRouter generation receipts.

The offline [cost auditor](../../integrations/longmemeval/audit_openrouter_costs.py)
reads the final report and optional journal, deduplicates cumulative events,
rejects cross-case/model attribution conflicts and emits per-case/stage sums.
Early blank-model events may acquire their model from later events for the same
case/stage/attempt/generation; conflicting nonempty models and unresolved final
identities fail validation. Duplicate saved receipts fail before journal merging.
It separately reports captured judge subtotal and the question denominator.
Mean, median and nearest-rank p95 of captured generation cost per question are
emitted only when every referenced generation has a reconciled GET charge; they
still exclude uncaptured attempts and lifecycle costs. Both missing-cost and
response-cost-only journal generations remain eligible for metadata recovery.
Only already reconciled generation receipts can skip GET lookup. Provisional
amounts stay separate when a lookup fails and never unlock priced-complete stats.
`--fetch` performs only GET requests to the fixed OpenRouter generation
metadata endpoint using an environment key; redirects are rejected and
authentication/rate-limit failures stop further lookup. It never sends prompts
or performs inference. Output files are exclusive-create and mode 0600.

```sh
python3 services/dittobench-api/integrations/longmemeval/audit_openrouter_costs.py \
  --report /private/final-report.json \
  --journal /private/checkpoint.provider-usage.jsonl \
  --output /new/private/cost-audit.json
# Add --fetch only after authorized local-key access works; never paste the key.
```

## Per-question and overall presentation

For an independently verified historical preparation total `P`, an explicitly
equal-amortization view is `P / 500` per question. This is an allocation policy,
not measured question-specific ingestion spend. Add each question's measured
reader/judge/embedding charges separately. The lifecycle total is
`P + sum(reader + judge + embedding + attributed retries)`; the incremental
rerun total excludes already-paid preparation but includes any newly executed
refresh/embedding/labeling work. Do not double-count failed attempts already
represented by generation receipts or count the same preparation afresh in
every matched arm's combined campaign total.

Until `P` and other missing categories are established, report the captured
subtotal, its coverage and unknown components. Full lifecycle cost and its
per-question amortization remain **null/unknown**, not the captured subtotal
under a broader label. No inference should be repeated merely to manufacture
historical cost receipts.

Reproduction: run `inventory_preparation_cost_evidence.py --root /private/.tmp/lme
--output /new/private/inventory.json`. Run `python3 -m unittest -q
test_audit_openrouter_costs` from the integration directory. This work changes
research accounting only, not production scoring or billing records.
