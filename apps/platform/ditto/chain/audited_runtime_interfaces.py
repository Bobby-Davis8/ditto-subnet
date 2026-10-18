"""Consumed-interface fingerprints of audited Subtensor runtimes.

Generated from live Finney metadata with
``ditto.chain.runtime_interface.runtime_interface`` at a block of each named
audited runtime. A runtime whose consumed interface digest appears here is
admitted by the source-emission collector without a per-release artifact
audit. See docs/source-runtime-interface.md before adding an entry.
"""

from __future__ import annotations

from ditto.chain.runtime_interface import RuntimeInterface

# digest -> (audited release it was derived from, consumed interface)
AUDITED_RUNTIME_INTERFACES: dict[str, tuple[str, RuntimeInterface]] = {
    # v473: runtime 0x7773f5c0a6d6e9ea9ff347edcc491246eec08a5cf441d964ee96f40d7fa65a08
    # at Finney block 9220000.
    "bc4ec062abdadc29a4e44d302013703cc6392a4d42997e88d710b3ff4e9c18ea": (
        "v473",
        RuntimeInterface(
            digest="bc4ec062abdadc29a4e44d302013703cc6392a4d42997e88d710b3ff4e9c18ea",
            has_epoch_consensus=False,
            entries={
                "call:SubtensorModule.commit_timelocked_mechanism_weights": (
                    "b91cbf77aa46270ce5a6106ab463fe6ea614d3af980a30eb5e0d27a8183ca878"
                ),
                "call:SubtensorModule.commit_timelocked_weights": (
                    "d38551a9ca2d86e0981c3a6f37fd265b8fa190cf39a88e28896fb9f917eda7e6"
                ),
                "event:SubtensorModule.IncentiveAlphaEmittedToMiners": (
                    "473cf636e046e3a796b359ec8f6f61c3657b579a5b3fd648a49673c1d6b6ce90"
                ),
                "event:SubtensorModule.TimelockedWeightsCommitted": (
                    "1fdb59b305f2b6566b0acb3bb819199582903515023019af8ad91d2ace59a4a0"
                ),
                "event:SubtensorModule.TimelockedWeightsRevealed": (
                    "d84c43e062581df1b71314fa750d93f1be60deb24d57c6af4221c41f67fbe034"
                ),
                "event:SubtensorModule.WeightsSet": (
                    "f92fc121ec1ca0c7cb2c09d8f5645f83d754ada0a79f55d8cb3e882949fac6d0"
                ),
                "event:System.CodeUpdated": (
                    "69efd6e7a661f9b359fbf99e782f00dc954e169beea81f5aa461ac0355fbd0bc"
                ),
                "event:System.ExtrinsicFailed": (
                    "6277caee7eac68db6c2271d0ef6dc8e266e0cf2d1bd31c1f27f9dd07ce07ad6e"
                ),
                "event:System.ExtrinsicSuccess": (
                    "723837d6bb4e343ea7035cafaef1a3a2e3eaeca1a032881507bcb801ad9b04cc"
                ),
                "storage:SubtensorModule.Active": (
                    "b2f990cb7406d7e31486ce8ee723321022e0dd706a605ee9d1aa7de9ab1a7a5d"
                ),
                "storage:SubtensorModule.CommitRevealWeightsEnabled": (
                    "1a2cbe2f87685f3b803085369edc26b75112c94bcafee8e96e0edcaf12c2e45c"
                ),
                "storage:SubtensorModule.Keys": (
                    "d94df250ac2b3791220aae792cb994c4ec4f2f836939f8517c7875dc4e87fa82"
                ),
                "storage:SubtensorModule.LastMechansimStepBlock": (
                    "a8dc1869cfd0cc74c2168d747e7d74aeaae0f8b9fe0743c15c3d466fe9d75526"
                ),
                "storage:SubtensorModule.LastUpdate": (
                    "5e40bfd66b6bce8d0b1a83cf784d4822a022967fc6afe827282f9fb53995a97c"
                ),
                "storage:SubtensorModule.MechanismCountCurrent": (
                    "16a52eb39675adf6c5b28a506b3104d9e5fce234cd79cb785b9b53cbf371a91a"
                ),
                "storage:SubtensorModule.OwnedHotkeys": (
                    "206aeda4b3eb7a62a8d1de1deda94dfa37a769944729347e5a14658a31dc0239"
                ),
                "storage:SubtensorModule.StakeWeight": (
                    "a80d79a96b89b46442b895b378e7f8b67291557baa8419c5efbae1ff2092a578"
                ),
                "storage:SubtensorModule.SubnetEpochConsensus": (
                    "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
                ),
                "storage:SubtensorModule.SubnetEpochIndex": (
                    "a8dc1869cfd0cc74c2168d747e7d74aeaae0f8b9fe0743c15c3d466fe9d75526"
                ),
                "storage:SubtensorModule.SubnetOwner": (
                    "6649622c6caec6dd208fde3933fd2ad844a5ee62236c4ba4c6a3c3ac67196080"
                ),
                "storage:SubtensorModule.SubnetOwnerHotkey": (
                    "6649622c6caec6dd208fde3933fd2ad844a5ee62236c4ba4c6a3c3ac67196080"
                ),
                "storage:SubtensorModule.TimelockedWeightCommits": (
                    "8349584c697b2d0321522b1b6a8d173bed7197052e61b8305b72259b55e012e1"
                ),
                "storage:SubtensorModule.ValidatorPermit": (
                    "b2f990cb7406d7e31486ce8ee723321022e0dd706a605ee9d1aa7de9ab1a7a5d"
                ),
                "storage:SubtensorModule.Weights": (
                    "3fae3bb6599b45c2b8bbc8b1e104b2fa35112649b41ebfed46b46f26c946c47e"
                ),
                "storage:Timestamp.Now": (
                    "e0165175e4ed1a4b634207ddff9aaf721124e73d209a6c309a10372dfb5e42b9"
                ),
            },
        ),
    ),
    # v478: runtime 0x7e722b3aa5c9192c98665a5d65fa91364bf9055e21fc90efce6d59bf4ef414e1
    # at Finney block 9280000.
    "31145be1ffd082d3d266128f56041ddae09311f507024bfef7cfc44f228d3291": (
        "v478",
        RuntimeInterface(
            digest="31145be1ffd082d3d266128f56041ddae09311f507024bfef7cfc44f228d3291",
            has_epoch_consensus=True,
            entries={
                "call:SubtensorModule.commit_timelocked_mechanism_weights": (
                    "b91cbf77aa46270ce5a6106ab463fe6ea614d3af980a30eb5e0d27a8183ca878"
                ),
                "call:SubtensorModule.commit_timelocked_weights": (
                    "d38551a9ca2d86e0981c3a6f37fd265b8fa190cf39a88e28896fb9f917eda7e6"
                ),
                "event:SubtensorModule.IncentiveAlphaEmittedToMiners": (
                    "473cf636e046e3a796b359ec8f6f61c3657b579a5b3fd648a49673c1d6b6ce90"
                ),
                "event:SubtensorModule.TimelockedWeightsCommitted": (
                    "1fdb59b305f2b6566b0acb3bb819199582903515023019af8ad91d2ace59a4a0"
                ),
                "event:SubtensorModule.TimelockedWeightsRevealed": (
                    "d84c43e062581df1b71314fa750d93f1be60deb24d57c6af4221c41f67fbe034"
                ),
                "event:SubtensorModule.WeightsSet": (
                    "f92fc121ec1ca0c7cb2c09d8f5645f83d754ada0a79f55d8cb3e882949fac6d0"
                ),
                "event:System.CodeUpdated": (
                    "69efd6e7a661f9b359fbf99e782f00dc954e169beea81f5aa461ac0355fbd0bc"
                ),
                "event:System.ExtrinsicFailed": (
                    "6277caee7eac68db6c2271d0ef6dc8e266e0cf2d1bd31c1f27f9dd07ce07ad6e"
                ),
                "event:System.ExtrinsicSuccess": (
                    "723837d6bb4e343ea7035cafaef1a3a2e3eaeca1a032881507bcb801ad9b04cc"
                ),
                "storage:SubtensorModule.Active": (
                    "b2f990cb7406d7e31486ce8ee723321022e0dd706a605ee9d1aa7de9ab1a7a5d"
                ),
                "storage:SubtensorModule.CommitRevealWeightsEnabled": (
                    "1a2cbe2f87685f3b803085369edc26b75112c94bcafee8e96e0edcaf12c2e45c"
                ),
                "storage:SubtensorModule.Keys": (
                    "d94df250ac2b3791220aae792cb994c4ec4f2f836939f8517c7875dc4e87fa82"
                ),
                "storage:SubtensorModule.LastMechansimStepBlock": (
                    "a8dc1869cfd0cc74c2168d747e7d74aeaae0f8b9fe0743c15c3d466fe9d75526"
                ),
                "storage:SubtensorModule.LastUpdate": (
                    "5e40bfd66b6bce8d0b1a83cf784d4822a022967fc6afe827282f9fb53995a97c"
                ),
                "storage:SubtensorModule.MechanismCountCurrent": (
                    "16a52eb39675adf6c5b28a506b3104d9e5fce234cd79cb785b9b53cbf371a91a"
                ),
                "storage:SubtensorModule.OwnedHotkeys": (
                    "206aeda4b3eb7a62a8d1de1deda94dfa37a769944729347e5a14658a31dc0239"
                ),
                "storage:SubtensorModule.StakeWeight": (
                    "a80d79a96b89b46442b895b378e7f8b67291557baa8419c5efbae1ff2092a578"
                ),
                "storage:SubtensorModule.SubnetEpochConsensus": (
                    "d2693a1e490d0349a41327db34137c7e9b4d4b34ea91ee1048efe85c368a3343"
                ),
                "storage:SubtensorModule.SubnetEpochIndex": (
                    "a8dc1869cfd0cc74c2168d747e7d74aeaae0f8b9fe0743c15c3d466fe9d75526"
                ),
                "storage:SubtensorModule.SubnetOwner": (
                    "6649622c6caec6dd208fde3933fd2ad844a5ee62236c4ba4c6a3c3ac67196080"
                ),
                "storage:SubtensorModule.SubnetOwnerHotkey": (
                    "6649622c6caec6dd208fde3933fd2ad844a5ee62236c4ba4c6a3c3ac67196080"
                ),
                "storage:SubtensorModule.TimelockedWeightCommits": (
                    "8349584c697b2d0321522b1b6a8d173bed7197052e61b8305b72259b55e012e1"
                ),
                "storage:SubtensorModule.ValidatorPermit": (
                    "b2f990cb7406d7e31486ce8ee723321022e0dd706a605ee9d1aa7de9ab1a7a5d"
                ),
                "storage:SubtensorModule.Weights": (
                    "3fae3bb6599b45c2b8bbc8b1e104b2fa35112649b41ebfed46b46f26c946c47e"
                ),
                "storage:Timestamp.Now": (
                    "e0165175e4ed1a4b634207ddff9aaf721124e73d209a6c309a10372dfb5e42b9"
                ),
            },
        ),
    ),
}
