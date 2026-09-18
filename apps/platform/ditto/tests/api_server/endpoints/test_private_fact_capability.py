"""Fact-world leases require an explicit identity-verified scorer capability."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ditto.api_server.endpoints.validator import (
    _assert_private_dataset_capability,
    _private_dataset_mode,
)
from ditto.db.queries.private_benchmark_datasets import (
    PrivateDatasetIdentity,
    pin_private_dataset,
)
from ditto.tests.db.test_private_fact_receipts import encode, fact_candidate


def heartbeat(fact=False, status="fresh_verified"):
    return SimpleNamespace(
        capabilities={
            "scorer_benchmarks": {
                "status": status,
                "private_datasets": True,
                "fact_world_datasets": fact,
            }
        }
    )


def test_missing_fact_capability_is_not_implied_by_legacy_support():
    _assert_private_dataset_capability(heartbeat(), "legacy-rewrite")
    _assert_private_dataset_capability(heartbeat(True), "fact-world-v1")
    for value in (None, heartbeat(), heartbeat(True, "stale"), heartbeat("true")):
        with pytest.raises(HTTPException, match="cannot execute"):
            _assert_private_dataset_capability(value, "fact-world-v1")


async def test_pinned_fact_dataset_advertises_exact_mode_only_to_capable_validator(
    session_maker,
):
    key = PrivateDatasetIdentity(
        str(uuid4()), 42, "small", "a" * 64, generation_mode="fact-world-v1"
    )
    values = encode(*fact_candidate(key))
    values["validation_receipt_bytes"] = values.pop("receipt_bytes")
    async with session_maker() as session, session.begin():
        result = await pin_private_dataset(session, identity=key, **values)
    async with session_maker() as session:
        with pytest.raises(HTTPException):
            await _private_dataset_mode(session, result.dataset_sha256, heartbeat())
        assert (
            await _private_dataset_mode(session, result.dataset_sha256, heartbeat(True))
            == "platform-fact-world-v1"
        )
