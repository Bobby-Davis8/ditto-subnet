# Source attribution across runtime upgrades

The source-emission collector proves that a king earned completed winner
emissions before its source is released. It used to read a block only when the
runtime's WASM `:code` hash matched a hand-audited release artifact
(`AUDITED_RUNTIME_CODE_HASHES`). Every Finney upgrade therefore froze source
release until someone audited the new artifact: v467 (#2231), v468 (#2703),
v475 (#2941), and v472, which stalled the cursor at block 9197055 from
2026-10-02 because v472 was an untagged deploy with no release to audit
(its artifact is `0x43bc67be…`, not v471's as `source-runtime-v468.md` assumed).
v479 is untagged too.

## Interface admission

`admit_runtime` (`ditto/chain/source_emission_verifier.py`) still admits the
audited artifacts directly. Any other runtime is admitted when its **consumed
interface** matches an audited runtime's:

- `ditto/chain/runtime_interface.py` reads the runtime's own metadata
  (`init_runtime(block_hash=…)`) and hashes the SCALE shape of exactly the
  storage items, event variants and calls the verifier and the payout receipt
  reader decode (`CONSUMED_STORAGE`, `CONSUMED_EVENTS`, `CONSUMED_CALLS`).
  Storage modifiers, hashers and defaults are included; docs and type names
  are not. `System.Events` is not hashed whole, since it embeds every
  pallet's events; the consumed variants are hashed by name instead.
- `ditto/chain/audited_runtime_interfaces.py` pins the audited digests,
  derived from live metadata of audited v473 (no epoch consensus) and v478
  (with `SubnetEpochConsensus`).
- A runtime that has `SubnetEpochConsensus` is read only while it is `Yuma`,
  the same rule the v475 audit set for its artifacts.
- Admissions are cached per `:code` hash, which fixes the metadata. A refused
  or unreadable interface is not cached, so a transient metadata failure
  retries as a `ChainConnectionError`.

Measured on Finney 2026-10-17, the consumed interface is identical for every
runtime from v448 to v479 except at v475, where `SubnetEpochConsensus` was
added. That is the only upgrade in that window that needed a human audit.

## When the collector stops

`collector_blocked_reason` on Backroom `get_source_release_policy` names the
runtime and the consumed entries that moved, for example:

```text
runtime fingerprint is not audited: 0x… (interface changed since v478:
event:SubtensorModule.IncentiveAlphaEmittedToMiners)
```

Audit only the named entries against the reader's assumptions. If the reader
still decodes them correctly, regenerate the pinned entry for that runtime:

```python
from ditto.chain.runtime_interface import runtime_interface
interface = runtime_interface((await substrate.init_runtime(block_hash=h)).metadata)
```

Add it to `AUDITED_RUNTIME_INTERFACES` with the release it came from. If the
reader's logic must change, change it and the consumed lists together.

## Residual risk

An upgrade that changes payout semantics without changing any consumed type
(for example epoch math inside the WASM) is admitted. The payout gate still
requires strictly more than 2/3 of effective stake backing the exact
submission's committed vector, and the backed hotkey must receive the maximal
miner payout in the chain's own `IncentiveAlphaEmittedToMiners` event, so a
semantic drift that changes who is paid fails the gate rather than releasing
source.

## Decoder isolation

async-substrate-interface 2.0.4 lost the type registry of an older runtime
once a session had decoded a newer one: re-decoding a v469 block after a v472
block failed with `'scale_info::N' object has no attribute 'index'`. That
error, raised by the payout resolver in the scan's failure path, also replaced
the scan's own "not audited" refusal in `collector_blocked_reason`. 2.2.1 does
not reproduce it, and the collector now logs a resolver failure instead of
letting it mask the scan error.
