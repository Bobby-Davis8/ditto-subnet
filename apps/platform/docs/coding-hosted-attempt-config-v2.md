# Hosted-v2 per-attempt runtime config

`ditto.coding_hosted_attempt_config` writes the one
[`HostedPlatformRuntimeInput`](coding-hosted-platform-runtime-v2.md) document
for a single admitted hosted-v2 assignment on `ditto-coding-hosted-v2`. It
derives authority from Platform, selects transferred public authorities by the
assignment's own digests, and references existing owner-only credential files by
fixed path. It is default-off and explicit. It never starts a unit, creates or
admits an assignment, issues a grant, contacts a provider or writes to
PostgreSQL. The runtime rechecks everything under row locks at start.

```text
runuser -u ditto-coding-hosted -- /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  /opt/ditto-coding-hosted/<rev>/apps/platform/.venv/bin/python -B -I -m ditto.coding_hosted_attempt_config \
  --materialize-attempt-config --evaluation-id <uuid> --assignment-sha256 <hex> \
  --probe-receipt-sha256 <hex> --evidence-wrapping-key-sha256 <hex>
```

The default-off `coding_hosted_attempt_config` role
(`infra/ansible/playbooks/gcp-coding-hosted-attempt-config.yml`) wraps this
command. See [the wrapper](#wrapper-role).

## Inputs

Only four pins are accepted. There is no path, host value or output option.

| Pin | Source |
| --- | --- |
| `--evaluation-id` | Admin create response |
| `--assignment-sha256` | The same response. It must equal the recomputed authority digest and the stored row |
| `--probe-receipt-sha256` | Receipt file SHA-256 from the `coding-hippius-probe.yml` run summary. It selects `inbox/probe-receipt-<sha>.json` |
| `--evidence-wrapping-key-sha256` | The independently reviewed SPKI fingerprint of the evidence public key. Nothing in Platform or on the host records it |

## Host layout

`<home>` is `/var/lib/ditto-coding-hosted`, the worker account's home. Every
worker file is a regular single-link `0600` file owned by the worker, below
canonical `0700` directories. These are the runtime's own rules.

| Path | Class | Provisioned by | Checked here |
| --- | --- | --- | --- |
| `<home>/private/postgres-environment.json` | Secret | #1888 role | Metadata. Parsed in memory only to open the read-only connection |
| `<home>/private/hippius-environment.json` | Secret | Owner (not yet automated) | Metadata only; never opened |
| `<home>/private/image-storage.json` | Secret | Owner (not yet automated) | Metadata only; never opened |
| `<home>/private/provider-key` | Secret | Owner (not yet automated) | Metadata only; never opened |
| `<home>/custody/unwrap` | Helper | #1860 custody install | Protected helper, distinct from the worker |
| `<home>/inbox/execution-profile-<sha>.json` | Authority | Wrapper role | SHA equals the assignment, canonical, patch bound equals the task |
| `<home>/inbox/grading-profile-<sha>.json` | Authority | Wrapper role | SHA equals the assignment, canonical |
| `<home>/inbox/inference-policy-<sha>.json` | Authority | Wrapper role | Canonical policy, digest equals the assignment |
| `<home>/inbox/budget-profile-<sha>.json` | Authority | Wrapper role | Named by the policy's `runtime_profile_sha256`; bound, valid now and through the deadline |
| `<home>/inbox/probe-receipt-<sha>.json` | Authority | Wrapper role | Pinned SHA, canonical and ready, reader authority equals the release |
| `<home>/authority/evidence-public.pem` | Authority | Owner (not yet automated) | RSA key fingerprint equals the pin |
| `<home>/release/<registration_sha256>/{transport-manifest,payload-authority,publication-receipt}.json`, `curator-public.pem` | Authority | Owner (not yet automated) | Full network-free release verification and catalog membership for the task |
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites.json` | Host record | #1899 role, root `0444` | Closed record; supplies `router_listen`, `egress_proxy`, `egress_network`, candidate UID/GID |
| `/usr/bin/docker`, `/run/ditto-coding-hosted/docker.sock` | Host | Daemon role | Root-owned executable; worker-owned `0600` socket |
| `/opt/ditto-coding-hosted/<rev>/` | Runtime | Runtime install | The running interpreter's prefix; installed worker admission |

`executor_repository` is the single `coding-runtime.invalid/<language>/runtime`
in the isolated daemon whose `RepoDigests` contain both profiles'
`image_digest` (read-only `docker image inspect`). Zero or several matches are
refused. Seccomp and AppArmor stay empty, which keeps Docker's default seccomp.

## Authority source

Authority is read directly from Platform PostgreSQL. It uses the worker's own
environment file in one `REPEATABLE READ, READ ONLY` transaction, without
locks. PostgreSQL rejects any write or `FOR UPDATE` in that transaction. No API
exposes private assignment authority, and reaching it through the admin API
would put a stronger credential on the host. The runtime already holds this
database credential. The snapshot is read again just before the write and must
be identical.

The snapshot checks the same facts as the runtime's `inspect_launch`:

- The stored projection, recomputed digest, stored digest and pin all agree,
  including every denormalized column and the deadline.
- The assignment is admitted, unstarted and has no worker. It has between 300
  and 3600 seconds left by the database clock.
- The release row matches the registration, is shadow-only and has no
  quarantine or retirement event.
- The agent still has the artifact and screened image, a scoreable status, a
  verified image and a current screening policy.
- The private task exists, is not closed or frozen, and its selection matches
  the assignment.

## Admission ordering

A config is written only for an admitted assignment. The runtime refuses an
unadmitted one at start, and the materializer generates the worker UUID that
custody `prepare` needs. Materializing after validator admission keeps one
order. Nothing is staged ahead of the validator's signed consent.

## Refusals

Each is a fixed stage on stderr (`hosted attempt config refused: <stage>`),
without a path or value. All refusals happen before any write:

- Malformed pins.
- The wrong host, account or interpreter prefix, or an unsafe installed worker.
- Unsafe home, inbox or attempts directories, unwrap helper, Docker executable
  or socket.
- A host-prerequisites record that is missing, writable or not closed, or one
  with public, loopback, mismatched or `ditto-job-` values.
- A live `ditto-coding-hosted-worker.service` or `ditto-coding-custody@*.service`
  (anything but inactive or failed), or an existing custody socket.
- Credential files with the wrong owner or mode, a symlink, extra links, a
  shared directory or a missing file.
- An unavailable assignment, digest or authority mismatch, or an assignment that
  is not admitted, already started, expired or near expiry.
- A quarantined or retired release, an unavailable artifact, or a closed, frozen
  or mismatched task.
- `<home>/attempts/<attempt_id>` already exists, reported as "already consumed"
  when a runtime or Go consumed marker is present.
- A missing, mismatched or non-canonical execution or grading profile, or a
  patch bound that differs from the task.
- A policy digest mismatch or unbound policy, or a budget that does not bind,
  is not yet valid or expires before the deadline.
- A probe receipt with the wrong SHA or owner, that is not ready or not
  canonical, that is future-dated, or that stays fresh for less than the
  deadline plus the runtime's one-hour finalization window. Evidence
  publication inside that window still requires a receipt under 24 hours old. A
  reader authority that differs from the release is also refused.
- An evidence key fingerprint that differs from the pin.
- Release authorities that fail verification or do not contain the task index.
- A missing or ambiguous executor image.
- Authority that changed between the two snapshots.

## Output

The config is written under `<home>/attempts/<attempt_id>/`:

```text
runtime.json   config, published by no-clobber link after fsync
authority/     copies of the verified profile, policy, budget, probe and key bytes
runtime/       empty runtime_root
unwrap/        empty unwrap_work_root
```

The directory is created exclusively, so a rerun for the same attempt is
refused. A crash mid-write leaves a partial directory that must be reviewed and
is never reused. Copying authorities pins the verified bytes against later inbox
changes; credentials and release authorities are referenced in place.
Stdout is one JSON receipt: identities, deadline, authority digests, the config
path and SHA-256, the runtime root, `admitted=true`, `services_started=false`,
`shadow_only=true` and `weight_eligible=false`. The catalog index and executor
language are not printed.

## Per-attempt sequence

Every step fits inside the assignment's deadline, which is at most one hour
after creation:

1. The admin creates the assignment (#1823). The operator pins
   `evaluation_id` and `assignment_sha256`.
2. The validator control command sends the signed evaluate, and Platform admits
   the assignment.
3. The operator runs this materializer and records `worker_id`, the config path
   and `config_sha256`.
4. Custody runs `prepare <worker_id>` and `systemctl start ditto-coding-custody@<worker_id>` (#1860).
5. The connectivity role installs a profile issued within five minutes of
   start, with `coding_hosted_worker_config` set to the config path. The egress
   proxy starts (#1899).
6. `systemctl start ditto-coding-hosted-worker.service`. The runtime reloads and
   rechecks under locks, writes `platform-consumed`, and the Go helper commits
   the irreversible start.

If the deadline passes before step 6, discard custody and create a new
assignment. The old attempt directory is retained, not reused.

## Wrapper role

`coding_hosted_attempt_config` defaults off. It requires:

- the exact confirmation
  `MATERIALIZE HOSTED CODING ATTEMPT CONFIG <evaluation_id> <assignment_sha256>`;
- an installed runtime revision;
- the three pins; and
- exactly five nonsecret inputs (`probe_receipt`, `execution_profile`,
  `grading_profile`, `inference_policy`, `budget_profile`), each an absolute
  controller path plus a reviewed file SHA-256.

It refuses check mode, live worker or custody units, controller files that
differ from their SHA, an unprotected interpreter, and unsafe existing inbox or
attempts directories. It copies inputs to their digest names without replacing
existing bytes, then verifies owner, mode, link count and SHA on the host. It
runs the materializer as the worker and prints only its receipt. It takes no
credential, starts nothing and never writes outside the inbox and attempts
directories.

## Validation

```bash
cd apps/platform && uv run pytest -q -n 0 ditto/tests/api_server/test_coding_hosted_attempt_config.py
uv run pytest -q ditto/tests/test_coding_hosted_attempt_config_role.py
cd infra/ansible && uvx --from ansible-core==2.21.2 ansible-playbook --check -i localhost, tests/coding-hosted-attempt-config.yml
```

The Platform tests use real PostgreSQL and a synthetic signed release. The
happy path produces a config that `load_runtime_config` accepts, and the
runtime's locked `inspect_launch` accepts it unchanged. Credential files are
never opened, and a rerun is refused. Tests cover the refusal classes above,
and the key guards were mutation-checked. Nothing here proves a live host, a staged
credential or a canary.
