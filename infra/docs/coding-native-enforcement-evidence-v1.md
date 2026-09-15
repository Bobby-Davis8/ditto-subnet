# Native enforcement evidence v1

This is the B5 evidence format for the hosted-v2 coding canary. It covers the
four `evidence_sha256` entries that the host preflight marks as pending:
network, resource, pre-exec and cleanup. This layer has three parts:

- the record format;
- the probe catalog;
- an offline tool, `infra/scripts/coding-native-evidence.py`.

Nothing here runs a probe, reaches a host or daemon, reads a custody path, or
creates approval. The probe runner, bundle shipping and collection come in
later PRs. Collectors never run from `coding-hosted-operate`, and a test checks
that no workflow except the offline regression job names these tools.

`native.py` still checks the six evidence values only as nonzero digests. Their
content is guaranteed only by this verifier plus Peyton's own review and
signature.

## Record

Schema `dittobench-coding-native-enforcement-evidence-v1`. All keys are closed;
there is no approval or readiness key. One record covers one `kind`:
`network_enforcement`, `resource_enforcement`, `preexec_confinement` or
`cleanup_recovery`.

| Field | Content |
|---|---|
| `host` | `machine_id_sha256`, `boot_id`, `kernel_release`, `daemon_identity_sha256`, `subordinate_ids` (uid/gid start and count), `router_namespace` (`host` or `rootless-netns`) |
| `release` | `source_revision`, `release_manifest_sha256`, `runtime_archive_sha256`, `image_approval_sha256` for go, node, python and rust |
| `pre_collection_preflight_sha256` | The preflight stdout taken before collection, retained in the store |
| `inputs` | Per kind: the connectivity profile digest (network) or the execution/grading profile digests. Each must equal the document supplied to the verifier |
| `endpoints` | Network only. Roles `router` and `refusing_proxy` (one each), `trusted` (1 to 32) and `trusted_dns` (0 to 2), each as `endpoint_sha256`. The set must equal the hashes derived from the connectivity profile |
| `tools` | `catalog_sha256`, `collector_sha256`, `evidence_tool_sha256`, `fixtures_sha256` |
| `preconditions`, `residue` | Worker and custody inactive, no custody socket, zero containers, job networks, volumes and processes |
| `phases` | Catalog phases in order, each with timestamps and its probes |
| `coverage`, `not_covered` | `same_boot`, and exactly `daemon_restart_recovery`, `reboot_recovery` |
| `tolerances_version`, `started_at_unix`, `completed_at_unix` | |

A probe entry is `id`, `language` (or null), `endpoint_sha256` (or null),
`expect` (copied from the catalog), `observed` and `matched`.

Records never hold a raw address, credential or private input. Observed values
are only integers, booleans, catalog outcome names and short lowercase names
such as `lo`. An endpoint is identified only by
`sha256("dittobench-coding-native-endpoint-v1\0" + connectivity_profile_sha256 + "\0" + list + "\0" + "address:port")`,
where `list` is `trusted_tcp`, `trusted_dns` or `candidate_tcp`. The connectivity
profile digest is the native canonical sha256 already used for rollout
connectivity profiles; the profile itself never leaves the verifier.

## Approved profiles

`verify`, `review` and `check-approval` read the exact profile documents with
`--execution-profile`, `--grading-profile` and `--connectivity-profile`.

- The execution and grading profiles must be their exact Go canonical bytes
  (sorted, compact, newline), and their sha256 must equal the record's
  `inputs`. Grading test groups are `hidden` then `visible`.
- The connectivity profile's `candidate_tcp` must list exactly the router and
  the refusing proxy. The record's router and proxy hashes must be those two,
  and its trusted and DNS hashes must equal the profile's entries.

Resource limits come only from these documents, never from the record:

| Container | Profile | Scratch | nofile | Log bound |
|---|---|---|---|---|
| `harness` | execution | `ScratchLimitBytes` | 1024 | 8 MiB (sandbox `max-size=8m`) |
| `executor_authoring` | execution | Rust: minus `min(scratch/2, 128 MiB)` | 1024 | 24 KiB model-visible output |
| `executor_grading` | grading | Rust: minus `min(scratch/2, 128 MiB)` | 1024 | 24 KiB model-visible output |

Memory, CPU quota (millis) and pids come from the container's profile
`resource_policy`. A resource probe's `profile`, `limit` or `deadline_ms` must
equal that value, so related probes (for example `memory_oom.limit` and
`memory_max.profile`) agree by construction. Only `executor_grading` has a
`supervisor_timeout` probe: its deadline is the approved timeout of the grading
test group it names. Authoring command timeouts come from the private task
runtime policy, which the verifier never reads.

## Catalog

