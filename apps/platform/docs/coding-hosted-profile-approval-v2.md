# Hosted-v2 profile approval document

`python -I -m ditto.coding_hosted_profile_approval` prepares and verifies the one
canonical approval document for the task-bound half of a hosted-v2 canary
bundle: the execution and grading profiles of one catalog index
(`services/dittobench-api/docs/coding-hosted-profiles-v2.md`). It is operator
tooling. Nothing runs it from Platform startup, CI, a worker, a validator or a
miner, and it changes no release, assignment, host or reward state.

Run it only on an owner-controlled machine. It never reads, accepts or writes a
private key.

## Approval model

The document has **no approval field**. `build` writes an unsigned draft and
prints `approved=false`. A draft stays unapproved however it is copied or
edited, because approval is not a value inside it.

A document is approved only by a detached curator signature over its exact
bytes. `verify` accepts only if that signature validates under the pinned
curator key. It uses the existing offline curator key and the signature format
of the private-v2 publication signing message
(`ditto/api_server/coding_private_v2_publication.py`):

- **Signed bytes:** `coding_canonical_json_bytes`, which is sorted keys, compact
  separators, UTF-8 and one trailing newline. There is no extra prefix or
  pre-hash. The closed `schema` value separates domains, so a publication
  message signed by the same key is never a valid approval document.
- **Signature:** a raw 64-byte Ed25519 signature in its own file.
- **Key identity:** `curator_signing_key_sha256` is the SHA-256 of the raw
  32-byte Ed25519 public key, as computed by `load_curator_signing_public_key`.
  It is inside the signed bytes and must also equal a pin supplied separately.
  For the registered `coding-private-v2-r2` curator key that pin is
  `aa7e1d820f2cfea52932c21629c0f51d3b1f9c2362b218e7a8759e32fd8b2220`.

## Build the draft

```text
python -I -m ditto.coding_hosted_profile_approval build \
  --request /ABS/PRIVATE/approval-request.json \
  --profiles /ABS/PRIVATE/PROFILE-HELPER-OUTPUT \
  --payload-authority /ABS/PRIVATE/payload/payload-authority.json \
  --registration /ABS/PRIVATE/private-v2-registration.json \
  --release-index /ABS/RELEASE/release.json \
  --native-approval /ABS/PRIVATE/native-controls-approval.json \
  --native-summary /ABS/PRIVATE/NATIVE-MATRIX/summary.json \
  --native-provenance /ABS/PRIVATE/NATIVE-MATRIX/provenance.json \
  --curator-public-key /ABS/PRIVATE/curator-signing-public.pem \
  --output /ABS/PRIVATE/approval-document.json
```

`--profiles` must be the exact helper output directory: `execution-profile.json`,
`grading-profile.json` and `receipt.json`, with no other files. The closed
`dittobench-coding-hosted-profile-approval-request-v1` request rejects unknown
fields. It carries only reviewed choices and pins that come from independent
channels:

| Field | Source |
|---|---|
| `catalog_index`, `language` | The reviewed task and its runtime language |
| `source_revision` | The exact 40-hex revision of the release set and native controls |
| `registration_sha256` | The live registry row for the private-v2 release |
| `release_manifest_sha256` | The independently retained `release.json` SHA |
| `native_controls_approval_sha256` | The native controls approval the host consumed |
| `grader_contract_sha256` | `HostedGraderContractSHA256()` compiled at `source_revision` |
| `curator_signing_key_sha256` | The pinned curator key identity |
| `shadow_only`, `weight_eligible` | Explicit `true`, `false` |

## Builder checks

Every digest in the document is recomputed from input bytes. Receipt claims are
only compared with those recomputations. Any failure prints a fixed reason,
exits 70 and writes nothing.

1. **Profiles.** The receipt is canonical, has exactly the helper's fields and
   says `launch_checks_passed=true`, `approved=false`, `shadow_only=true` and
   `weight_eligible=false`. Both profiles are canonical and hash to the receipt's
   digests. The grading profile has exactly the hosted-v2 keys, so a
   `test_manifest_sha256` is rejected. Image, grader contract, grader bundle and
   patch limit agree across profiles and receipt, and the grader contract equals
   the pin.
