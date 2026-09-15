# Native worker unit and custody template uninstall

The `coding_hosted_unit_uninstall` role and
`infra/ansible/playbooks/gcp-coding-hosted-unit-uninstall.yml` remove the two
systemd unit files the native Coding host's install roles create on
`ditto-coding-hosted-v2`, then run `systemctl daemon-reload`. The role is off by
default. It stops nothing and starts nothing. It needs no secret.

## What it removes

| Path | Installed by |
|---|---|
| `/etc/systemd/system/ditto-coding-hosted-worker.service` | `coding_hosted_connectivity` |
| `/etc/systemd/system/ditto-coding-custody@.service` | `coding_hosted_custody_service` |

Neither install role adds a drop-in, an instance file or an `[Install]` section,
so it never creates an enablement link. The role removes nothing else.
`ditto/tests/test_coding_hosted_unit_uninstall.py` parses both install roles
and fails if any of these changes:

- the template destination, owner or mode;
- a template gains `[Install]`;
- an install role enables or drops in either unit;
- any other role writes a path under `/etc/systemd/` for either unit.

## What it never touches

- Credentials. The PostgreSQL environment copies belong to the cleanup role in
  #1897, and the worker credential files belong to the cleanup role in #1925.
- Data directories under `/var/lib/ditto-coding-hosted` and
  `/var/lib/ditto-coding-custody`, including release inputs, the RSA key and
  per-run configuration.
- Accounts, Docker, the rootless daemon, the egress guard and the runtime
  install.
- The running state of any unit.

## Run it

First stop the hosted worker and every custody instance. Each unit must be
inactive or failed. Then run:

```bash
GCP_OSLOGIN_USER=… ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-coding-hosted-unit-uninstall.yml \
  --limit ditto-coding-hosted-v2 \
  -e '{"coding_hosted_unit_uninstall_enabled": true,
       "coding_hosted_unit_uninstall_confirmation": "UNINSTALL NATIVE CODING WORKER AND CUSTODY UNITS",
       "coding_hosted_unit_uninstall_source_revision": "<40-char reviewed commit>"}'
```

Pass the inputs as JSON. The `key=value` form makes the flag the string
`"true"`, and the role treats that as disabled.

## Guards, in order

1. **Preset names (`main.yml`).** Before the role creates any name, it refuses
   every `coding_hosted_unit_uninstall_*` variable except the three inputs. That
   covers a `-e` preset of the gate, a captured input or a registered result.
2. **Gate freeze.** The enabled flag is frozen once, under `no_log`, as
   `(enabled | default(false, true)) is sameas true`. The `bool` filter is never
   used: on ansible-core 2.21.2 it prints its input in a deprecation warning.
3. **Dynamic include.** The removal lives in `remove.yml`, reached through
   `include_tasks`. `--start-at-task` cannot jump into it.
4. **Include preset refusal.** The first task in `remove.yml` repeats the preset
   refusal, with only the gate added to the allowed names. It also requires the
   raw enabled flag to be boolean true. So neither a `--start-at-task` past
   `main.yml` nor a preset gate can open the removal.
5. **One host.** The play must target exactly one host. Both
   `ansible_play_batch == ['ditto-coding-hosted-v2']` and
   `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` must hold, so
   `serial: 1` without `--limit` is refused.
6. **Check mode.** An enabled run in check mode is refused. The disabled fixture
   covers `--check`.
7. **Confirmation and revision.** Both are frozen once under `no_log` and must
   match the exact confirmation and a 40-character lowercase-hex revision.
8. **Host identity.** The host must be Debian 13 x86_64 `ditto-coding-hosted-v2`.
   Identity comes from a registered `setup` probe. The playbook sets
   `gather_facts: false` because gathered facts could be forged with `-e`.
9. **Live units (refuse first).** Every `systemctl list-units` line for the
   worker or a `ditto-coding-custody@*` instance must show the unit inactive or
   failed. An empty listing passes. Anything else is refused, including
   `active`, `activating`, `deactivating`, `reloading`, `refreshing`,
   `maintenance`, an unknown state or an unparseable line.
10. **Pinned unlink.** The role-local `coding_hosted_unit_uninstall_unlink`
    module opens `/etc/systemd/system` from `/` one component at a time with
    `O_NOFOLLOW`. The `etc/systemd/system` directories must be root-owned and
    not writable by group or others.
    - It first scans the unit directory for anything neither install role
      creates: a drop-in, an instance file, or a link in a `.wants`,
      `.requires` or `.upholds` directory. If it finds one, it refuses and
      removes nothing.
    - It removes the two names with `unlinkat`. It refuses a symlink, directory,
      special file, hard link or non-root file, and attempts nothing after the
      first refusal.
11. **daemon-reload.** This always runs after the module, even after a partial
    removal, so systemd never keeps a definition whose file is gone.
12. **Receipt.** The run fails unless the module refused nothing and both paths
    were removed or already absent. The failure lists `refused`, `foreign`,
    `removed`, `already_absent` and `not_attempted`.
13. **Re-check.** The units must still be inactive or failed, and
    `systemctl list-unit-files` must list neither name. A match means a
    definition outside `/etc/systemd/system` that this role never removes.

Every failure message is constant, apart from the module's fixed-path lists. The
report shows only the source revision and the `removed` and `already_absent`
lists.

A second run is idempotent. It reports both paths as `already_absent` and
reloads again.

## Residuals

- Files the install roles create outside the unit directory stay on the host:
  - `custody-run.py`
  - `/etc/ditto-coding-custody/base.json`
  - the worker's unwrap proxy
  - release inputs
  - `connectivity-policy.py`
  - `/etc/ditto-coding-hosted/connectivity.json`

  Without their units these files are inert. Removing them would need its own
  reviewed role.
- Failed instances stay in `list-units` as `not-found failed` until someone runs
  `systemctl reset-failed`. The role runs no state-changing systemctl command.
- The module scans only `/etc/systemd/system`. It does not scan
  `/etc/systemd/system.control`, `/run/systemd` or `/usr/lib/systemd`. The
  post-reload `list-unit-files` check catches a unit file there, but not a
  drop-in alone.
- Reinstalling means running `coding_hosted_connectivity` and
  `coding_hosted_custody_service` again with their own reviewed inputs.

## Rehearsal

Setting `DITTO_ANSIBLE_REHEARSAL=1` enables the rehearsal:

```bash
DITTO_ANSIBLE_REHEARSAL=1 uv run --locked --only-group dev \
  pytest -p no:cacheprovider -rs ditto/tests/test_coding_hosted_unit_uninstall.py
```

It runs the real role through ansible-core 2.21.2 against temporary unit trees. The Ansible
job in `infra-ci.yml` runs the same command. The rehearsal covers:

- a disabled or non-boolean flag doing nothing;
- a wrong confirmation or revision;
- every live-unit state;
- every preset internal name;
- `--start-at-task` at every task;
- symlink, hard link, directory, parent-symlink, drop-in and enablement-link
  swaps;
- an extra host and a `serial: 1` play;
- forged facts;
- partial removal reporting;
- an idempotent second run.

It then removes or weakens each guard in turn and shows that the unsafe outcome
follows.
