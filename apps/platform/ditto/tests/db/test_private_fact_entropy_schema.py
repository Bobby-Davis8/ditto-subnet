"""Real-Postgres entropy format and identity fences, not rollout approval."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import DBAPIError

from ditto.db.models import PrivateBenchmarkPreparation
from ditto.db.queries.private_benchmark_datasets import PrivateDatasetIdentity
from ditto.db.queries.private_benchmark_preparations import (
    claim_private_preparation,
    request_private_preparation,
)


def values(mode, entropy):
    identifier = uuid4()
    return {
        "preparation_id": identifier,
        "identity_sha256": hashlib.sha256(identifier.bytes).hexdigest(),
        "scope": str(identifier),
        "bench_version": 13,
        "seed": 42,
        "run_size": "small",
        "transform_profile_sha256": "a" * 64,
        "generation_mode": mode,
        "surface_salt": entropy,
        "state": "pending",
        "attempts": 0,
    }


def entropy(world=731, presentation=92713):
    return world.to_bytes(8, "big") + presentation.to_bytes(8, "big")


async def test_fact_reservation_is_idempotent_and_distinct_from_legacy(session_maker):
    profile = hashlib.sha256(uuid4().bytes).hexdigest()
    key = PrivateDatasetIdentity(
        str(uuid4()), 42, "small", profile, generation_mode="fact-world-v1"
    )
    assert key.digest() != replace(key, generation_mode="legacy-rewrite").digest()
    async with session_maker() as session, session.begin():
        first = await request_private_preparation(session, identity=key)
    async with session_maker() as session, session.begin():
        assert await request_private_preparation(session, identity=key) == first
        claim = await claim_private_preparation(
            session, transform_profile_sha256=profile, now=datetime.now(UTC)
        )
        assert claim is not None and claim.identity == key
        assert claim.preparation_id == first and len(claim.surface_salt) == 16
        reserved = claim.surface_salt
    async with session_maker() as session:
        row = await session.get(PrivateBenchmarkPreparation, first)
        assert row.generation_mode == "fact-world-v1" and row.surface_salt == reserved


@pytest.mark.parametrize(
    "mode,raw",
    [
        ("fact-world-v1", entropy()),
        ("fact-world-v1", entropy(2**63 + 731)),
        ("legacy-rewrite", (731).to_bytes(8, "big")),
    ],
)
async def test_generation_entropy_mode_is_immutable(session_maker, mode, raw):
    row = values(mode, raw)
    async with session_maker() as session, session.begin():
        await session.execute(insert(PrivateBenchmarkPreparation).values(**row))
    async with session_maker() as session, session.begin():
        # Change BOTH mode and correctly-sized entropy. Shape validation alone
        # would permit this, but the reserved identity must be immutable.
        other_mode = "legacy-rewrite" if mode == "fact-world-v1" else "fact-world-v1"
        other_entropy = (
            (731).to_bytes(8, "big") if mode == "fact-world-v1" else entropy()
        )
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(
                update(PrivateBenchmarkPreparation)
                .where(
                    PrivateBenchmarkPreparation.preparation_id == row["preparation_id"]
                )
                .values(generation_mode=other_mode, surface_salt=other_entropy)
            )


@pytest.mark.parametrize(
    "mode,raw",
    [
        ("unknown", entropy()),
        ("fact-world-v1", bytes(8)),
        ("fact-world-v1", entropy(0)),
        ("fact-world-v1", entropy(42)),
        ("fact-world-v1", entropy(presentation=0)),
        ("fact-world-v1", entropy(presentation=731)),
        ("fact-world-v1", entropy() + b"x"),
        ("legacy-rewrite", entropy()),
    ],
)
async def test_generation_entropy_invalid_shapes_rejected(session_maker, mode, raw):
    async with session_maker() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(
                insert(PrivateBenchmarkPreparation).values(**values(mode, raw))
            )
