# Native-v2 host prerequisites

The `coding_hosted_prerequisites` role fixes the host values the hosted runtime
reads for candidate networking and identity, checks them against the live host,
and installs a manual egress proxy unit with an empty allowlist. It defaults
off. It creates no account, no Docker network, no nft rule and no credential,
and it never enables or starts a service. It is not a worker installation, a
connectivity profile, packet-enforcement evidence or canary approval.

## What the runtime needs

`services/dittobench-api/internal/codinghostedruntime/config.go` loads four
host-bound fields that "the operator must provision beforehand". The role
pins each one to a closed value; none is an operator input.

| Field | Fixed value | Runtime check | Host prerequisite |
|---|---|---|---|
| `router_listen` | `<host>:18080` | Private, non-loopback IPv4 and canonical port 1024–65535 (`config.go:168-173`). The address is also the sandbox host gateway | The worker binds it during an attempt. The role proves the address is local, the exact listener is free and the port is outside the ephemeral range |
| `egress_proxy` | `http://<host>:18090` | `http`, private non-loopback IPv4, explicit port, no credentials, path, query or fragment (`config.go:174-181`) | Refusing proxy unit, installed but not started |
| `egress_network` | `ditto-coding-restricted` | Lowercase identifier (`config.go:200-205`); must be nonempty (`codingharness/sandbox.go:43`) | None. See below |
| `candidate_uid`, `candidate_gid` | `10001` | Nonzero (`codingexecutor/factory.go:42`); the Rust driver requires exactly 10001 (`codingexecutor/docker.go:167`) | Must map into the daemon's subordinate range. No host account |

The runtime never attaches containers to the configured network name. For each
harness start, `sandbox.LocalDocker` creates a fresh ICC-disabled
`ditto-job-<id>` bridge in the rootless daemon and removes it at stop; executor
phases run with `--network none`. The name only switches that behaviour on. So
the role **does not create a Docker network**: a standing network would be
unused daemon state. It rejects names beginning with `ditto-job-`.

Rootless Docker maps container ID 0 to the daemon account and IDs 1–65,536 into
its single subordinate range. Candidate 10001 is therefore host ID
`subuid_start + 10000` (and likewise for groups). The daemon role already
refuses any host account inside that range; this role reports the mapped IDs.

## One-time and per-attempt state

This role owns only static host state. Per-attempt authority stays elsewhere:

- The [connectivity profile](coding-hosted-connectivity-v2.md) grants the
  daemon's `candidate_tcp` access for one window. It must list `<host>:18080`.
  Bounded rollout also requires `<host>:18090`; in single-attempt mode listing
  the proxy is optional, and an unlisted proxy is simply unreachable.
- The worker binds the router; the runtime creates and removes each bridge; the
  Platform companion creates `state_root` for each attempt.
- An operator starts the proxy before the worker and stops it afterwards.

## Egress model

Candidate traffic leaves the per-run bridge through RootlessKit as the daemon
UID, which the deny guard blocks except for the connectivity window. Inference
and workspace tools reach the router at `host.docker.internal`, exempt through
`NO_PROXY`. Hosted-v2 candidates need no other destination, so the proxy has
an **empty allowlist**: `CONNECT` gets `403`, any other request `405`, and the
connection closes. It has no upstream connection code. Its unit runs as a
`DynamicUser`, allows only `AF_INET`, binds only TCP 18090, denies every IP peer
except `<host>/32`, and discards output rather than logging candidate data.
A non-empty allowlist would be a separate reviewed change, not an input.

## Inputs and refusals

- `coding_hosted_prerequisites_enabled: true`;
- `coding_hosted_prerequisites_confirmation`: exactly
  `CONVERGE NATIVE CODING HOST PREREQUISITES`;
- `coding_hosted_prerequisites_source_revision`: the reviewed lowercase
  40-character commit;
- `coding_hosted_prerequisites_host_address`: the host's primary IPv4 from the
  reviewed host apply, `10.33.0.2`–`10.33.0.253`, equal to the gathered default
  IPv4 address.

The play targets `role_coding_hosted` and requires hostname
`ditto-coding-hosted-v2`, Debian 13 and x86_64. It refuses check mode when
enabled. Before any write it also refuses:

