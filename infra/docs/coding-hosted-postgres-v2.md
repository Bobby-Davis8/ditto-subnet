# Native-v2 private PostgreSQL path

This layer prepares the private network and guest admission path from the
Platform-owned Coding host to the existing Platform PostgreSQL VM. Both network
and guest gates default off. It creates no database user, password, grant, worker,
assignment or private evaluation, and performs no protected apply or convergence.
The [host foundation](coding-hosted-host-v2.md) remains independently default-off.

## Network boundary

`enable_coding_hosted_postgres` requires `enable_coding_hosted_host`. The root
passes the actual `module.pg_vm.internal_ip`, Platform VPC self-link and existing
PostgreSQL target tag into the native-host module. The optional `postgres_peer`
input defaults null and rejects disabled hosts, another project's/network's
self-link and addresses outside the current Platform `10.30.0.0/24` IPv4 subnet.

Two reviewed VPC peerings exchange private subnet routes. Custom routes, public
subnet routes and IPv6 exchange are disabled. Peering is not a port-specific
route or a firewall grant: private subnet routes are automatically exchanged by
[Google Cloud's peering model](https://docs.cloud.google.com/vpc/docs/vpc-peering).
The only new access permissions are separate firewall rules:

- Coding egress: its existing VM tag to the actual PostgreSQL `/32`, TCP 5432,
  at priority 800 before the existing priority-900 private/metadata deny.
- PostgreSQL ingress: the actual Coding VM's single `/32` to the existing
  PostgreSQL target tag, TCP 5432. No whole-subnet or cross-VPC source-tag grant.

Peering creation depends on both rules. The original private/metadata and other
egress denials remain intact; no reverse inbound exception is added on Coding.
Review effective inherited policies and both networks' rules before apply.
VPC rules cannot distinguish trusted worker and candidate processes sharing one
VM: separately qualify the default-deny host/cgroup/router boundary before
granting this route. The route does not approve a private execution environment.

The nonsecret `coding_hosted_postgres_access` output records the exact client IP,
client `/32`, PostgreSQL private IP and port. It explicitly reports
`database_login_ready=false`, `shadow_only=true` and `weight_eligible=false`.
An IP change after host replacement requires renewed review and guest convergence;
do not reuse an old host/IP approval or destroy retained host state automatically.

## Guest firewall and PostgreSQL admission

After separately reviewing the protected Terraform plan/apply and actual host
boundary, supply `coding_hosted_postgres_enabled: true` and the exact
`coding_hosted_postgres_client_ip` from that output to the existing
`gcp-platform-pg.yml` convergence. Do not guess an address or place passwords in
inventory, Git, receipts or command output. Existing protected password handling
and OS Login/IAP access remain unchanged.

The guard runs before the `base` role can change UFW, and again for direct
`postgres` role callers. It permits only `ditto-pg-platform` in its proper
inventory group and a canonical single usable native-subnet address. Reserved,
public, other-subnet, CIDR, IPv6, wrong-type and newline/injection inputs fail.

With the guest flag off, both existing allowlists render unchanged. When enabled:

- UFW gains only TCP 5432 from the exact host `/32`.
- HBA gains only `ditto_platform_prod`, the existing `ditto` Platform application
  principal, that same `/32`, and `scram-sha-256` authentication. Existing Platform
  subnet rules remain unchanged; the new rule grants neither dev-database nor
  all-user admission from Coding.

This uses the existing trusted Platform application principal and does not change
its SQL privileges. Its credential must never reach validators, miners or
candidate containers. Approval of that principal's use by the trusted native
service, credential custody, and authenticated/encrypted database-channel policy
remain operator responsibilities. Neither a private VPC route nor SCRAM alone
is a claim of certificate-verified TLS. No public PostgreSQL endpoint, PgBouncer,
new database, key distribution or password rotation is added by this layer.

HBA-only changes now notify a PostgreSQL reload, not a restart. Tuning changes
that require restart retain their existing restart handler. A full database
convergence can still change other approved settings; review the complete diff
and any resulting maintenance needs rather than treating it as HBA-only blindly.

## Qualification and rollback

After convergence, verify the actual routes, source IP, cloud and guest firewall
rules, effective HBA rows, credential/TLS behavior and a bounded native-service
database query. Prove rejection from other hosts and candidate containers and
against unintended ports, databases and users. Then continue approved runtime
installation, unchanged-private-suite qualification, custody/signing, Hippius
release publication and the private shadow canary. None of those live proofs is
provided by mocked plan or configuration-rendering tests.

Rollback is explicit and must preserve private state:

1. Stop/drain the exact native invocation and revoke its host network authority;
   reconcile any unfinished attempt/evidence before another run.
2. Review removal of only the PostgreSQL peerings and exceptions by setting the
   separate network flag false. Keep the host flag true if retained host data
   must remain; do not decommission the host as a network rollback shortcut.
3. Remove the exact native UFW rule through an authorized targeted operation.
   UFW convergence is additive: omitting a rule does not delete an existing one.
   Never reset the complete guest firewall.
4. Set the guest flag false and converge/reload the managed HBA configuration.
   A reload does not terminate existing connections. Inspect exact Coding-host
   backend identities and obtain approval before terminating any retained
   sessions; do not kill all PostgreSQL clients.

The module tests use a mocked provider and `command=plan` only. The Ansible
fixture forces a local connection, has no convergence roles or privilege
escalation, and checks native rendering plus 13 invalid addresses. CI syntax
checks the real playbook and runs that fixture with `--check`. These tests do not
read production database credentials or apply network/host changes.

## Native environment files

The hosted worker and the custody service each read their own PostgreSQL
environment copy. The private reader requires a file owned by the reading
account, mode `0600`, single-link, inside a `0700` directory it owns, so one
shared root-owned file cannot serve both. The default-off
`coding_hosted_postgres_environment` role writes:

- `/var/lib/ditto-coding-custody/private/postgres-environment.json`, owned by
  `ditto-coding-custody`;
- `/var/lib/ditto-coding-hosted/private/postgres-environment.json`, owned by
  `ditto-coding-hosted`.

Each is the JSON list of `POSTGRES_*=value` entries that the runtime parser
accepts:
- **Fixed fields:** user `ditto`, database `ditto_platform_prod`, port `5432`,
  pool 1–4 and a 30-second command timeout.
- **Host:** the exact Platform private IP.
- **Password:** read only from `DITTO_CODING_PG_PASSWORD` in the controller's
  local environment. It is not an Ansible variable, so inventory, Git and `-e`
  cannot carry it, and a `coding_hosted_postgres_environment_password` variable
  is refused.

This is a separate protected convergence. It uses the same pattern as the first
provisioning of `gcp-platform-pg.yml`: an operator who already holds
`platform-db-password` exports it, runs
`playbooks/gcp-coding-hosted-postgres-environment.yml` with the exact
confirmation `MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT`, passing booleans
as JSON extra vars (`-e '{"coding_hosted_postgres_environment_enabled": true, …}'`),
and unsets it. No workflow identity gets Secret Manager access.

The role behaves as follows:
- The `enabled` gate is decided exactly once. `tasks/main.yml` captures the
  flag a single time, with no loop item in scope, into a `no_log` fact guarded
  by `default(false, true)`, and hands the work to a dynamic `include_tasks`
  gated on that fact. A lazily templated flag such as `{{ item is defined }}` is
  therefore false and cannot flip to true inside a later loop; `--start-at-task`
  cannot jump into the not-yet-included file to skip the guards and reach the
  write, and starting at the include itself fails on the missing captured fact;
  and a flag whose template renders undefined while reading a secret resolves to
  false without surfacing the value.
- The gate opens only for a real boolean true (`is sameas true`); a string such
  as `"true"` leaves it closed. The `bool` filter is avoided: on ansible-core
  2.21 it prints any non-boolean string it coerces, such as a flag templated to
  the password, in a deprecation warning that `no_log` does not suppress.
- Before it inspects anything, it refuses a password variable and any other
  variable named `coding_hosted_postgres_environment_*` except the `enabled`,
  `confirmation`, `source_revision` and `host` inputs and the captured gate,
  whether set by extra vars, inventory or vars files. Presetting the captured
  gate only enables materialization, which every guard still decides. A preset
  loop `item` is refused too. Both checks list variable names with `varnames`
  and never render a value, because `is defined` would render a raising
  template and print its error. Extra vars outrank registered results and
  set_facts. Without this check, a preset result such as
  `coding_hosted_postgres_environment_units` would disable the live-unit guard,
  and a preset `coding_hosted_postgres_environment_document` would replace both
  the file and the digest used to verify it. The separate removal role's
  `_cleanup_` variables are excluded so its presence never produces a misleading
  refusal here.
- It gathers no facts. Host identity and the worker and custodian accounts come
  from registered `setup` and `getent` probes, because an `ansible_facts` extra
  var replaces gathered facts but not a registered result. It also requires
  `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` and
  `ansible_play_batch == ['ditto-coding-hosted-v2']`, so the play targets
  exactly the reviewed inventory host: the hostname, architecture and
  distribution come from the target's own `setup`, which a labelled rogue VM
  controls, but the inventory names in the play and batch do not, and extra
  vars cannot override either, so a run without `--limit` cannot route the
  password to such a host. Both are pinned because with `serial: 1` the batch
  alone is the reviewed host while a rogue host is still in the play.
- It refuses check mode up front with a constant message, since it only writes
  files and cannot verify a write in `--check`. The dormant fixture stays the
  check path: `main.yml` never includes the write file when the gate is closed.
- Before the password is read it requires pipelining to be genuinely on and
  `keep_remote_files` off. Without pipelining ansible-core writes the module,
  including its arguments, to a temp file under the ssh user's `~/.ansible/tmp`
  on the target, and with `keep_remote_files` on it is left there, so the
  password could persist even on a dropped connection. The playbook sets
  `ansible_pipelining: true` in its play vars: that is the ssh connection
  plugin's own input variable, so it genuinely enables pipelining, whereas the
  repo `ansible.cfg`'s `[ssh_connection] pipelining = True` sets the plugin
  option without populating the variable. On ansible-core 2.21.2 the plugin
  reads `ansible_pipelining` and then `ansible_ssh_pipelining`, the later one
  wins, and both outrank the `ANSIBLE_PIPELINING` environment and ini settings,
  so an extra var `ansible_ssh_pipelining=false` disables pipelining while
  `ansible_pipelining` still reads true. The role therefore requires both to be
  a real boolean true (`is sameas true`, never the `bool` filter, which prints a
  coerced value) and `keep_remote_files`, which has no variable form and
  disables pipelining on its own, to be false. Run the playbook as written,
  without exporting `ANSIBLE_PIPELINING` or overriding either variable.
- Every variable it reads is a documented input, a prefixed name the preset
  check refuses, the refused loop `item`, or a magic variable extra vars cannot
  override. `inventory_hostname` and `group_names` are host variables that an
  extra var replaces, so group membership is proved from `groups` and
  `ansible_play_hosts_all` instead: every host in the play must belong to
  `role_coding_hosted`. Every message is fixed text.
- Every operator input is captured once, with no loop item in scope and with a
  `default(..., true)` guard, so a lazily templated value cannot render one
  thing for a guard and another inside a loop, and a template that errors while
  reading the password becomes an empty string that fails validation instead of
  surfacing the value in a fatal error message. Every later task, and the
  document, use only the captured values.
- It requires a source revision of exactly 40 lowercase hex characters and a
  host address that trimming leaves unchanged. A `$` anchor alone would accept a
  trailing newline.
- It validates a bounded, single-line password without logging it.
- It refuses unless every listed worker or custody unit is `inactive` or
  `failed`. This is an allow-list, so `active`, `activating`, `deactivating`,
  `reloading`, `refreshing` (systemd 256 and later), `maintenance`, a future
  state or an unparseable line all refuse. An empty listing means no such unit
  is loaded and is allowed. The role stops nothing.
- It writes each copy with a role-local module, never `copy`/`file`, so no
  symlink can be followed. A path-based write run as root would follow a
  `private` (or copy) symlink the reader account swaps in, even mid-run: it
  would chown and chmod the link target to the account and write the password
  inside it, and a `follow=false` stat of the final path would still pass. The
  module instead opens every component from `/` with `O_NOFOLLOW`, verifies the
  reader-owned home and `private` directories on the descriptors (creating
  `private` with `mkdirat`+`fchown`+`fchmod`, then re-opening to verify), writes
  an `O_CREAT|O_EXCL` temp with `fchown`/`fchmod`/`fsync` and `renameat`s it into
  the pinned directory, unlinks the temp on any failure, and re-verifies the
  parents after the write. The document is a `no_log` argument, so the module
  never writes its invocation to the target's journal and
  `ansible_inject_invocation` returns nothing sensitive; the write result and
  the digest check are `no_log`.
- The unit state is re-checked after the write and again after verification.
  A unit could start between the pre-write listing and the write, so if any unit
  is no longer `inactive` or `failed` the role fails loudly, warning that a copy
  may have been read mid-rotation; it does not roll the copy back. This narrows,
  but by itself does not eliminate, the check-then-act window (see below).
- It verifies ownership, mode, single link and the SHA-256 of each copy against
  the rendered document, with `no_log`. The stat result carries the checksum, so
  `no_log` on the reinspection and the owner/mode assert is what keeps the
  document digest out of the `-v` output and failure lines. It never reads the
  bytes back to the controller and never prints the values or the digest.
- It starts nothing. Admission still depends on the separately reviewed HBA and
  firewall rules above.

Two residual limitations are accepted, not closed, by design:
- **Check-then-act.** The role stops nothing, so a worker or custody unit could
  start after the final re-check but before a reader opens the copy. The
  operator stops every unit first (the pre-write listing must be `inactive` or
  `failed`); the re-checks catch a unit that starts during the write or verify.
- **Operator-supplied Jinja.** ansible-core renders a trusted `-e`/inventory
  string on any reference and offers no way to read a variable's raw text
  without rendering it. Capturing each input once behind `default(..., true)`
  confines every input to a single render and turns a template that renders
  undefined, such as `{{ {}[lookup('env', …)] }}`, into an empty value. It does
  not neutralise a template that raises: for example
  `{{ lookup('file', lookup('env', 'DITTO_CODING_PG_PASSWORD')) }}` as `enabled`
  or `host` fails the run closed at the capture, writing nothing, but
  ansible-core 2.21.2 prints the raised message, including the value, through
  the task result's `exception` field, which `no_log` deliberately preserves, on
  the console and in any `ANSIBLE_LOG_PATH` log. Core Jinja offers no construct
  that swallows such an error. An operator who pastes hostile Jinja into their
  own command still holds the exported password directly. The write module's
  `no_log` argument and the pipelining guard keep the password off the target's
  disk and journal, but cannot stop an operator who already holds it. Moving
  `host` to a controller-only environment input, like the password, would remove the last
  rendered input; that is a larger interface change left for review.

Root tests check the guard structure. With `DITTO_ANSIBLE_REHEARSAL=1` they also
run the real, restructured role through ansible-core 2.21.2 against a temporary
tree, imported statically as the playbook does, under the repo's `ansible.cfg`
and `-v --diff`, with a stand-in password. That rehearsal proves that a lazily
templated gate, a gate templated to the password, a string `"true"` and
`--start-at-task` at the write, the render, a guard or the include write
nothing; that a preset loop `item`, a raising password variable, and an
`inventory_hostname` and `group_names` forged for a host outside the group are
refused; that `-vvv` with `ansible_inject_invocation` prints nothing sensitive; that a lazily templated `host` writes the safe captured address rather
than the loop-time one; that a `private` or copy swapped for a symlink is never
followed (the write module refuses or replaces it with a real file); that a
rogue inventory host (also under `serial: 1`), check mode, kept remote files,
and `ansible_pipelining` or `ansible_ssh_pipelining` overridden to false or to a
string are refused, with pipelining coming only from the playbook's play vars
and the repo `ansible.cfg`, nothing exported; that `--start-at-task` at any `main.yml` task with the captured gate
and registers preset still writes nothing; that preset results and document variables, forged
`ansible_facts`, `refreshing`, `maintenance` and unknown unit states, and
trailing-newline inputs are refused before anything is written; and that no
password form reaches the console or log even when the owner/mode check fails.
It searches for the stand-in in raw, JSON-, YAML- and repr-escaped forms, for a
canary no escaping changes, and for the SHA-1, MD5 and SHA-256 digests of the
password and of the document, and it pins the raising-template residual above
exactly. The infra CI Ansible job runs it.

The `role_coding_hosted` group connects through IAP only
(`group_vars/role_coding_hosted.yml`). Every native host role therefore reaches
the private address the same way the Platform PostgreSQL VM is reached.
