# Qualified coding-certification leases

Platform mints one shadow-only public-canary lease after it re-checks current
core qualification for the exact agent artifact. The lease is not a scoring
ticket, not a private coding assignment, and remains `weight_eligible=false`.

## Write authority

`POST /api/v1/validator/coding-certification-leases` issues a lease only when
all of these hold in one transaction:

- a permitted validator hotkey and valid sr25519 signature;
- the strict operator allowlist admits this exact
  `(agent_id, artifact_sha256, validator_hotkey)` tuple (see below; nothing is
  admitted by default);
- a complete, content-addressed screened image on the agent;
- no receipt (`certified`, `failed`, or `unsupported`) already exists for the
  identity `(agent, artifact, screened image, benchmark, coding contract)`,
  from any validator;
- a configured shadow core-qualification policy for the requested benchmark;
- the latest complete observation is `qualified` and binds the same agent,
  source artifact, screened image, benchmark, and current policy checksum;
- no `issued` or `claimed` lease already exists for that identity, after any
  overdue in-flight lease has been expired;
- fewer than 3 allowlist-admitted claims for that identity in the last 24 hours;
- the committed public `certification/v1` canary identity can be loaded.

Issuance and policy revision share a per-benchmark transaction lock, so a
lease cannot bind an observation that is already stale at commit. A later
incomplete observation does not hide an earlier complete qualified wave.

An absent policy, incomplete wave, unqualified observation, missing image,
stale artifact, terminal receipt, or exhausted attempt budget is not an error
against the normal submission. The endpoint returns `404`. An allowlist refusal
returns `403` with the fixed detail `coding certification is not allowlisted`,
before any lease, expiry, or grant row is written. Every refusal still consumes
the request nonce, and it commits instead of rolling back: an overdue lease the
issue expired (and the grant it revoked) stays expired even when the attempt
budget then refuses the request.

`POST /api/v1/validator/coding-certification-leases/{lease_id}/claim` is
exclusive to the named validator. Exact signed retries of an already-issued,
already-claimed, or already-aborted lease authenticate and return the stored
row. The request nonce is recorded on first success; a replay of that nonce
is accepted only when the mutation is idempotent. `POST .../abort` is allowed
only while the lease is still `issued`. A claimed lease cannot be aborted by
its validator, so a restart cannot create an immediate clean rerun. Expiry is
committed before the endpoint returns `404`. A `completed` lease is never
returned; claim and abort answer `404`.

## Retry semantics (contract v1)

An accepted receipt is the terminal certification result for its identity.
[`docs/coding-qualified-certification-lease-shadow.md`](../../../docs/coding-qualified-certification-lease-shadow.md)
requires that no active or terminal certification exists for the identity
before issue, and makes `unsupported` and candidate-attributable `failed`
results terminal for that artifact. Only infrastructure or control-plane
failures, which produce no receipt, may run again. So:

- the transaction that accepts a receipt moves its lease from `claimed` to
  `completed`; `completed` is terminal, never expires, and is shown as such in
  the lease audit;
- issue refuses any identity that already has a receipt row, whichever
  validator produced it, so a failed run cannot be retried into a
  certification and a certified identity is never certified twice;
- a claimed lease that ends without a receipt expires (below) and releases its
  identity, so a post-claim crash does not burn it.

A certified receipt is valid for at most 24 hours; contract v1 has no renewal
lease for the same identity. A new upload or screened-image rebuild is a new
identity.

## Deadlines, the receipt window, and recovery

Every lease has a 20-minute deadline. Harness launch, certification inference
grant offer and exchange, and relay inference (the grant's `expires_at` equals
the deadline) all end at the deadline. Only receipt submission continues, for
`CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS` (120 seconds) after the deadline,
so a certifier run bounded by the deadline can still revoke its grant and
submit. The receipt's own `issued_at` must still be no later than the deadline.

Every one of these decisions reads the database `clock_timestamp()` after the
lease row lock (and any agent or grant lock) is held, never the API host clock
and never a time captured before a lock wait.

An `issued` lease is overdue at its deadline; a `claimed` lease only once its
receipt window has passed. An overdue lease is transitioned to `expired` the
next time Platform touches it: a claim or abort retry, a receipt submission,
or the next issue request for the same identity. The transition:

- keeps `claimed_at`, so an expired row still records whether it was claimed;
- terminally revokes a `pending` or `active` certification inference grant
  bound to the lease (bearer and broker bindings cleared, accounting kept);
- releases the in-flight unique slot, so the same agent, artifact, image, and
  benchmark can be certified again without a new upload.

Nothing is deleted. A post-claim failure without a receipt (validator or scorer
crash, Platform `503`, host mismatch) therefore costs at most one deadline plus
the receipt window instead of the identity.

A receipt that arrives after the receipt window is refused with `404` and no
receipt row, and the expiry commits. An exact replay of an accepted receipt
stays idempotent at any time.

