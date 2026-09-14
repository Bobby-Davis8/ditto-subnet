# Qualified coding-certification leases

Platform mints one shadow-only public-canary lease after it re-checks current
core qualification for the exact agent artifact. The lease is not a scoring
ticket, not a private coding assignment, and remains `weight_eligible=false`.

## Write authority

`POST /api/v1/validator/coding-certification-leases` issues a lease only when
all of these hold in one transaction:

- a permitted validator hotkey and valid sr25519 signature;
- a complete, content-addressed screened image on the agent;
- a configured shadow core-qualification policy for the requested benchmark;
- the latest complete observation is `qualified` and binds the same agent,
  source artifact, screened image, benchmark, and current policy checksum;
- when the operator allowlist is enabled, it names this exact
  `(agent_id, artifact_sha256, validator_hotkey)` tuple (see below);
- no `issued` or `claimed` lease already exists for that identity, after any
  in-flight lease past its deadline has been expired;
- fewer than 3 leases for that identity were claimed in the last 24 hours;
- the committed public `certification/v1` canary identity can be loaded.

Issuance and policy revision share a per-benchmark transaction lock, so a
lease cannot bind an observation that is already stale at commit. A later
incomplete observation does not hide an earlier complete qualified wave.

An absent policy, incomplete wave, unqualified observation, missing image,
stale artifact, or exhausted attempt budget is not an error against the normal
submission. The endpoint returns `404`. An allowlist refusal returns `403` with
the fixed detail `coding certification is not allowlisted`, before any lease,
expiry, or grant row is written; the request nonce is still consumed.

`POST /api/v1/validator/coding-certification-leases/{lease_id}/claim` is
exclusive to the named validator. Exact signed retries of an already-issued,
already-claimed, or already-aborted lease authenticate and return the stored
row. The request nonce is recorded on first success; a replay of that nonce
is accepted only when the mutation is idempotent. `POST .../abort` is allowed
only while the lease is still `issued`. A claimed lease cannot be aborted
before its deadline, so a restart cannot create an immediate clean rerun.
Expiry is committed before the endpoint returns `404`.

## Deadline expiry and recovery

Every lease has a 20-minute deadline. Once it passes, an `issued` or `claimed`
lease is transitioned to `expired` the next time Platform touches it: a claim
or abort retry, a receipt submission, or the next issue request for the same
identity. The transition:

- keeps `claimed_at`, so an expired row still records whether it was claimed;
- terminally revokes a `pending` or `active` certification inference grant
  bound to the lease (bearer and broker bindings cleared, accounting kept);
- releases the in-flight unique slot, so the same agent, artifact, image, and
  benchmark can be certified again without a new upload.

Nothing is deleted. A post-claim failure (validator or scorer crash, Platform
`503`, host mismatch) therefore costs at most one deadline instead of the
identity. Harness launch, grant offer, and grant exchange already refuse a
lease past its deadline; the relay also refuses the grant because its
`expires_at` equals the lease deadline.

A receipt submitted after its lease deadline is refused with `404` and no
receipt row, even if the lease was claimed. An exact replay of a receipt that
was accepted before the deadline stays idempotent.

Because expiry now releases the identity, re-runs are bounded: at most 3 leases
per identity may be claimed in any rolling 24 hours, counted from
`claimed_at`. An unclaimed or aborted lease does not count.

The validator-signed abort was deliberately not extended to claimed leases.
Doing so would let the claiming validator discard an attempt it has already
seen and immediately start a clean rerun, which the deadline bound prevents.

## Operator allowlist

`coding_certification_allowlist_revisions` is an append-only, admin-only
restriction. It is disabled by default: with no revision, or with a disabled
latest revision, issue and grant eligibility are exactly the rules above.

When the latest revision is enabled, Platform admits only its exact
`(agent_id, artifact_sha256, validator_hotkey)` tuples (at most 16). There are
no wildcards. An enabled revision with no entries refuses every lease and
grant. The check runs on:

- lease issue, before any row is written;
- certification inference grant offer (`.../inference-grant`);
- certification inference grant exchange (`.../inference-exchange`), which
  mints the bearer. A refused offer or exchange also terminally revokes the
  lease's live grant.

Writing an enabled revision also revokes every live certification grant whose
lease tuple is not listed, in the same transaction, so tightening the
allowlist cuts off Platform-paid inference immediately rather than at the
grant deadline.

Every enforcing read takes a shared transaction advisory lock and every write
takes it exclusively, so no lease or grant commits against a stale revision.
A stored revision whose entries fail to parse or whose checksum does not bind
them refuses everything.

Admin API (`DITTO_ADMIN_API_TOKEN`):

- `GET /api/v1/admin/coding-certification-allowlist?history_limit=` returns
  `enabled`, the current revision (revision `0` is the built-in disabled
  default), and newest-first history with actor, reason, and checksum.
- `POST /api/v1/admin/coding-certification-allowlist` appends one complete
  revision: `expected_revision`, `enabled`, `entries`, `reason` (at least 8
  characters), `actor`, and the exact confirmation
  `APPLY CODING CERTIFICATION ALLOWLIST ENABLED <entry count>` or
  `APPLY CODING CERTIFICATION ALLOWLIST DISABLED`. A disabled revision must
  carry no entries. It returns `revoked_inference_grant_count`.
- `GET /api/v1/admin/coding-certification-leases` pages lease rows newest
  first (`limit` 1-200, `offset`, optional `agent_id`, `validator_hotkey`,
  `status`). Rows carry identity, status, issued/claimed/aborted/deadline
  timestamps, `deadline_passed`, and the bound grant and receipt status. They
  never include grant ids, bearer digests, broker keys, or image locators. The
  read never transitions a row, so an overdue lease shows its stored status
  with `deadline_passed=true`.

Backroom exposes these as `get_coding_certification_allowlist`,
`set_coding_certification_allowlist`, and `list_coding_certification_leases`.

For the one-agent canary, write the enabled revision naming only the canary
tuple before enabling Platform coding transport or the validator canary
worker, and confirm it with the read tool.

## Storage

`coding_certification_leases` stores the frozen authority JSON plus the
screened-image identity (digest, config id, reference, upload id). Rows are
never rewritten to look current except for the one-way status transitions
`issued → claimed`, `issued → aborted`, `issued → expired`, and
`claimed → expired` (after the deadline, keeping `claimed_at`). Coding
contract v1 stays `weight_eligible=false`.

## Activation boundary

The validator public-canary worker claims this lease, drives
`codingcertifier` through the scorer control plane, and submits the terminal
receipt against the claimed lease. Private-task admission remains a later
reviewed step. Ordinary Tool + Memory scoring, weights, and emissions do not
read this table.