The catalog is
`services/dittobench-api/internal/codingenforcement/catalog/catalog-v1.json`.
It is the single source of truth: the Go package embeds it and the Python tool
reads the same file. It defines:

- the probe IDs for each kind and phase;
- each probe's scope: once per record, once per language image, or once per
  trusted endpoint;
- the accepted outcomes and tolerances;
- where each resource limit comes from.

The first network probe is `candidate.router.source`. Candidate identities follow
the runtime: the harness sandbox runs as `65532:65532` and the executor candidate
as `10001:10001` (required for the Rust driver). Rootless Docker maps container id
`c` to subordinate start `+ c - 1`, so each host id is checked exactly.

Expectation types:

| Type | Matched when |
|---|---|
| `outcome_in` | The observed outcome is in the accepted list. `probe_error` is never accepted, so a broken probe cannot count as a deny |
| `exact` | The observed object equals the catalog value |
| `profile_equal` | The cgroup value equals the profile value, and the profile value is at least 1 |
| `bounded` | Enforcement was seen, `limit` and `measured` are at least 1, and `measured * 1000 <= limit * permille` |
| `supervisor_timeout` | Exit 124, no live processes, and elapsed time between the deadline and the tolerance, for a named `hidden` or `visible` test group |
| `control` | `all_pass`: at least two tests, all passed. `some_fail`: at least two tests, at least one passed and one failed. `timeout`: the run timed out. Pass, wrong and hang controls per language must share one `suite_sha256`, and pass and wrong must have the same total |
| `subordinate_ids` | Host uid and gid equal subordinate start `+ id - 1` for the catalog candidate ids |

Tolerances are versioned integer constants
(`dittobench-coding-native-enforcement-tolerances-v1`), repeated in the catalog,
the Go package and the verifier. CPU usage may reach 1150 per mille of quota.
Supervisor elapsed time may reach 1100 per mille of the deadline. Memory peak,
pids, nofile, scratch and log bytes must stay within 1000 per mille of their
limits.

## Canonical encoding

A record's digest is the sha256 of its canonical bytes. The canonical form is
the one native qualification already hashes in `run.py`, `prepare.py` and the
release tools: `json.dumps(value, sort_keys=True, separators=(",", ":"))`.

- Keys are sorted by code point, with no spaces and no trailing newline.
- Every non-ASCII character is escaped as lowercase `\uXXXX`, including U+2028
  and U+2029.
- Only int64 integers are allowed. Floats, NaN and duplicate keys are refused.

This is not Platform's `coding_canonical_json_bytes` form, which keeps UTF-8 and
adds a newline. A golden record and an escaping vector are pinned byte for byte
in both the Go and Python tests.

## Tool

- `retain --store DIR --type record|host-preflight|custody-binding --input FILE`
  retains one object.
  - The store is an existing mode-0700 directory owned by the invoking user
    (root on the host). Its ancestors must be owned by root or that user and
    not writable by others, except sticky directories such as `/tmp`.
  - Objects are named by their sha256 and written exclusively (`O_EXCL`), fsynced
    and made mode 0400. Existing objects are never replaced.
  - The host preflight is kept verbatim: the exact stdout of
    `inspect-coding-native-host.py`.
- `verify --store DIR --checkout DIR --host-preflight SHA --record SHA...`
  verifies records against a post-collection preflight. It needs the profile
  documents that the records name.
- `review --store DIR --checkout DIR` with all six evidence digests assembles
  `dittobench-coding-native-evidence-review-v1`.
  - The review has per-item verification, a consistency result and
    `approval_generated: false`.
  - The six-digest map, host, release, profile `inputs`, `endpoints`,
    `endpoint_counts` and time window appear only if every item verified. A
    failed review prints no digests.
- `check-approval` checks Peyton's signed approval against a verified review.

Every JSON comparison uses exact types, so `false` never equals `0`. Integers
longer than int64 are refused before conversion. Objects are renamed into place
with `renameat2(RENAME_NOREPLACE)` where available, otherwise linked and
unlinked; a crash between those steps is recovered on the next read or write.

`--checkout` must be a reviewed checkout of the release `source_revision`. Tool
hashes always come from that checkout, never from the record. The checkout, its
files and their directories must not be links or writable by group or others.

### Verification rules

A record is refused unless all of these hold:

1. It is canonical, uses closed keys, and has the right schema and kind.
2. `coverage` is `same_boot` and `not_covered` lists exactly the two uncovered
   items.
3. Every required probe appears exactly once, in the right phase and sorted
   order, with no extra IDs. Every language image and every trusted endpoint is
   covered, and the refusing proxy is listed.
4. Each `expect` equals the catalog, each `observed` has the closed shape for
   its type, and `matched` equals the recomputed value. Every probe must match.