Because expiry releases the identity, re-runs are bounded: at most 3 claims
per identity in any rolling 24 hours, counted from `claimed_at`. Only claims
an allowlist revision admitted count (`claim_allowlist_revision` is set at
claim time), so claims made before the strict allowlist, or by a validator the
allowlist never named, cannot exhaust the canary's budget. An unclaimed lease
does not count.

The validator-signed abort was deliberately not extended to claimed leases.
Doing so would let the claiming validator discard an attempt it has already
seen and immediately start a clean rerun, which the deadline bound prevents.

### Validator and scorer rule

The scorer and the validator certify call stay bounded by the lease deadline
itself (`certify budget = lease.deadline - now`, scorer operation deadline =
`lease.deadline`). After certify returns or fails, the validator revokes the
grant and submits the receipt once, and must not start the submission after
`lease.deadline + 120s - 15s` by its own clock (15 seconds of allowance for
host-to-Platform clock skew and request latency). A `404` for a claimed lease
after its deadline is a no-receipt infrastructure outcome, never retried with
the same receipt.

## Operator allowlist

`coding_certification_allowlist_revisions` is an append-only, admin-only,
strict restriction. It refuses everything by default: with no revision, a
latest `enabled=false` (refuse-all) revision, or a latest revision whose
entries fail to parse or whose checksum does not bind them, Platform refuses
every certification lease issue, claim, harness launch, grant offer, grant
exchange, and receipt. No revision can reopen global access: an enabled
revision must list 1 to 16 exact `(agent_id, artifact_sha256,
validator_hotkey)` tuples, there are no wildcards, and there is no admin
certification bypass.

The check runs in the one lease authority gate shared by claim, harness launch,
grant offer, grant exchange, and receipt submission, and on lease issue before
any row is written. A refused grant offer or exchange also terminally revokes
the lease's live grant. A settlement-bound `certified` receipt is never inserted
for a tuple the current allowlist refuses.

Writing a revision, in the same transaction, aborts every `issued` or `claimed`
lease the new revision does not admit (status `aborted`, `aborted_at` set,
`claimed_at` kept, `aborted_allowlist_revision` naming the revision) and
terminally revokes every live certification grant it does not admit. The tuple
filter runs in SQL, so only refused rows are locked. A `completed`, `expired`, or
already `aborted` lease is never rewritten.

Every authorizing transaction takes the shared transaction advisory lock before
it locks any lease, agent, or grant row, and a write takes it exclusively before
it locks leases and grants, so no lease, grant, or receipt commits against a
stale revision and the two orders cannot deadlock.

Admin API (`DITTO_ADMIN_API_TOKEN`):

- `GET /api/v1/admin/coding-certification-allowlist?history_limit=` returns
  `enabled` (true only for an intact revision with exact tuples), `effective`
  (`refuse_all` or `exact_tuples`), `integrity` (`valid` or `invalid`), the
  current revision (revision `0` is the built-in refuse-all default), and
  newest-first history with actor, reason, checksum, stored `enabled`,
  `integrity`, and `effective` per revision. A corrupt revision reads as
  `integrity: "invalid"`, `effective: "refuse_all"`, and no entries; appending an
  intact revision repairs it.
- `POST /api/v1/admin/coding-certification-allowlist` appends one complete
  revision: `expected_revision`, `enabled`, `entries`, `reason` (at least 8
  characters), `actor`, and the exact confirmation
  `APPLY CODING CERTIFICATION ALLOWLIST ENABLED <entry count>` (1 to 16
  entries) or `APPLY CODING CERTIFICATION ALLOWLIST REFUSE ALL` (no entries).
  It returns `aborted_lease_count` and `revoked_inference_grant_count`.
- `GET /api/v1/admin/coding-certification-leases` pages lease rows newest
  first (`limit` 1-200, `offset`, optional `agent_id`, `validator_hotkey`,
  `status`). Rows carry identity, status (`completed` means receipted),
  issued/claimed/aborted/deadline timestamps, `receipt_window_ends_at`,
  `deadline_passed` (database clock), `claim_allowlist_revision`,
  `aborted_allowlist_revision`, and the bound grant and receipt status. They
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
`issued → claimed | aborted | expired`, `claimed → completed` (receipt
accepted), `claimed → expired` (after the receipt window, keeping
`claimed_at`), and `claimed → aborted` (only by an allowlist revision, keeping
`claimed_at`). The migration backfills claimed leases that already carry a
receipt to `completed`. Coding contract v1 stays `weight_eligible=false`.

## Activation boundary

The validator public-canary worker claims this lease, drives
`codingcertifier` through the scorer control plane, and submits the terminal
receipt against the claimed lease. Private-task admission remains a later
reviewed step. Ordinary Tool + Memory scoring, weights, and emissions do not
read this table.
