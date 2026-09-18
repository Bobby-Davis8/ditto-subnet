"""Platform receipt boundary tests; synthetic fixtures are not semantic proof."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ditto.db.queries.private_benchmark_datasets import (
    FACT_GENERATION_REVISION,
    PrivateDatasetError,
    PrivateDatasetIdentity,
    _validate,
    find_private_dataset,
)
from ditto.db.queries.private_benchmark_preparations import (
    claim_private_preparation,
    finish_private_preparation,
    request_private_preparation,
)


def fact_candidate(key, world=731, presentation=92713):
    manifest = {
        "revision": FACT_GENERATION_REVISION,
        "seed": key.seed,
        "world_seed": world,
        "presentation_seed": presentation,
        "run_size": key.run_size,
        "profile_sha256": key.transform_profile_sha256,
        "qualified": False,
    }
    dataset = {
        "seed": key.seed,
        "bench_version": 13,
        "fact_generation": {
            "revision": FACT_GENERATION_REVISION,
            "world_seed": world,
            "presentation_seed": presentation,
            "events": [
                {
                    "phase": "document_plan",
                    "request_sha256": "a" * 64,
                    "plan_sha256": "b" * 64,
                },
                # Check hashes bound text, not the author's token plan.
                {
                    "phase": "document_check",
                    "request_sha256": "a" * 64,
                    "plan_sha256": "c" * 64,
                },
            ],
        },
    }
    receipt = {
        "schema": "private-fact-generation-validation-v1",
        "accepted": True,
        "qualified": False,
        "replay_verified": True,
        "generation_revision": FACT_GENERATION_REVISION,
        "transform_profile_sha256": key.transform_profile_sha256,
        "run_size": key.run_size,
        "render_event_count": 2,
    }
    return manifest, dataset, receipt


def encode(manifest, dataset, receipt):
    base = json.dumps(manifest).encode()
    data = json.dumps(dataset).encode()
    receipt = {
        **receipt,
        "generation_sha256": hashlib.sha256(base).hexdigest(),
        "dataset_sha256": hashlib.sha256(data).hexdigest(),
    }
    return {
        "base_bytes": base,
        "dataset_bytes": data,
        "receipt_bytes": json.dumps(receipt).encode(),
    }


def key():
    return PrivateDatasetIdentity(
        str(uuid4()),
        42,
        "small",
        hashlib.sha256(uuid4().bytes).hexdigest(),
        generation_mode="fact-world-v1",
    )


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("manifest", "seed", True),
        ("manifest", "seed", 43),
        ("manifest", "world_seed", 42),
        ("manifest", "presentation_seed", 0),
        ("manifest", "profile_sha256", "f" * 64),
        ("manifest", "qualified", True),
        ("manifest", "run_size", "full"),
        ("dataset", "seed", True),
        ("dataset", "surface_salt", 1),
        ("dataset", "surface_salt", False),
        ("dataset", "fact_generation", None),
        ("receipt", "schema", "v13-fact-candidate-v2"),
        ("receipt", "accepted", False),
        ("receipt", "qualified", True),
        ("receipt", "replay_verified", False),
        ("receipt", "render_event_count", 4),
        ("receipt", "generation_revision", "old"),
    ],
)
def test_fact_receipt_rejects_mismatched_contract_even_with_rehashed_bytes(
    target, field, value
):
    identity = key()
    manifest, dataset, receipt = fact_candidate(identity)
    {"manifest": manifest, "dataset": dataset, "receipt": receipt}[target][field] = (
        value
    )
    with pytest.raises(PrivateDatasetError):
        _validate(identity, **encode(manifest, dataset, receipt))


def test_fact_receipt_cannot_be_used_as_legacy_or_without_complete_checks():
    identity = key()
    source = fact_candidate(identity)
    _validate(identity, **encode(*source))
    with pytest.raises(PrivateDatasetError):
        _validate(
            replace(identity, generation_mode="legacy-rewrite"), **encode(*source)
        )
    for change in ("truncate", "phase", "request", "malformed"):
        manifest, dataset, receipt = fact_candidate(identity)
        events = dataset["fact_generation"]["events"]
        if change == "truncate":
            events.pop()
        elif change == "phase":
            events[1]["phase"] = "document_plan"
        elif change == "request":
            events[1]["request_sha256"] = "d" * 64
        else:
            events[0]["phase"] = []
        with pytest.raises(PrivateDatasetError):
            _validate(identity, **encode(manifest, dataset, receipt))


async def test_fact_preparation_binds_reserved_entropy_and_pins_unqualified_bytes(
    session_maker,
):
    identity = key()
    async with session_maker() as session, session.begin():
        await request_private_preparation(session, identity=identity)
    async with session_maker() as session, session.begin():
        claim = await claim_private_preparation(
            session,
            transform_profile_sha256=identity.transform_profile_sha256,
            now=datetime.now(UTC),
        )
    world = int.from_bytes(claim.surface_salt[:8], "big", signed=True)
    presentation = int.from_bytes(claim.surface_salt[8:], "big", signed=True)
    for actual, succeeds in ((world ^ 1, False), (world, True)):
        values = encode(*fact_candidate(identity, actual, presentation))
        values["validation_receipt_bytes"] = values.pop("receipt_bytes")
        async with session_maker() as session, session.begin():
            if succeeds:
                result = await finish_private_preparation(
                    session, claim=claim, now=datetime.now(UTC), **values
                )
            else:
                with pytest.raises(PrivateDatasetError, match="entropy mismatch"):
                    await finish_private_preparation(
                        session, claim=claim, now=datetime.now(UTC), **values
                    )
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=identity) == result
        assert json.loads(result.validation_receipt_bytes)["qualified"] is False
