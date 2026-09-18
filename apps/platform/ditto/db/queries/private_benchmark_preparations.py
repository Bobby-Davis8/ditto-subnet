"""Short transactions around a durable private-generation queue.

No inference runs inside these transactions. Commit a claim, produce outside
the database, then fence completion by its token and deadline. Queue rows confer
neither qualification nor authority to issue benchmark work.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import PrivateBenchmarkPreparation
from ditto.db.queries.private_benchmark_datasets import (
    MAX_ARTIFACT_BYTES,
    PrivateDatasetBytes,
    PrivateDatasetError,
    PrivateDatasetIdentity,
    _object,
    find_private_dataset,
    pin_private_dataset,
)

MAX_ATTEMPTS = 3
CLAIM_TTL = timedelta(hours=3)
_FAILURE_CODES = {"producer_rejected", "producer_unavailable", "invalid_output"}


@dataclass(frozen=True)
class PreparationClaim:
    preparation_id: UUID
    token: UUID
    identity: PrivateDatasetIdentity
    deadline: datetime
    attempt: int
    surface_salt: bytes = field(repr=False)


def _identity(row: PrivateBenchmarkPreparation) -> PrivateDatasetIdentity:
    identity = PrivateDatasetIdentity(
        scope=row.scope,
        seed=row.seed,
        run_size=row.run_size,
        transform_profile_sha256=row.transform_profile_sha256,
        bench_version=row.bench_version,
        generation_mode=row.generation_mode,
    )
    if row.identity_sha256 != identity.digest():
        raise PrivateDatasetError("private preparation identity mismatch")
    return identity


def _time(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise PrivateDatasetError("private preparation requires an aware clock")


async def request_private_preparation(
    session: AsyncSession, *, identity: PrivateDatasetIdentity
) -> UUID:
    """Idempotently reserve entropy once. Caller must commit before returning."""
    digest = identity.digest()
    width = 16 if identity.generation_mode == "fact-world-v1" else 8
    salt = bytes(width)
    for _ in range(2):
        salt = secrets.token_bytes(width)
        if _valid_entropy(identity, salt):
            break
    if not _valid_entropy(identity, salt):
        raise PrivateDatasetError("private preparation entropy unavailable")
    try:
        await session.execute(
            insert(PrivateBenchmarkPreparation)
            .values(
                preparation_id=uuid4(),
                generation_mode=identity.generation_mode,
                identity_sha256=digest,
                scope=identity.scope,
                bench_version=identity.bench_version,
                seed=identity.seed,
                run_size=identity.run_size,
                transform_profile_sha256=identity.transform_profile_sha256,
                surface_salt=salt,
                state="pending",
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["identity_sha256"])
        )
        preparation_id = await session.scalar(
            select(PrivateBenchmarkPreparation.preparation_id).where(
                PrivateBenchmarkPreparation.identity_sha256 == digest
            )
        )
    except SQLAlchemyError:
        raise PrivateDatasetError("private preparation storage unavailable") from None
    if preparation_id is None:
        raise PrivateDatasetError("private preparation reservation unavailable")
    return preparation_id


def _valid_entropy(identity: PrivateDatasetIdentity, raw: bytes) -> bool:
    if identity.generation_mode == "legacy-rewrite":
        return len(raw) == 8 and raw != bytes(8)
    return (
        identity.generation_mode == "fact-world-v1"
        and len(raw) == 16
        and raw[:8] not in {bytes(8), identity.seed.to_bytes(8, "big")}
        and raw[8:] != bytes(8)
        and raw[:8] != raw[8:]
    )


async def claim_private_preparation(
    session: AsyncSession, *, transform_profile_sha256: str, now: datetime
) -> PreparationClaim | None:
    """Claim one matching profile with SKIP LOCKED and a bounded recovery budget."""
    _time(now)
    # Exhausted crash recovery is visible, not an endlessly renewable bill.
    await session.execute(
        update(PrivateBenchmarkPreparation)
        .where(
            PrivateBenchmarkPreparation.transform_profile_sha256
            == transform_profile_sha256,
            PrivateBenchmarkPreparation.state == "running",
            PrivateBenchmarkPreparation.claim_until <= now,
            PrivateBenchmarkPreparation.attempts >= MAX_ATTEMPTS,
        )
        .values(state="failed", claim_until=None, failure_code="attempts_exhausted")
    )
    row = await session.scalar(
        select(PrivateBenchmarkPreparation)
        .where(
            PrivateBenchmarkPreparation.transform_profile_sha256
            == transform_profile_sha256,
            PrivateBenchmarkPreparation.attempts < MAX_ATTEMPTS,
            or_(
                PrivateBenchmarkPreparation.state == "pending",
                and_(
                    PrivateBenchmarkPreparation.state == "running",
                    PrivateBenchmarkPreparation.claim_until <= now,
                ),
            ),
        )
        .order_by(
            PrivateBenchmarkPreparation.created_at,
            PrivateBenchmarkPreparation.preparation_id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    identity = _identity(row)
    existing = await find_private_dataset(session, identity=identity)
    if existing is not None:
        row.state, row.dataset_id, row.claim_until = "ready", existing.dataset_id, None
        await session.flush()
        return None
    row.state = "running"
    row.attempts += 1
    row.claim_token = uuid4()
    row.claim_until = now + CLAIM_TTL
    row.failure_code = None
    await session.flush()
    return PreparationClaim(
        row.preparation_id,
        row.claim_token,
        identity,
        row.claim_until,
        row.attempts,
        row.surface_salt,
    )


async def _owned_claim(
    session: AsyncSession, claim: PreparationClaim, now: datetime
) -> PrivateBenchmarkPreparation:
    _time(now)
    row = await session.get(
        PrivateBenchmarkPreparation,
        claim.preparation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if (
        row is None
        or row.claim_token != claim.token
        or _identity(row) != claim.identity
        or row.surface_salt != claim.surface_salt
        or row.state != "running"
        or row.claim_until is None
        or row.claim_until <= now
    ):
        raise PrivateDatasetError("private preparation claim is stale")
    return row


async def finish_private_preparation(
    session: AsyncSession,
    *,
    claim: PreparationClaim,
    now: datetime,
    base_bytes: bytes,
    dataset_bytes: bytes,
    validation_receipt_bytes: bytes,
) -> PrivateDatasetBytes:
    """Atomic artifact pin plus ready state. Stale writers cannot publish."""
    row = await _owned_claim(session, claim, now)
    base = _object(base_bytes, MAX_ARTIFACT_BYTES)
    if claim.identity.generation_mode == "fact-world-v1":
        entropy_matches = (
            _valid_entropy(claim.identity, claim.surface_salt)
            and type(base.get("world_seed")) is int
            and type(base.get("presentation_seed")) is int
            and base["world_seed"]
            == int.from_bytes(claim.surface_salt[:8], "big", signed=True)
            and base["presentation_seed"]
            == int.from_bytes(claim.surface_salt[8:], "big", signed=True)
        )
    else:
        entropy_matches = base.get("surface_salt") == int.from_bytes(
            claim.surface_salt, "big"
        )
    if not entropy_matches:
        raise PrivateDatasetError("private preparation entropy mismatch")
    result = await pin_private_dataset(
        session,
        identity=claim.identity,
        base_bytes=base_bytes,
        dataset_bytes=dataset_bytes,
        validation_receipt_bytes=validation_receipt_bytes,
    )
    row.state, row.dataset_id, row.claim_until = "ready", result.dataset_id, None
    await session.flush()
    return result


async def fail_private_preparation(
    session: AsyncSession,
    *,
    claim: PreparationClaim,
    now: datetime,
    code: str,
) -> None:
    if code not in _FAILURE_CODES:
        raise PrivateDatasetError("private preparation failure code invalid")
    row = await _owned_claim(session, claim, now)
    row.state, row.failure_code, row.claim_until = "failed", code, None
    await session.flush()
