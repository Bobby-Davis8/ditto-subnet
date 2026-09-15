# Native Coding worker credentials

The default-off `coding_hosted_worker_credentials` role materializes the three
worker-owned credential files the native hosted-v2 runtime reads, on the
dedicated host `ditto-coding-hosted-v2`, without ever holding Secret Manager or
IAM access itself. The operator (Peyton) retrieves each secret and exports it in
the shell that runs the playbook; the role validates ownership, mode and
single-link state, writes atomically, verifies, and starts nothing. It is
disabled by default and, while disabled, only reports that it is dormant.

This role does not write `postgres-environment.json` (owned by the
`coding_hosted_postgres_environment` role, PR #1888) or any release or authority
file under `authority/`, `release/` or the probe receipt (handled by the
attempt-config materializer, PR #1909). It writes exactly three files.

## What it writes

All three live in `/var/lib/ditto-coding-hosted/private/`, which must already
exist as `ditto-coding-hosted:ditto-coding-hosted 0700`. The role validates that
directory and the worker home above it; it never creates, repairs or loosens
them. Each file is written `ditto-coding-hosted:ditto-coding-hosted 0600`,
regular, single link, and is re-verified after the write.

| Destination file | Format the runtime loader accepts |
| --- | --- |
| `hippius-environment.json` | JSON object with only the `DITTO_CODING_HIPPIUS_*` keys the loader's `HIPPIUS_KEYS` allows |
| `image-storage.json` | JSON object `{endpoint_url, bucket, access_key, secret_key, region}`; HTTPS is required |
| `provider-key` | Raw nonempty printable-ASCII key, no whitespace, no trailing newline |

### Fixed, non-secret settings (role constants, never inputs)

- Hippius: `DITTO_CODING_HIPPIUS_ENDPOINT_URL=https://s3.hippius.com`,
  `DITTO_CODING_HIPPIUS_REGION=decentralized`,
  `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET=ditto-subnet-coding-private-input`,
  `DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET=ditto-subnet-coding-sealed-evidence`,
  `DITTO_CODING_HIPPIUS_TIMEOUT_SECONDS=20`. These are byte-identical to the
  `coding-hippius-probe` workflow, so the storage authorities the runtime
  derives from this file equal the probe receipt's, and PR #1909 accepts them.
- Image storage: `endpoint_url=https://storage.googleapis.com`,
  `bucket=ditto-platform-agents-prod`, `region=auto`.

### Controller-side secret inputs (environment only)

Every secret enters only through a `DITTO_CODING_WORKER_*` environment variable
the operator exports on the controller. None is an Ansible variable, so
inventory, Git, vars files and `-e` never carry one; the role refuses any
`coding_hosted_worker_credentials_*` variable other than the three inputs, and
any Ansible variable named like a credential.

| Controller environment variable | Written into | Runtime key |
| --- | --- | --- |
| `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY` |
| `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY` |
| `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY` (access key **id only**) |
| `DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY` |
| `DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY` |
| `DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY` | `image-storage.json` | `access_key` |
| `DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY` | `image-storage.json` | `secret_key` |
| `DITTO_CODING_WORKER_PROVIDER_KEY` | `provider-key` | the raw key |

The Hippius **curator secret key** is never accepted and never written: the
runtime needs only the curator access key id to verify publications, and the
curator secret key stays with the offline curator workflow. The role refuses to
run if `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY` or
`DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY` is set in the
environment.

Each input must be nonempty printable ASCII with no whitespace and no trailing
newline. The three access keys must start with `hip_` and be at most 512 bytes;
the two Hippius secret keys and the image secret key at most 4096 bytes; the
image access key at most 256 bytes; the provider key at most 4096 bytes. The
eight values must be distinct.

## Operator procedure (Peyton runs this)

The operator retrieves each secret himself and exports it without echoing it, in
the same shell that runs the playbook, then unsets everything afterwards. Secret
Manager and IAM are entirely outside the role; see
`coding-worker-credential-staging-peyton.md` for the source secret names and the
least-privilege recommendations.

```bash
# Export each value from its Secret Manager secret without printing it. Prefer a
# dedicated image reader HMAC and a dedicated capped OpenRouter key (below).
export DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY="$(gcloud secrets versions access latest --secret=platform-coding-catalog-access-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY="$(gcloud secrets versions access latest --secret=platform-coding-catalog-secret-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY="$(gcloud secrets versions access latest --secret=platform-coding-catalog-curator-access-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY="$(gcloud secrets versions access latest --secret=platform-coding-hippius-evidence-access-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY="$(gcloud secrets versions access latest --secret=platform-coding-hippius-evidence-secret-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY="$(gcloud secrets versions access latest --secret=coding-hosted-image-reader-hmac-access --project=ditto-app-dev)"
export DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY="$(gcloud secrets versions access latest --secret=coding-hosted-image-reader-hmac-secret --project=ditto-app-dev)"
export DITTO_CODING_WORKER_PROVIDER_KEY="$(gcloud secrets versions access latest --secret=coding-hosted-openrouter-key --project=ditto-app-dev)"

GCP_OSLOGIN_USER=… ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-coding-hosted-worker-credentials.yml \
  --limit ditto-coding-hosted-v2 \
  -e '{"coding_hosted_worker_credentials_enabled": true, "coding_hosted_worker_credentials_confirmation": "MATERIALIZE NATIVE CODING WORKER CREDENTIALS", "coding_hosted_worker_credentials_source_revision": "<40-char lowercase-hex reviewed revision>"}'

unset DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY \
  DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY \
  DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY \
  DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY \
  DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY \
  DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY \
  DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY \
  DITTO_CODING_WORKER_PROVIDER_KEY
```

Pass the three `-e` inputs literally on the command line, never from a vars file
or inventory. Enabling requires all three: the exact confirmation
`MATERIALIZE NATIVE CODING WORKER CREDENTIALS`, a 40-character lowercase-hex
source revision with no trailing newline, and the exact target host.

## What the role refuses

- The enabled gate is decided once, in a single top-level task with no loop
  variable in scope, and the enabled branch runs through a dynamic
  `include_tasks`. ansible re-templates a variable on every read, so a lazily
  templated extra var such as `-e '{"..._enabled": "{{ item is defined }}"}'`
  evaluates false at the gate and true inside a write loop; a block-level `when`
  is pushed down to every child task and re-evaluated with the loop variable in
  scope. Reading the gate once and using a dynamic include closes that flip and
  also `--start-at-task`, which cannot jump into a dynamically included file and
  so cannot skip the guards.
- Every accepted input is captured once with `set_fact` and validated as a
  frozen literal. A `set_fact` result is a plain value, not a trusted template,
  so it never re-templates in a later scope. The confirmation and source
  revision are matched exactly, and a value that a nested template rendered into
  a literal `{{`, `{%` or `{#` is refused.
- Any `coding_hosted_worker_credentials_*` variable other than the three
  documented inputs, including a preset registered result, a preset capture such
  as `coding_hosted_worker_credentials_documents`, or an undocumented input, is
  refused before anything is inspected. The refusal pattern excludes the cleanup
  role's prefix, and the cleanup refusal excludes this one's, so neither matches
  the other's variables.
- The playbook gathers no facts. Host identity and the worker and custodian
  accounts come from a registered `setup` and `getent`, because an
  `ansible_facts` extra var replaces gathered facts.
- The worker and every custody instance must be stopped. The live-unit guard is
  an allow-list: only `inactive` or `failed` pass, so `active`, `activating`,
  `deactivating`, `reloading`, `refreshing` (systemd 256 and later),
  `maintenance`, a future state or an unparseable line all refuse.
  An empty listing means no such unit is loaded and is allowed. The role stops
  nothing.
- The private directory must be the worker's own `0700` directory, not a
  symlink, below a real worker home that is not group- or world-writable.
- Each destination, inspected without following links, must be absent or a
  regular, single-link, mode-`0600` file owned by the worker. A symlink,
  directory, hard link, another account's file or a wrong-mode file is refused;
  the role never follows, replaces or re-permissions such a path.

## How it writes and verifies

Each file is written atomically with `no_log`, without a diff, without following
a final-component link and without a backup. After the write the role re-stats
each file (regular, single link, owner, group, mode `0600`, size bounds) and
checks its SHA-256 against the intended content, all under `no_log`, so no value
or digest is ever printed. It then re-lists the units and refuses loudly if any
unit went live during materialization.

Residual race: a unit that starts after this final recheck and before any later
service start is outside this role, which starts nothing. Start services only
after re-confirming the files and the stopped state through the reviewed
procedure.

## Nothing is logged, and one residual

No task prints an input value or a digest. Secret-bearing tasks are `no_log`;
asserts are `quiet` with static failure messages that never interpolate a value;
the report is a fixed string. The rehearsal (below) proves that stand-in secrets
and their digests never reach ansible output, including under `-v` and `--diff`.

One residual comes from ansible-core itself: if an input is a template that
raises while referencing an environment lookup, for example
`{{ {}[lookup('env','DITTO_CODING_WORKER_PROVIDER_KEY')] }}`, ansible prints the
offending expression in its own `[ERROR]` finalization error before the role can
inspect it, and `no_log` does not suppress that banner. The role still writes
nothing, because the failure aborts the run before any write. This is why the
three inputs must be passed literally on the command line, from the operator's
own shell, and secrets must be exported only in that same shell: a hostile
templated input can then only surface a value already present to that operator.

## Cleanup and rotation

`coding_hosted_worker_credentials_cleanup` (playbook
`gcp-coding-hosted-worker-credentials-cleanup.yml`, confirmation
`REMOVE NATIVE CODING WORKER CREDENTIALS`) removes only the three fixed files. It
refuses unless the units are stopped, inspects each path without following
links, and removes each with `unlink` semantics — never `file: state=absent` on
a path that could be a directory or symlink. It reports any partial removal and
verifies absence. It reads no secret, so export nothing for it. It keeps the
directories.

### Removal is not revocation

Removing these files does not revoke any credential. Each stays valid at its
issuer until the operator revokes or rotates it there: the Hippius reader and
evidence tokens (`hippius-token-lifecycle`), the GCS HMAC key for image storage,
and the OpenRouter provider key. A suspected exposure is a leak of those
credentials; rotate them at the issuer, not by running cleanup. Runtime roots
written by earlier attempts may also hold copies and are retained evidence this
role neither finds nor removes.

## Non-goals

- No Secret Manager access and no IAM change. The role reads no secret from a
  cloud API; the operator supplies every value through the controller
  environment.
- No service is installed, started, restarted or enabled. There is no `systemd`,
  `service`, handler or command that starts anything; only read-only state
  queries.
- No release, authority, probe-receipt or PostgreSQL file is touched.

## Least-privilege recommendations (recommendations only)

These are recommendations for the secrets the operator exports; the role is
indifferent to which account issued a value.

- **Image storage:** prefer a **dedicated image reader** GCS HMAC key bound to a
  service account with only `storage.objects.get` on screened-image objects in
  `ditto-platform-agents-prod`, stored in its own secret, rather than reusing the
  Platform's `platform-storage-hmac-secret`, which grants the whole agents
  bucket.
- **Provider key:** prefer a **dedicated capped OpenRouter key** with a hard
  credit limit sized to the canary policy, in its own secret, rather than reusing
  `validator-openrouter-key`. The provider-evidence review must then cover that
  key's account posture.
- **Hippius identities** are reused as-is so the host's reader and evidence
  authorities equal the probe receipt's; dedicated host sub-tokens are a later
  rotation (`hippius-token-lifecycle`).

## Tests

Structural pytest tests parse the role and assert its shape, the `no_log`
coverage, the include gate and the CI wiring. A platform-side test renders the
three documents and feeds them to the real runtime parsers. With
`DITTO_ANSIBLE_REHEARSAL=1` the rehearsal runs the enabled tasks through
ansible-core 2.21.2 against a temporary tree with stand-in credentials: it writes
the files when units are stopped; refuses forged facts, preset results and
captures, undocumented and credential-shaped variables, a templated (`lookup`)
input, wrong unit states, a trailing-newline revision, missing, duplicate and
wrong-prefix credentials, wrong-owner/mode/symlink/hardlink destinations, the
`--start-at-task` and enabled-flip bypasses, and a unit that goes live after the
write; and proves the stand-ins and their digests never appear in output. The
infra CI Ansible job runs it and both disabled fixtures.
