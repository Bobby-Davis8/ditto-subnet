# Default-off shadow coding worker

For the proposed Platform-hosted private v2 deployment, follow
[the private execution boundary](coding-platform-private-execution-v2.md).
The worker and remote mode described below retain their existing v1 semantics;
they do not establish confidentiality from a validator operator. Private v2
requires new request/result projections and Platform-owned execution before
this worker can be used with that release.

The shadow coding pipeline now has one complete composition path, but every
release keeps it disabled by default and every coding artifact remains
`weight_eligible=false`.

## Runtime order

The validator runs a separate `CodingShadowWorker` beside the ordinary
tool/memory scoring loop only when `VALIDATOR_CODING_SHADOW_ENABLED=true`.
For one stable worker instance it:

1. claims one Platform coding ticket from its configured exact run;
2. proves the private scorer handoff and fetches the non-executing authoring,
   screened-harness, and Luna-grant authority while the claim is transferable;
3. commits the Platform `start` boundary before invoking candidate code;
4. heartbeats the exact claim generation while work is active;
5. calls the private Go supervisor for authoring and pristine grading;
6. stores the exact signed authoring-freeze request in the Go outbox before
   Platform transmission and stores the exact verified acknowledgement after;
7. obtains the freeze-bound grading lease, runs the protected grader, and uses
   the same prepare -> publish -> acknowledge order for terminal evidence.

