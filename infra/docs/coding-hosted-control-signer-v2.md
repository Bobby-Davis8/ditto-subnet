# Hosted-v2 control signer: host wiring and protected ceremony

This layer wires the existing default-off Platform signer loader
(`apps/platform/docs/coding-hosted-control-startup-v2.md`) and the validator
control command (`docs/coding-hosted-validator-transport-v2.md`) into host
convergence. Every switch ships off. It does not generate, transport, back up
or activate a key, and CI never handles seed material.

## The key

The control signer is a new **online operational** SR25519 key held only by the
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

`deploy` is `platform_owner`, the user that pm2 runs `ditto-api` as
(`pm2 startup systemd -u deploy`, deploys via `sudo -iu deploy`). The loader's
`read_private` requires the seed and its directory to be owned by the process
UID. The relay processes share the rendered `.env`, but neither the Go relay
nor a Python `DITTO_ROLE=relay` process reads the seed.

## What automation does

| | Disabled (default) | Enabled |
|---|---|---|
| `platform_app` converge | Renders `DITTO_CODING_HOSTED_CONTROL_ENABLED=false`. Includes no signer task, so the seed path is never inspected. | Before touching the host, asserts the fixed path and a 48-character SS58 hotkey distinct from the screener hotkey, then stat-verifies ancestors, directory and seed with `follow: false` and `get_checksum: false`. Renders `true`, the literal seed path and `DITTO_CODING_HOSTED_SIGNER_HOTKEY`. |
| `validator_stack` converge | Renders `VALIDATOR_CODING_HOSTED_CONTROL_ENABLED=false` and an empty `VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY`. | Asserts a 48-character SS58 address different from `validator_stack_hotkey`, then renders both. |
| `docker-compose.yml` | `${VALIDATOR_CODING_HOSTED_CONTROL_ENABLED:-false}`, `${VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY:-}` | Passes the operator's values through to the `ditto-subnet` container |

No task creates, copies, templates, fetches, slurps, hashes, moves or deletes
the seed. A missing or unsafe seed fails the converge with a pointer here. The
`platform_app` role does not restart `ditto-api`. Startup is fail-closed: if the
seed later disappears or no longer matches the hotkey, `ditto-api` refuses to
start at its next restart.

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

## Enabling order

1. Ceremony places the seed on the target Platform host and records the public
   address.
2. Reviewed Git change to `infra/ansible/host_vars/ditto-platform-<env>.yml`:
   `platform_coding_hosted_control_enabled: true` and
   `platform_coding_hosted_signer_hotkey: <public SS58>`.
3. Converge `infra/ansible/playbooks/gcp-platform-app.yml --limit ditto-platform-<env>`.
   The signer guard runs before anything else on the host.
4. Restart `ditto-api` through the ordinary Platform deploy path
   (`scripts/update.sh` sources `.env` and reloads). Startup derives the key,
   compares it with the hotkey and runs a sign/verify challenge.
5. Verify through Backroom `get_coding_control_plane` that
   `hosted_control_configured=true`, `shadow_only=true` and
   `weight_eligible=false`.
6. Reviewed validator trust change, e.g. in
   `infra/ansible/host_vars/ditto-validator-prod.yml`:
   `validator_stack_coding_hosted_control_enabled: true` and
   `validator_stack_coding_hosted_platform_hotkey: <same public SS58>`, taken
   from the reviewed Platform change or custodian record, never from a
   Platform response.
7. Converge `infra/ansible/playbooks/gcp-validator-prod.yml`. The command also
   needs a validator stack release that contains it and this Compose
   pass-through.

## Rotation

There is no hotkey fallback and validators trust exactly one address. Rotate
when no hosted operation is in flight:

1. Ceremony creates a new seed and places it at the fixed path by exclusive
   create and rename, keeping owner, mode and single link.
2. Reviewed change to the Platform hotkey, converge, restart, verify step 5.
3. Reviewed change to every validator's trust address, converge.
4. Custodians retire the old seed and backup under their policy.

Between steps 2 and 3, validator commands fail verification. That is the
intended fail-closed state.

## Revocation

1. Set `platform_coding_hosted_control_enabled: false`, converge and restart.
   Platform stops signing and `hosted_control_configured` reads `false`. The
   disabled loader does not read the path.
2. Set `validator_stack_coding_hosted_control_enabled: false` (and clear the
   address), then converge. Any validator operator can do this independently.
3. The ceremony removes the seed from the host and records the revocation.
   Automation never deletes it. A replacement requires a fresh ceremony.

## Validator trust

Validators trust only `VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY`. The address never
comes from a command argument, a Platform response, a wallet or a key file,
and it cannot equal the validator's own hotkey. Community validators keep both
values off and empty unless they opt in through their own reviewed
configuration. The validator worker never reads either value.

## Validation

- `uv run pytest -q ditto/tests/test_coding_hosted_control_signer_wiring.py`
- `cd apps/platform && uv run pytest -q -n 0 ditto/tests/api_server/test_coding_hosted_signer_host_wiring.py`
- From `infra/ansible`:
  `uvx --from ansible-core==2.21.2 ansible-playbook -i localhost, tests/coding-hosted-control-signer.yml`
  renders both env templates with real Ansible and drives the stat guard
  through synthetic temporary trees (missing, wrong mode, symlink, hard link,
  wrong size, unsafe directory or ancestor, foreign owner).