- an inactive `ditto-coding-hosted-egress.service` deny guard;
- a live `ditto-coding-hosted-worker.service`, `ditto-coding-custody@*.service`
  or egress proxy (any state other than inactive or failed);
- a missing or unsafe daemon policy directory or `host-policy.py`;
- any installed file whose bytes, owner, mode or link count differ from this
  revision, and any unit override or drop-in;
- a non-root check, a host policy rejection, an unmapped candidate identity, a
  non-local address, a busy listener or a port in the ephemeral range;
- a unit that `systemd-analyze verify` reports anything about.

The helper and unit are checked from a scratch directory that is then removed,
so a failed check leaves no installed file.

## What it changes

| Path | Owner, mode | Purpose |
|---|---|---|
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites.py` | root, `0444` | Read-only `check` helper |
| `/usr/local/lib/ditto-coding-hosted/egress-proxy.py` | root, `0444` | Refusing proxy |
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites.json` | root, `0444` | Fixed host record; the per-attempt `host` settings must match it |
| `/etc/systemd/system/ditto-coding-hosted-egress-proxy.service` | root, `0644` | Proxy unit without an `[Install]` section |

Writes never replace existing bytes, and the role then reloads unit definitions.
A rerun with identical files changes nothing.

```text
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-coding-hosted-prerequisites.yml \
  --limit ditto-coding-hosted-v2 \
  -e '{"coding_hosted_prerequisites_enabled":true,
       "coding_hosted_prerequisites_confirmation":"CONVERGE NATIVE CODING HOST PREREQUISITES",
       "coding_hosted_prerequisites_source_revision":"<merged-source-sha>",
       "coding_hosted_prerequisites_host_address":"<reviewed-primary-ipv4>"}'
```

Around one separately approved attempt:

```text
sudo systemctl start ditto-coding-hosted-egress-proxy.service
# start custody and the worker as their own documents describe
sudo systemctl stop ditto-coding-hosted-egress-proxy.service   # after the worker has stopped
```

## Verification

After convergence the role requires the unit to be loaded from the installed
file with no drop-ins, `UnitFileState=static`, `ActiveState=inactive`, and each
file to match its rendered bytes. It prints the values with
`services_started=false`, `shadow_only=true` and `weight_eligible=false`. To
recheck later, with the worker and proxy stopped:

```text
systemctl show ditto-coding-hosted-egress-proxy.service \
  -p UnitFileState -p ActiveState -p DropInPaths -p FragmentPath
sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-hosted/host-prerequisites.py check \
  < /usr/local/lib/ditto-coding-hosted/host-prerequisites.json
```

These are configuration checks. They do not prove candidate packet denial,
proxy reachability, bridge isolation or cleanup on the real host; the
network-enforcement evidence stays a separate qualification record.

## Rollback and removal

There is no account, Docker network, firewall rule or data to remove. With the
worker, every custody instance and the proxy stopped:

```text
sudo systemctl stop ditto-coding-hosted-egress-proxy.service
sudo rm /etc/systemd/system/ditto-coding-hosted-egress-proxy.service
sudo systemctl daemon-reload
sudo rm /usr/local/lib/ditto-coding-hosted/egress-proxy.py \
  /usr/local/lib/ditto-coding-hosted/host-prerequisites.py \
  /usr/local/lib/ditto-coding-hosted/host-prerequisites.json
```

Reissue any connectivity profile that names the proxy. A new host address or a
changed role revision needs this removal and a fresh reviewed convergence; the
role refuses to rewrite the old files in place.

## Validation

```bash
uv run pytest -q ditto/tests/test_coding_hosted_prerequisites.py
(cd services/dittobench-api && go test ./internal/codinghostedruntime -run Prerequisites)
cd infra/ansible
uvx --from ansible-core==2.21.2 ansible-playbook --syntax-check \
  -i inventory/validator-static.yml playbooks/gcp-coding-hosted-prerequisites.yml
uvx --from ansible-core==2.21.2 ansible-playbook --check \
  -i localhost, tests/coding-hosted-prerequisites.yml
```

The Go test renders the role's record and passes it through the real runtime
loader. The fixture runs the real input guard against synthetic facts. None of
this touches a host, daemon, firewall or provider. Shadow-only operation and
`weight_eligible=false` remain mandatory.