2. **Private-v2 identity.** The payload authority hashes to the receipt's file
   digest, and its self-digest recomputes to the receipt's `payload_sha256`.
   Exactly one task has the reviewed catalog index. Its task version,
   commitment and grader bundle match the receipt. The registration validates
   through `CodingPrivateV2RegistrationAuthority`, matches its pin, and binds
   the same corpus release, private release, payload and catalog.
3. **Release set.** `release.json` matches its pin and its writer's exact
   encoding. Readiness fields are unedited and `source_revision` equals the
   reviewed revision. The reviewed language's `image_ref` digest equals the
   profiles' `image_digest`.
4. **Native controls.** The approval matches its pin and has the
   `dittobench-coding-native-controls-approval-v2` shape. Its revision, release
   set and four images equal the release index. `provenance.json` and
   `summary.json` are the native-bound outputs of `qualification/run.py` at the
   same revision, carry the same `native_control_authority` for that approval,
   and match its plan, helper and runner. The profile image is in the
   inspected repository digests. The summary must report native and private
   controls passed, zero failed controls, equal repeats, complete controls, and
   every readiness flag still false.

On success `build` exclusively creates the mode-`0600` document in a protected
directory and prints only `approved=false` and `document_sha256`.

## Document fields

`dittobench-coding-hosted-profile-approval-v1` is a closed flat object with
these fields:
- `schema`, `coding_contract_version=2`, `catalog_index` and `language`;
- `source_revision`;
- task identity: `task_version_id`, `task_commitment_sha256`, `corpus_release_id`,
  `registration_sha256`, `private_release_sha256`, `payload_sha256` and
  `payload_authority_file_sha256`;
- profiles: `profile_receipt_sha256`, `execution_profile_sha256`,
  `grading_profile_sha256`, `grader_contract_sha256`, `grader_bundle_sha256`
  and `image_digest`;
- release set: `release_manifest_sha256` and `image_approval_sha256`;
- compatibility evidence: `native_controls_approval_sha256`,
  `native_controls_plan_sha256`, `native_controls_provenance_sha256` and
  `native_controls_summary_sha256`;
- `curator_signing_key_sha256`, `shadow_only=true` and `weight_eligible=false`.

## Offline signing

Review the digests. Then sign the exact document bytes on the offline signing
machine with the curator key, using the same step as the private-v2 publication
message:

```text
openssl pkeyutl -sign -rawin \
  -inkey <offline curator private key> \
  -in approval-document.json \
  -out approval-signature.bin
wc -c approval-signature.bin   # must be exactly 64
```

Ed25519 signatures are deterministic, so any conforming signer produces the
same 64 bytes. Do not re-encode, reformat or append to the document.

## Verify

```text
python -I -m ditto.coding_hosted_profile_approval verify \
  --document /ABS/PRIVATE/approval-document.json \
  --signature /ABS/PRIVATE/approval-signature.bin \
  --curator-public-key /ABS/PRIVATE/curator-signing-public.pem \
  --curator-signing-key-sha256 aa7e1d820f2cfea52932c21629c0f51d3b1f9c2362b218e7a8759e32fd8b2220
```

`verify` rejects (exit 70) unless all of these hold:
- the document is exact canonical bytes in the closed schema, with
  `shadow_only=true` and `weight_eligible=false`;
- the signature file is exactly 64 bytes;
- the public key hashes to the pin and to the document's
  `curator_signing_key_sha256`;
- the signature verifies over the exact bytes.

A missing, empty or wrong-length signature is rejected, so an unsigned document
is never accepted. On success it prints only the document, signature and bound
digests.

## Not established here

- **Driver compatibility of this task.** The native summary proves that the
  whole matrix plan passed on the approved images. It does not show that the
  plan's cases for this task's group use the same driver arguments and expected
  counts as the grading profile. Reviewers must compare those privately.
- **Grader contract provenance.** The builder checks the grader contract against
  the reviewed pin. It cannot compile Go, so the pin must be taken from the
  helper built at `source_revision`.
- **Time-bound inputs.** The inference policy and budget profile are separate,
  re-issued at least daily and not covered by this signature.
- **Activation.** A valid signature does not install, import, select, assign,
  weight or reward anything. Hosted v2 still binds no test manifest, which
  weighted activation requires.
- **Revocation.** The document has no expiry. Superseding an approval means
  signing a new document and retiring the old digest out of band.
