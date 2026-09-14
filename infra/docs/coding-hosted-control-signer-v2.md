# Hosted-v2 control signer: host wiring and protected ceremony

This layer wires the existing default-off Platform signer loader
(`apps/platform/docs/coding-hosted-control-startup-v2.md`) and the validator
control command (`docs/coding-hosted-validator-transport-v2.md`) into host
convergence and the Platform deploy. Every switch ships off. It does not
generate, transport, back up or activate a key. No CI job, workflow input or
artifact contains seed material. That does not keep deploy tooling away from
the seed on the host; see [Who can read the seed](#who-can-read-the-seed).

## The key

The control signer is a new **online operational** SR25519 key held by the
Platform API process. It signs canonical native-v2 status and result envelopes
and nothing else. It is distinct from the offline curator Ed25519 key, from
every validator hotkey and coldkey, from the screener hotkey and from any
payment wallet. A signature never makes an operation weight-eligible.

## Fixed placement on the Platform host

| Item | Requirement |
|---|---|
| Seed file | `/etc/ditto-platform/coding-hosted-signer/seed`, fixed, not configurable |
| Content | Raw 32-byte SR25519 seed. No hex text, mnemonic, seed URI or wallet file |
| File | Regular file, single link, mode `0600`, owner `deploy` |
| Directory | `/etc/ditto-platform/coding-hosted-signer`, real directory, mode `0700`, owner `deploy` |
| Ancestors | `/etc/ditto-platform`, `/etc`, `/`: real directories owned by root or `deploy`, no group or world write |

The path is a literal in `platform.env.j2` and in the import vars of
`roles/platform_app/tasks/coding_hosted_signer.yml`. It is not a role default
or inventory variable.

`deploy` is `platform_owner`, the user pm2 runs `ditto-api` as
(`pm2 startup systemd -u deploy`; the Deploy Platform workflow runs
`sudo -iu deploy ... ./scripts/update.sh`). The loader's `read_private` requires
the seed and its directory to be owned by the process UID, so today the seed
must be owned by `deploy`.

## Who can read the seed

File permissions do not separate `ditto-api` from anything else that runs as
`deploy`. **Any process running as `deploy` can read the seed**, including:

- every pm2 app on the host: `ditto-api`, the Go relays `ditto-api-relay-1`
  and `ditto-api-relay-2` (which serve the public inference path through Caddy)
  and the daily `ditto-screened-image-cleanup` job
  (`apps/platform/scripts/ecosystem.config.js`);
- every child of those processes, for example `gcloud`, `skopeo`, the Docker
  CLI and the hosted runtime helper executables that `ditto-api` starts;
- everything `scripts/update.sh` runs on each deploy: `git`, `uv sync` (package
  build backends), `npm ci` and `npm run build` (package lifecycle scripts),
  `alembic` and `docker compose`;
- anyone who can `sudo -iu deploy`: the Deploy Platform workflow's IAP SSH
  identity, and every `ditto` group member through
  `%ditto ALL=(deploy) NOPASSWD: ALL` in `roles/base/tasks/users.yml`.

`deploy` is root-equivalent too. It is in the `docker` group
(`roles/platform_app/tasks/main.yml`), and `ditto` members may run
`sudo /bin/journalctl *`, which has a documented pager shell escape. A process
running as `deploy` can therefore also read files owned by other users.

"The relay never reads the seed" is a property of the code: the Go relay has no
loader, and the Python loader returns before touching the path unless
`DITTO_ROLE` selects the API process. It is not an operating-system boundary.

## What automation does

| | Disabled (default) | Enabled |
|---|---|---|
| `platform_app` converge | Renders `DITTO_CODING_HOSTED_CONTROL_ENABLED=false`. The statically imported signer tasks are all skipped, so the seed path is never inspected. | Right after the role's preflight, before any other `platform_app` task (the playbook runs the `base` role first), asserts a 48-character SS58 hotkey that differs from the screener hotkey and a non-root API user. It then stat-verifies ancestors, directory and seed with `follow: false` and `get_checksum: false`. Renders `true`, the literal seed path and `DITTO_CODING_HOSTED_SIGNER_HOTKEY`. |
| `scripts/update.sh` deploy | Skips the signer check. | After sourcing `.env` and `.env.deploy`, and before Pylon, migrations or pm2 are touched, runs `uv run python -m ditto.api_server.coding_hosted_signer_preflight --check-metadata`. That entry point uses the loader's own path checks through `lstat` and never opens the seed. On failure the deploy stops at stage `signer-preflight` and the checkout rolls back while the old process keeps serving. |
| `validator_stack` converge | Renders `VALIDATOR_CODING_HOSTED_CONTROL_ENABLED=false` and an empty `VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY`. | Before any other `validator_stack` task (the playbook runs `base` first), requires both this address and `validator_stack_hotkey` to be exact 48-character SS58 strings and different, then renders both. |
| `docker-compose.yml` | `${VALIDATOR_CODING_HOSTED_CONTROL_ENABLED:-false}`, `${VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY:-}` | Passes the operator's values through to the `ditto-subnet` container |

No task creates, copies, templates, fetches, slurps, hashes, moves or deletes
the seed. A missing or unsafe seed fails the converge, or the deploy, with a
pointer here. Neither check can read the seed, so neither can catch a hotkey
that does not match it or an all-zero seed. Only API startup catches those.

## How ditto-api picks up a change

- A converge rewrites `.env` but never restarts `ditto-api`.
- `scripts/update.sh` sources `.env`, then calls
  `pm2 start|reload scripts/ecosystem.config.js --only ditto-api --update-env`
  and `pm2 save`. It reloads `ditto-api` even when the revision is unchanged.
  Run it through the Deploy Platform workflow
  (`.github/workflows/platform-deploy.yml`, `workflow_dispatch`) for the
  revision already in service.
- pm2 restarts `ditto-api` on its own after a crash or `max_memory_restart`
  (`autorestart: true`, `max_restarts: 10`, `min_uptime: 10s`), and resurrects
  it after a reboot. It then reuses the environment from the last
  `--update-env` and `pm2 save`, **not** the current `.env`. A bare
  `pm2 restart ditto-api` does not refresh it either.
- The disabled template renders an explicit `false`, so a refreshed
  environment overrides an earlier `true`.

So while pm2's saved environment enables the signer, the matching seed must stay
in place. If it disappears or is replaced, the next automatic restart fails
closed, and pm2 stops retrying once `ditto-api` is `errored`. The orders below
never create that state.

## Protected ceremony (outside this repository)

Seed creation, backup, placement and production trust activation are a separate
protected ceremony run by the key custodians. This document fixes only the
properties the ceremony must meet:

- Generate the seed on an offline or otherwise protected machine the
  custodians control. Never in CI, a workflow, a workflow artifact, Git, chat
  (including Telegram), tickets or this repository's automation.
- Use fresh randomness for this key alone. Never derive or reuse it from the
  curator key, a validator or screener key, a mnemonic or any wallet.
- Record only the derived public SS58 address for review.
- Place the seed directly at the fixed path with the owner, modes and single
  link above, created exclusively rather than edited in place. Leave no
  intermediate copy on another host, image, candidate or validator host, shell
  history or temporary file.
- Custodians hold the backup under their own custody policy and rehearse
  recovery. The backup never enters automation.
- Verify with metadata only. Never print or hash the seed in shared output.

## Activation is a reviewed change

`ditto/tests/test_coding_hosted_control_signer_wiring.py` parses every
inventory, playbook and workflow file. It fails when
`platform_coding_hosted_control_enabled` or
`validator_stack_coding_hosted_control_enabled` is set truthy in a file that is
not listed in its `REVIEWED_ACTIVATIONS` table. The check reads values, not
names. Staging a public hotkey, or setting a flag to `false` while revoking,
needs no entry. An activation pull request adds its host_vars file to
`REVIEWED_ACTIVATIONS` in the same reviewed change.

## Enabling order

1. The ceremony places the seed on the target Platform host and records the
   public address.
2. Reviewed Git change to `infra/ansible/host_vars/ditto-platform-<env>.yml`:
   `platform_coding_hosted_control_enabled: true` and
   `platform_coding_hosted_signer_hotkey: <public SS58>`, plus its
   `REVIEWED_ACTIVATIONS` entry.
3. Converge `infra/ansible/playbooks/gcp-platform-app.yml --limit ditto-platform-<env>`.
   The signer guard runs before any other `platform_app` task.
4. Run the Deploy Platform workflow for the revision in service. `update.sh`
   runs the metadata preflight, then reloads `ditto-api` with
   `--update-env` and saves it. Startup derives the key, compares it with the
   hotkey and runs a sign/verify challenge.
5. Verify through Backroom `get_coding_control_plane` that
   `hosted_control_configured=true`, `shadow_only=true` and
   `weight_eligible=false`.
6. Reviewed validator trust change, for example in
   `infra/ansible/host_vars/ditto-validator-prod.yml`:
   `validator_stack_coding_hosted_control_enabled: true` and
   `validator_stack_coding_hosted_platform_hotkey: <same public SS58>`, plus its
   `REVIEWED_ACTIVATIONS` entry. Take the address from the reviewed Platform
   change or the custodian record, never from a Platform response.
7. Converge `infra/ansible/playbooks/gcp-validator-prod.yml`. The command also
   needs a validator stack release that contains it and this Compose
   pass-through.

If the hotkey reviewed in step 2 does not match the seed, the metadata preflight
in step 4 still passes, and `ditto-api` refuses to start after the reload. The
deploy fails at `verify` with the API down. Recover with steps 1 and 2 of
Rotation (disable, then deploy).

## Rotation

There is no hotkey fallback, and validators trust exactly one address. Rotate
when no hosted operation is in flight. Never touch the seed while pm2's saved
environment enables the signer.

1. Reviewed change setting `platform_coding_hosted_control_enabled: false`
   (and removing its `REVIEWED_ACTIVATIONS` entry). Converge
   `gcp-platform-app.yml --limit ditto-platform-<env>`.
2. Run the Deploy Platform workflow for the revision in service. `update.sh`
   skips the signer check and reloads `ditto-api` with the disabled environment
   (`--update-env`, then `pm2 save`).
3. Confirm through `get_coding_control_plane` that
   `hosted_control_configured=false`. From here validator commands fail
   verification, which is the intended fail-closed state.
4. The ceremony replaces the seed at the fixed path, keeping owner, mode and
   single link. It creates the new file exclusively and renames it into place,
   then records the new public address. Custodians retire the old seed and
   backup under their policy.
5. Reviewed change setting `platform_coding_hosted_control_enabled: true` and
   the new `platform_coding_hosted_signer_hotkey`, restoring the
   `REVIEWED_ACTIVATIONS` entry. Converge. The stat guard checks the new
   placement.
6. Run the Deploy Platform workflow again. The metadata preflight runs before
   pm2 is touched, then `ditto-api` reloads with the new hotkey.
7. Verify `hosted_control_configured=true`, `shadow_only=true` and
   `weight_eligible=false`.
8. Reviewed change to every validator's trust address, then converge each
   validator.

## Revocation

1. Validators remove trust independently of Platform, and first when a
   compromise is suspected: set `validator_stack_coding_hosted_control_enabled:
   false`, clear the address and converge. The validator control command then
   refuses to run, whoever signed the envelope.
2. Set `platform_coding_hosted_control_enabled: false` and converge.
3. Run the Deploy Platform workflow for the revision in service, so `ditto-api`
   restarts with the refreshed, disabled environment.
4. Confirm `hosted_control_configured=false`. The disabled loader no longer
   reads the path, and neither will a later automatic restart.
5. Only then does the ceremony remove the seed from the host and record the
   revocation. Automation never deletes it. A replacement requires a fresh
   ceremony and the enabling order.

## Validator trust

Validators trust only `VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY`. The address never
comes from a command argument, a Platform response, a wallet or a key file,
and it cannot equal the validator's own hotkey. Community validators keep both
values off and empty unless they opt in through their own reviewed
configuration. The validator worker never reads either value.

## Risks and open decisions

### The seed is readable by every `deploy` process

Leaving the flag off avoids this risk. Enabling it as designed accepts that a
code-execution bug in a relay or the cleanup job, a malicious package script run
by `update.sh`, or anyone who can act as `deploy` can copy the control key. What
they gain is the ability to forge shadow status and result envelopes that trusting
validators accept. Weights are not affected. Revocation is the remedy.

Running `ditto-api`, and only `ditto-api`, under a dedicated user would take
these changes:

1. **User and file ownership.** Add a system user (for example `ditto-api`) with
   no login and no sudo, outside the `docker` and `ditto` groups.
   `read_private` and `private_directory` compare owners with the process UID.
   So the seed and its directory move to that user. So do the other files the
   API checks the same way: the Hippius evidence spool
   (`platform_coding_hippius_evidence_spool_root`) and the authority files the
   role asserts `pw_name == platform_owner`. That needs a new variable such as
   `platform_api_user`, used by those tasks and by this guard instead of
   `platform_owner`. `.env` is `deploy:ditto 0640` today, so the API user needs
   its own readable copy or an `EnvironmentFile=`. Log paths and the
   `gcloud`/`skopeo` home directories need the same treatment.
2. **Process manager.** pm2's per-app `uid`/`gid` only works when the pm2
   daemon runs as root, which would give root a checkout that `deploy` can
   write. Not an option. The alternatives are a second pm2 daemon for the new
   user (`pm2 startup systemd -u ditto-api`, separate `PM2_HOME`) or a systemd
   unit `ditto-api.service` (`User=`, `EnvironmentFile=`, `Restart=`,
   `MemoryMax=` for `max_memory_restart`, sandboxing). A unit could also use
   `LoadCredential=`, so the at-rest seed is root-owned. That needs the
   `read_private` owner and mode rules revisited, and has not been checked.
   Relays and the cleanup job stay on `deploy`'s pm2.
3. **`scripts/update.sh`.** Today it runs as `deploy` and drives `ditto-api`
   with `pm2 jlist`, `pm2_deploy_plan.js`, `pm2 start|reload --update-env`,
   `pm2 save` and the pm2-based verification loop. With a separate manager,
   `ditto-api` needs its own restart and state probe. The existing sudoers
   grant `%ditto ... /bin/systemctl restart ditto-*` would already cover
   `systemctl restart ditto-api`. The update.sh test harness
   (`apps/platform/ditto/tests/scripts/test_update_script.py`) would need a
   matching path.
4. **Code integrity.** This is the part that decides whether the split helps.
   `ditto-api` runs `/opt/ditto-subnet` and its `.venv`, and `deploy` writes
   both (`git reset --hard`, `uv sync`). Anything running as `deploy` can change
   that code and have it run as the new user at the next restart. A user split
   alone turns "read the seed now" into "read it after the next restart". To
   close the gap, the API must run from a release directory `deploy` cannot
   write, installed by a root-owned step. The relay already uses immutable
   releases, but those are `deploy`-writable too.
5. **Root-equivalent paths.** While `deploy` is in the `docker` group
   (`update.sh` uses it for `docker compose up`) and `ditto` members may run
   `sudo journalctl *`, any `deploy` process can read any user's files. Both
   would need narrowing, for example Pylon compose through a root-owned unit and
   `journalctl --no-pager`-only grants.
6. **Unaffected.** Caddy reaches the API over TCP
   (`reverse_proxy localhost:{{ platform_api_port }}` in `Caddyfile.j2`), so
   the upstream does not change. Children the API itself starts (hosted helpers,
   `gcloud`, `skopeo`, Docker CLI) would still share its user.

Options for review:

- **A.** Keep the current design and document the exposure, with the flag off
  until someone accepts it for a time-boxed shadow canary. No code change.
- **B.** User split only (items 1 to 3). A moderate change. It stops casual reads
  by relays, the cleanup job and package scripts, but not persistence through
  the checkout or the root-equivalent paths.
- **C.** User split plus root-owned immutable API releases plus narrowed
  `docker` and `sudo` grants (items 1 to 5). The only host-local option that
  keeps `deploy` away from the key. Redesigns the Platform API deploy.
- **D.** Move signing out of the API process into a separately deployed signer
  with its own user and code path, or remote or hardware custody. The Platform
  startup doc already requires a reviewed adapter for that.

### Other open decisions

- Only API startup detects a hotkey that does not match the seed. A wrong
  reviewed hotkey takes `ditto-api` down at deploy until it is disabled again.
- The relay processes do not read the three `DITTO_CODING_HOSTED_*` keys.
  Blanking them in `ecosystem.config.js` would cost a relay rollout and would
  not stop a relay from reading the seed file.
- Is Secret Manager ruled out as the custodian backup location? Any process on
  the VM can use the VM service account, so it is not a per-user boundary.
- The Ansible ancestor rule forbids group or world write even on root-owned
  sticky directories, which the Python loader accepts. The update.sh preflight
  uses the loader's rule. The fixed path's ancestors are not sticky, so both
  agree there.

## Validation

- `uv run pytest -q ditto/tests/test_coding_hosted_control_signer_wiring.py`
- `cd apps/platform && uv run pytest -q -n 0 ditto/tests/api_server/test_coding_hosted_signer_host_wiring.py ditto/tests/api_server/test_coding_hosted_signer_preflight.py ditto/tests/scripts/test_update_script.py`
- From `infra/ansible`:
  `uvx --from ansible-core==2.21.2 ansible-playbook -i localhost, tests/coding-hosted-control-signer.yml`
  renders both env templates with real Ansible and runs the real guards:
  - validator trust cases, including padded own hotkeys;
  - Platform profile cases: screener hotkey, root user;
  - the disabled and enabled entry point on the fixed path;
  - the stat guard on synthetic temporary trees: missing, wrong mode, symlink,
    hard link, wrong size, unsafe directory or ancestor, foreign owner.