The Go host is constructed when `DITTOBENCH_CODING_SHADOW_ENABLED=true` or
`DITTOBENCH_CODING_CANARY_ENABLED=true`. It composes the phase-specific Docker
executor factory, artifact fetcher, scoped memory projector, durable outbox,
dormant screened-harness controller, direct-source registry, opaque workspace
and Luna routes, relay journal, attempt supervisor, publication service, and a
bounded outbox sweep loop. The public-canary handler is attached only when
`DITTOBENCH_CODING_CANARY_ENABLED=true` and
`DITTOBENCH_CODING_CERTIFICATION_ROOT` contains the pinned `certification/v1`
pack; a canary-enabled host without that pack fails closed. The sandbox scorer
image carries that pack, and Compose pins the root to it (see
[Validator certification canary](#validator-certification-canary)). The
default-off canary worker claims a lease, exchanges a lease-bound inference
grant, posts the exchanged grant into the canary control plane, and always
revokes the grant. The canary path does not claim private tickets or set
weights.
The relay-journal root has a durable directory-cardinality ceiling equal to the
host attempt bound; an unexpected entry or exhausted root fails closed instead
of allocating unbounded restart residue.

Authoring and grading do not share one prebuilt executor. Authoring receives
only the selected public environment-image digest and the validator-only
resource profile. The complete protected grader manifest is accepted only
after Platform returns the freeze-gated grading lease.

## Recovery

A started claim is never reassigned to another instance. On restart the same
instance asks the supervisor for durable state:

- `terminal_pending` reopens and republishes only the exact stored request;
- `authoring_pending` publishes the exact stored freeze, then continues from
  the acknowledged immutable patch;
- `authoring_published` reloads the stored request and acknowledgement and may
  enter pristine grading without rerunning candidate authoring;
- `released` is already complete;
- `none`, `ambiguous`, and `expired` never execute candidate code again.

Restored grading is admitted only when the phase runner independently observes
the acknowledged authoring publication in the durable outbox and the supplied
authoring evidence matches that outbox record. Process-local session loss by
itself grants no retry.
Terminal acknowledgement advances the outbox to `released`; observing that
durable state is also the only point where the supervisor may evict its
process-local session tombstone.

## Dedicated-executor client boundary

`CodingSupervisorRuntime` now has an explicit remote mode for the dedicated
executor transport. A trusted caller supplies a separately constructed TLS 1.3
client, the loaded validator keypair, private executor origin, and validator
hotkey. Every exact supervisor body is SHA-256 bound into a fresh, short-lived
`dittobench-coding-executor-control-v1` envelope; recovery additionally carries
the durable claim's agent UUID and artifact SHA instead of inventing missing
identity. The remote request sends only the signed envelope and JSON body—never
the local scorer bearer.

`CodingPublicationClient` now has the same explicit remote boundary. Its five
publication operations carry the real claim's agent UUID, artifact digest,
ticket, run, and deadline authority and sign their exact canonical JSON bytes.
The executor ingress independently requires the signed agent, artifact,
ticket, and run identities to match the publication command. Remote `open` is
also constrained to that outbox record and remote `pending` is a
non-enumerating readiness probe. Because a valid envelope cannot exist before
a claim, remote mode claims a still-unstarted ticket, performs this probe, and
only then requests leases or crosses the Platform start boundary. Local mode
retains its pre-claim loopback probe.

`CodingExecutorTLSConfig` and `create_coding_executor_http_client` validate
three distinct absolute credential files, reject symlinks, writable or
executable credentials, oversized inputs, and group/world-readable private
keys, then construct a no-proxy, no-redirect TLS 1.3-only client. This client is
now constructed by validator startup only when the separate
`VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED=true` gate has a complete private
`:9443` origin and credential profile. One independently closed client is
injected into both supervisor and publication construction; a partial profile
or one-sided remote client is impossible. The ordinary validator HTTP client is
not reused for coding control traffic; both local and remote coding clients
ignore environment proxy settings.

Compose exposes the pre-positioned CA, validator certificate, and validator key
as three read-only secrets. With the remote gate off their sources resolve to
`/dev/null`, all validator runtime settings stay empty, and no credential is
opened. The production validator-stack role can separately verify root-owned
mode-`0400` files, client purpose, chain, key match, expiration, and the exact
`spiffe://dittobench.ai/validator/<hotkey>` URI. It never copies credential
contents and never starts the worker or executor transport.

Before any ticket-bearing rollout, the mutually exclusive
`VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED=true` mode can perform
one startup probe through that same TLS client and then exit. It runs before
the validator loads its signing key or constructs Platform, Pylon, telemetry,
ordinary worker, or coding-worker clients. The transport requires the
validator client certificate, forwards `GET /v1/coding/ready` to the private
Unix control process, and accepts a fixed response only when the scorer host
has atomically constructed both supervisor and publication services. The
response explicitly records `ticket_authority_used=false`; the probe carries
no executor envelope, agent UUID, artifact digest, run ID, ticket ID, Platform
request, candidate operation, or S3 access. Configuration rejects combining
this canary with either shadow coding or remote execution.

The validator-stack role has a third, separately false
`validator_stack_coding_executor_connectivity_canary_run_enabled` control. It
accepts only the updater-validated managed release, a digest-pinned validator
image, exact source revision, canary-only Compose model, and three root-owned
mode-`0400` credential files. A successful operator run writes one unsigned,
mode-`0600` diagnostic receipt containing public release provenance and a hash
of the private executor origin. See
`infra/docs/coding-executor-connectivity-canary.md`; the receipt is never
execution, certification, scoring, or emissions authority.

## Validator certification canary

The contract-v1 certification canary runs on the ordinary production
validator stack, beside scoring, through the supported lease, grant, and
receipt path. There is no admin certification bypass. Every switch below ships
false.

- **Scorer origin.** `CodingCanaryRuntime` accepts an HTTPS origin, a loopback
  origin, or exactly the Compose service origin `http://sandbox-docker:8000`.
  Every other plaintext host or port is rejected, as are paths, userinfo,
  queries, and fragments. Plaintext is acceptable only there: the scorer shares
  sandbox-docker's network namespace on the stack's private bridge, and miner
  containers in the nested daemon cannot reach port 8000. The scorer bearer
  and per-lease broker private key use a dedicated client that ignores proxy
  environment settings.
- **Certify bound.** The certify call is single-shot. Its timeout is the time
  left before the lease deadline, capped at the scorer's 32-minute operation
  bound. The call is refused once the deadline has passed. A deadline or task
  cancellation closes the stream, so the scorer sees the disconnect and
  destroys the harness. The worker still revokes the grant in its shielded
  cleanup.
- **Scorer pack.** The `coding-certification-pack` build stage copies the
  committed `certification/v1` capsule and the locked inference policy it
  names, then:
  - checks each file against a pinned SHA-256 digest, including the manifest
    digest that Platform binds into leases;
  - fails the build on any extra file or link;
  - makes the tree read-only.

  Only the `sandbox` target carries it, root-owned, at
  `/opt/ditto/coding/certification-root`. Compose pins
  `DITTOBENCH_CODING_CERTIFICATION_ROOT` to that path. The scorer reads it only
  when `DITTOBENCH_CODING_CANARY_ENABLED=true`, and the root is not
  operator-selectable, because the runtime loader does not re-hash the
  workspace or grader files. A pack edit selects the scorer release.
- **Production rendering.** The `validator_stack` role has two switches:
  - `validator_stack_dittobench_coding_canary_enabled` renders the scorer gate
    and the runtime image repository and `sha256:` digest;
  - `validator_stack_coding_canary_enabled` renders the validator worker and
    its poll interval, and requires the scorer switch.

  Validation runs before any host mutation. Stage the scorer switch first, then
  confirm two things before turning on the validator switch: the scorer stays
  healthy, and the canary route no longer returns 404.
- **Exchange origin.** Validators accept a coding grant exchange URL only when
  it is exactly `{VALIDATOR_PLATFORM_API_URL}/api/v1/validator/...`. Platform
  renders that URL from `platform_coding_validator_api_base_url`, which
  production pins to `https://platform-api.heyditto.ai`, not the
  `https://dittobench.ai` inference origin. The default keeps other hosts'
  rendering unchanged. Production still leaves `platform_coding_shadow_enabled`
  unset.

### Remaining prerequisite: rootless coding daemon

The production `sandbox-docker` service is privileged rootful DinD. The
certification executor requires a rootless daemon with the isolated-daemon
label and checks this in its Docker preflight. That check runs during
`certify`, after Platform has already issued and claimed the lease. This change
installs no such daemon, and the canary runtime has no remote-executor mode.
Do not enable `validator_stack_coding_canary_enabled` on a host until a
separately reviewed rootless, isolated coding daemon serves that scorer.

### One-shot risks

A certification attempt is not retried. Today a lease that fails after claim
never expires and cannot be aborted. Causes include the missing daemon, the
certify deadline, and a Platform 503 during grant exchange or submission. The
failure blocks certification for that exact agent, artifact, and screened
image. The separate Platform lease-expiry PR mitigates this by expiring and
boundedly aborting claimed leases. Until it lands, treat each enabled attempt
as irreversible.

Two more constraints apply:

- **No targeting.** The worker is offered every agent the validator scores. With
  both switches on and no targeting allowlist, the validator attempts
  certification for every qualified agent, not one chosen canary.
- **Short validity.** An issued certificate is valid for one hour. Any hosted
  assignment that depends on it must be created inside that window.

## Activation checklist

Activation is deliberately a coordinated operator action, not a code default.
All of the following must be configured together:

- Platform: an explicit Ansible
  `platform_coding_shadow_enabled: true`, which renders
  `DITTO_CODING_SHADOW_ENABLED=true`, the relay's separate coding gate, the
  canonical locked policy file, and exact HTTPS exchange, proxy, and
  revocation URLs. The exchange URL is on the validator-facing API origin
  (`platform_coding_validator_api_base_url`). The reconciler and k=3 ticket-set
  admin gates remain separate false-by-default controls;
- scorer: `DITTOBENCH_CODING_SHADOW_ENABLED=true`, an euid-owned mode-0700
  private root, the canonical locked policy file, the source-bound port/base
  URL, and the reviewed runtime-image repository with every selected immutable
  environment/grader digest preloaded on the dedicated daemon;
- sandbox: a dedicated rootless daemon with the isolated-daemon label, the
  capability-only egress network, and an explicit allowlisting proxy;
- validator: `VALIDATOR_CODING_SHADOW_ENABLED=true`, one exact
  `VALIDATOR_CODING_SHADOW_RUN_ID`, one stable instance ID, and the existing
  private scorer control token. Dedicated execution additionally requires the
  separate remote gate, private IPv4 `https://...:9443` origin, and the three
  verified mTLS secret paths.

The default-zero dedicated GCP executor cohort documented in
`infra/docs/coding-executor-hosts.md` is the physical isolation foundation for
the future k=3 canary. It creates neither a daemon nor a worker. A later
reviewed role must install and prove the rootless isolated daemon before any of
the activation settings above are eligible for operator use.

That dedicated rootless-daemon role is itself false by default and has no
scorer or worker consumer. It creates an empty daemon identity and an empty
socket-client group only; enabling a future coding worker remains a separate
reviewed operator action.

Setting only a subset fails closed: no ticket is claimed, or startup rejects
the incomplete runtime. The committed Compose values keep both worker gates
false. This PR does not deploy the Platform transport configuration, change a
benchmark version, combine coding with ordinary scoring, alter emissions, or
enable a leaderboard weight.

## Validation

```bash
uv run pytest -q ditto/tests/validator/test_coding_worker.py \
  ditto/tests/validator/test_coding_attempt.py \
  ditto/tests/validator/test_coding_supervisor.py \
  ditto/tests/validator/test_coding_publication.py \
  ditto/tests/validator/test_coding_canary.py \
  ditto/tests/validator/test_coding_runtime_wiring.py \
  ditto/tests/test_coding_certification_scorer_image.py

(cd infra/ansible && uvx --from ansible-core==2.21.2 ansible-playbook --check \
  -i localhost, tests/validator-stack-coding-canary.yml)

cd services/dittobench-api
go test -race ./internal/codinghost ./internal/codingpublication \
  ./internal/codingsupervisor ./internal/codingphase \
  ./internal/codingattempt ./internal/codingexecutor ./internal/codingcanary
go vet ./...
```
