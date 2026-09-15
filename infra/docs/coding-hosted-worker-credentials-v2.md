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
refuses any Ansible variable whose name matches a `DITTO_CODING_WORKER_*` or
`DITTO_CODING_HIPPIUS_*` controller input (so `-e DITTO_CODING_WORKER_PROVIDER_KEY=…`
is refused, not silently ignored).

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

Run from the `infra/ansible` directory, or export
`ANSIBLE_CONFIG=infra/ansible/ansible.cfg`, so the repo `ansible.cfg` applies its
`roles_path` (otherwise the role is reported "not found") and its
`callback_result_format=yaml`. The commands below assume the `infra/ansible`
working directory.

```bash
cd infra/ansible
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

GCP_OSLOGIN_USER=… ansible-playbook -i inventory/gcp.yml \
  playbooks/gcp-coding-hosted-worker-credentials.yml \
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

- The enabled gate is frozen once in `main.yml` with `is sameas true` (never
  `| bool`, which prints the coerced value in a deprecation warning even under
  `no_log`), in a task with no loop variable in scope, and the enabled branch
  runs through a dynamic `include_tasks`. ansible re-templates a variable on
  every read, so a lazily templated extra var such as
  `-e '{"..._enabled": "{{ item is defined }}"}'` evaluates false at the gate and
  true inside a write loop; a block-level `when` is pushed down to every child
  task. Freezing the gate once and using a dynamic include closes that flip.
- The preset refusal is enforced twice: once in `main.yml` before the gate is
  frozen, and again as the first task inside the dynamically included
  `materialize.yml` / `remove.yml`. `--start-at-task` can begin at the `main.yml`
  gate freeze and skip the static refusal, but it cannot jump into a dynamic
  include, so the in-include refusal always runs. It refuses any
  `coding_hosted_worker_credentials_*` variable other than the three inputs and
  the gate (a preset registered result, a preset capture such as
  `coding_hosted_worker_credentials_documents`, or an undocumented input), any
  `DITTO_CODING_WORKER_*` / `DITTO_CODING_HIPPIUS_*` Ansible variable, and it
  asserts the raw `coding_hosted_worker_credentials_enabled is sameas true`, so a
  preset gate fact alone (with the flag false or unset) cannot open the run. The
  refusal pattern excludes the cleanup role's prefix, and the cleanup refusal
  excludes this one's, so neither matches the other's variables.
- The run must target exactly the one dedicated host. `hosts: role_coding_hosted`
  makes an `inventory_hostname`/group check tautological, and a host reports its
  own name, so the role asserts both `ansible_play_batch == ['ditto-coding-hosted-v2']`
  and `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` — values computed
  from the run's targeting that `-e` cannot override. Requiring the whole host
  set, not only the batch, means `serial: 1` on the group cannot pass either, so
  a role-labelled VM that merely reports this hostname is never reached without
  `--limit ditto-coding-hosted-v2`.
- The write module carries the credentials to the host, so before any
  secret-carrying task the role refuses unless SSH pipelining is on and
  `ANSIBLE_KEEP_REMOTE_FILES` is unset, so the module is never left in the host's
  remote temp directory. The materialize playbook sets `ansible_pipelining: true`
  in its play vars: the SSH connection plugin reads pipelining from that var (a
  `[ssh_connection] pipelining` ini setting in `ansible.cfg` turns pipelining on
  but does **not** populate the var), so setting it there both turns pipelining
  on and lets the guard confirm it. The guard requires `ansible_pipelining` true
  and `ansible_ssh_pipelining` undefined or true, because on ansible-core 2.21.2
  `ansible_ssh_pipelining` wins over `ansible_pipelining`, so
  `-e ansible_ssh_pipelining=false` (or `-e ansible_pipelining=false`) turns
  pipelining off and is refused. This is advisory against accidental
  misconfiguration.
- Every accepted input is captured once with `set_fact` and validated as a
  frozen literal. A `set_fact` result is a plain value, not a trusted template,
  so it never re-templates in a later scope. The confirmation and source
  revision are matched exactly, and a value that a nested template rendered into
  a literal `{{`, `{%` or `{#` is refused.
- The playbook gathers no facts. Host identity and the worker and custodian
  accounts come from a registered `setup` and `getent`, because an
  `ansible_facts` extra var replaces gathered facts.
- The worker and every custody instance must be stopped. The live-unit guard is
  an allow-list read directly from the registered `systemctl` result in an
  inlined assert (no overridable include variable), by both the pre-write and
  post-write checks: only `inactive` or `failed` pass, so `active`,
  `activating`, `deactivating`, `reloading`, `refreshing` (systemd 256 and
  later), `maintenance`, a future state or an unparseable line all refuse.
  An empty listing means no such unit is loaded and is allowed. The role stops
  nothing.
- No process may be running as the worker UID. The listed units are not enough:
  the rootless dockerd user manager (`user@<uid>.service`) and an escaped
  candidate share that UID and could read the files or swap a directory. The
  role reads `/proc` for the worker UID and refuses if any process is present.
- The private directory must be the worker's own `0700` directory, not a
  symlink, below a real worker home that is not group- or world-writable. This
  Ansible stat is an early, clear refusal; the authoritative symlink-safe check
  is on the module's own file descriptors (below).

## How it writes and verifies

The write is done by a role-local Ansible module,
`library/coding_hosted_worker_credentials_write.py`, that ansible transfers to
the target and runs as root. The three documents are one module parameter
declared `no_log: true` in its `argument_spec`, so ansible replaces it with
`VALUE_SPECIFIED_IN_NO_LOG_PARAMETER` in the target's module-invocation journal
line and in `-vvv` controller output; nothing is passed in argv or on stdin, and
the module task is `no_log` too. The module never resolves the destination as a
string: it opens every component of the private directory path from `/` with
`O_NOFOLLOW|O_DIRECTORY` — so a directory the worker account could swap for a
symlink between the Ansible stat and the write cannot redirect it — verifies the
home and private directory's owner and mode on the open descriptors, writes each
file to a tracked temporary with `O_CREAT|O_EXCL|O_NOFOLLOW`, `fchown`s and
`fchmod`s it, `fsync`s, renames every temporary into place only after all three
are written, and re-verifies each result (regular, single link, owner, mode
`0600`, size, and SHA-256 of the bytes, compared in process) on its own
descriptor. Every temporary it creates is unlinked on any failure path, so a
failed write leaves no partial secret temporary behind. It refuses a destination
that is a symlink, directory, hard link, another account's file or not mode
`0600`, and never follows or re-permissions such a path. The module prints only
non-secret metadata — filenames, mode, link count, size, owner — never a value
or a digest. After it, the role re-lists the units and refuses loudly if any
unit went live during materialization.

Cleanup uses the sibling module `coding_hosted_worker_credentials_unlink.py`,
which opens the path the same way and removes the three fixed names, and any
leftover `.<name>.*.tmp` a partial write may have left, with `unlinkat` on the
pinned directory descriptor, reporting which it removed.

Partial write: the module writes all three temporaries before renaming any, so a
rename failing part-way is rare, but if it happens the module fails and reports
exactly which fixed names were `replaced` and which were left `unchanged_or_unknown`;
it does not silently leave a mixed set unreported. Reconcile by hand from that
report before re-running. A re-run overwrites any already-replaced file with the
same content, so re-running after reconciling is safe.

Residual race: a unit that starts after the final recheck and before any later
service start is outside this role, which starts nothing. Start services only
after re-confirming the files and the stopped state through the reviewed
procedure.

## Nothing is logged, and one residual

No task prints an input value or a digest. The set_fact captures that hold
credentials are `no_log`; the write module carries them as a `no_log`
`argument_spec` parameter and its task is `no_log`; asserts are `quiet` with
static failure messages that never interpolate an input; the modules and report
show only filenames, states and the non-secret source revision. The rehearsal
(below) proves that stand-in secrets and their MD5, SHA-1, SHA-256 and SHA-512
digests, in raw, JSON-, YAML- and repr-escaped forms, never reach ansible
output, including under `-vvv` and `--diff` with the repo's yaml callback.

Inputs are captured once with `set_fact ... | default('', true)` and validated
as frozen literals, and the enabled gate is frozen the same way. On ansible-core
2.21.2 this absorbs an **undefined-class** template error — for example
`{{ {}[lookup('env','X')] }}`, whose subscript raises an Undefined — into `''` or
`false`, so such an input is refused with no leak (the rehearsal exercises this).

One residual remains and cannot be closed on 2.21.2: if an input is a template
that raises a **lookup or filter plugin error whose message embeds a value**, for
example `{{ lookup('file', lookup('env','DITTO_CODING_WORKER_PROVIDER_KEY')) }}`,
ansible prints that value in its own `[ERROR]` finalization banner before the
role can inspect it, and neither `no_log`, `ignore_errors`, a rescue nor
`default(..., true)` suppresses that banner (all were tested). The role still
writes nothing, because the failure aborts the run before any write. This is why
the three inputs must be passed literally on the command line, from the
operator's own shell, and secrets must be exported only in that same shell: a
hostile templated input can then only surface a value already present to that
same operator.

A second, interactive-only residual: `ansible-playbook --step` prompts before
each task and lets the operator answer `n` (skip) to a guard then `c` (continue)
past the rest. `--step` is an operator-run interactive choice, not something an
attacker supplies through `-e`, and there is no reliable `--step` signal to
detect on ansible-core 2.21.2; run these playbooks without `--step`.

## Cleanup and rotation

`coding_hosted_worker_credentials_cleanup` (playbook
`gcp-coding-hosted-worker-credentials-cleanup.yml`, confirmation
`REMOVE NATIVE CODING WORKER CREDENTIALS`) removes only the three fixed files
through the `coding_hosted_worker_credentials_unlink` module. It refuses unless
the units are stopped and no process runs as the worker UID, opens every path
component with `O_NOFOLLOW`, and removes each name with `unlinkat` — never
`file: state=absent` on a path that could be a directory or symlink. The module
returns `removed`, `already_absent`, `refused` and `not_attempted` lists on every
path, so a partial removal (for example a later name that is a symlink after
earlier names were unlinked) names exactly what was removed, what refused and
why, and what was not attempted. It also removes and reports any leftover
`.<name>.*.tmp` a partial write may have left. It reads no secret, so export
nothing for it. It keeps the directories.

Asymmetry with the write module: the write module requires the private directory
to be mode `0700` and each destination mode `0600`, but cleanup only requires the
directory to be owned by the worker and not group/other writable, so a directory
left at a wrong mode can still be cleaned up; the observed directory mode is
reported (`private_dir_mode`) for the operator to reconcile. Removing a file does
not depend on the file's own mode.

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