5. The tool, collector, catalog and fixture hashes equal the reviewed checkout.
   The preflight's own tool hashes must also match.
6. Preconditions held and the residue is empty.
7. Profile inputs, resource limits, deadlines and endpoints match the supplied
   profile documents, and controls come from one suite per language.
8. The pre-collection preflight and the post-collection `host_preflight` are
   separate objects. Both match the record's machine, boot, kernel, daemon and
   release, and they have the same nft snapshot and config digests.
9. Timestamps are in order: the pre-collection preflight (at most 15 minutes
   old), then the record start, the phases and the record end, then the
   post-collection preflight.
10. All records share one host (including `router_namespace` and subordinate
    IDs), release, tool set and shared input digests. No digest is reused.
11. Records run in the order network, resource, pre-exec, cleanup, and never
    overlap: each ends before the next starts.
12. The custody binding names the same machine and boot.
13. Records, the post-collection preflight and the custody binding all fall
    within six hours of each other, in `verify` as well as `review`.

`check-approval` then requires all of these:

- a 64-byte detached Ed25519 signature over the exact approval bytes;
- a PEM Ed25519 curator public key whose raw-key sha256 equals
  `--curator-signing-key-sha256`.

This is the algorithm and key identity that the profile approval and the
curator key loader use. The infra Python environment has neither `cryptography`
nor PyNaCl, so verification runs through OpenSSL 3 (`--openssl`, default
`/usr/bin/openssl`). OpenSSL must be a root-owned file with root-owned,
non-writable ancestors. The key, message and signature are written exclusively
into a fresh owner-only directory, and re-read unchanged after OpenSSL runs. The
tool never handles a private key. It then checks:

- The review re-verifies from the store and re-encodes to the exact supplied
  bytes.
- `native.policy` accepts the approval. `native.py` is read once, its bytes
  must hash to the approval's `binding_sha256`, and exactly those bytes are
  compiled and run. No import loader or `__pycache__` file is used.
- The approval's `runner_sha256` matches the checkout's `run.py`.
- The review's profile digests equal independently reviewed pins
  (`--execution-profile-sha256`, `--grading-profile-sha256`,
  `--connectivity-profile-sha256`), for example from the signed profile
  approval. `native.policy`'s closed approval shape cannot carry profile
  digests; the approval binds them through the record digests it names, and
  these pins make that binding visible.
- The approval's `evidence_sha256`, machine, boot, source revision, release
  manifest and image approvals equal the review.
- `issued_at_unix` is no earlier than every record end, the post-collection
  preflight and the custody binding, and at most six hours after the earliest
  record start and the custody binding.

It prints a consistency result, never an approval.

## Custody binding

Custody keeps its own digest, and this tool never opens custody paths. To bind
that digest to the machine and boot, the custodian retains a small canonical
`dittobench-coding-native-custody-binding-v1` object:
`custody_evidence_sha256`, `machine_id_sha256`, `boot_id`, `bound_at_unix`. The
approval's `private_input_custody` digest is the digest of that object.

## Peyton's decisions (2026-09-15)

- **Freshness:** one machine and one boot. Records fall within six hours of
  `issued_at`. `host_preflight` is a post-collection preflight, kept verbatim
  and taken after the last record.
- **Resources:** every language image the approval names.
- **Endpoints:** hashed endpoints plus the connectivity-profile digest only.
- **Custody:** its digest is cross-bound to machine and boot.
- **Proxy:** the refusing proxy is part of network collection and must be
  running and listed.
- **Worker:** a handshake-only positive check to trusted endpoints. Only the
  handshake outcome is recorded.
- **Tolerances:** as above, versioned.
- **Timing:** B5 lands before the release cut, and collectors come from the
  reviewed release.
- **Signature:** a detached curator signature over the final approval is
  required.
- **Workflow:** collectors are never added to `coding-hosted-operate`.

## Not covered

- Daemon restart and reboot recovery. Every record and review says so, and any
  claim of more than same-boot coverage is refused.
- This layer cannot tell whether a record is true. It checks internal
  consistency and binding only. A fabricated record made with the reviewed
  tools still needs Peyton's review.
- It cannot confirm that `--checkout` is at `source_revision`.
- The router and proxy roles are the record's own split of the two candidate
  endpoints; only the pair itself is derived from the profile.
- The harness sandbox passes `--memory` without `--memory-swap`, so Docker's
  default likely gives the harness cgroup a swap allowance equal to its memory
  limit. `harness.memory_swap_max` would then fail until the runtime sets it.
- Endpoint hashes hide raw addresses from the record, but IPv4 addresses are
  few enough to guess a hash by brute force. They are labels, not secrets.
