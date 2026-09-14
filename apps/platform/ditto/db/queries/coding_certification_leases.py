"""Issue, claim, and abort shadow coding-certification leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseStatus,
)
from ditto.api_server.coding_certification_canary import (
    CodingCertificationCanaryUnavailableError,
    public_certification_canary,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries.coding_certification_allowlist import (
    require_coding_certification_allowlisted,
)
from ditto.db.queries.core_qualification import (
    latest_complete_core_qualification_observation,
    latest_core_qualification_policy,
    lock_core_qualification_bench,
)

_LEASE_TTL = timedelta(minutes=20)
_INFLIGHT = (
    CodingCertificationLeaseStatus.ISSUED.value,
    CodingCertificationLeaseStatus.CLAIMED.value,
)
# A claimed lease that passes its deadline now releases its identity, so bound
# how often one exact identity can be re-run (and re-granted Platform-paid
# inference) inside a rolling window.
MAX_CLAIMED_ATTEMPTS_PER_IDENTITY = 3
CLAIMED_ATTEMPT_WINDOW = timedelta(hours=24)


class CodingCertificationLeaseNotAvailableError(RuntimeError):
    """The agent is not currently eligible for a certification lease."""


class CodingCertificationLeaseConflictError(RuntimeError):
    """An in-flight lease already exists, or the requested transition is illegal."""


class CodingCertificationLeaseUnavailableError(RuntimeError):
    """The public canary identity or database clock is unavailable."""


@dataclass(frozen=True)
class CodingCertificationLeaseResult:
    row: CodingCertificationLease
    authority: CodingCertificationLeaseAuthority
    idempotent: bool


@dataclass(frozen=True)
class CodingCertificationLeaseAuditRow:
    lease: CodingCertificationLease
    inference_grant_status: str | None
    receipt_status: str | None


@dataclass(frozen=True)
class CodingCertificationHarnessAuthority:
    agent_id: UUID
    lease_id: UUID
    deadline: datetime
    bench_version: int
    agent_artifact_sha256: str
    screened_image_sha256: str
    screened_image_size_bytes: int
    screened_image_id: str
    screened_image_ref: str
    screened_image_upload_id: UUID
    screening_policy_version: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _database_now(session: AsyncSession) -> datetime:
    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):  # pragma: no cover - DB invariant
        raise CodingCertificationLeaseUnavailableError(
            "database clock did not return a timestamp"
        )
    return _aware(now)


def _screened_image_is_complete(agent: Agent) -> bool:
    return (
        agent.screened_image_sha256 is not None
        and len(agent.screened_image_sha256) == 64
        and agent.screened_image_size_bytes is not None
        and agent.screened_image_size_bytes > 0
        and agent.screened_image_id is not None
        and agent.screened_image_ref is not None
        and agent.screened_image_upload_id is not None
        and agent.screened_image_verified_at is not None
    )


def authority_from_row(
    row: CodingCertificationLease,
) -> CodingCertificationLeaseAuthority:
    return CodingCertificationLeaseAuthority.model_validate(row.authority)


def result_from_row(
    row: CodingCertificationLease, *, idempotent: bool
) -> CodingCertificationLeaseResult:
    return CodingCertificationLeaseResult(
        row=row,
        authority=authority_from_row(row),
        idempotent=idempotent,
    )


async def _expire_due_leases(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    coding_contract_version: int,
    now: datetime,
) -> None:
    rows = (
        await session.scalars(
            select(CodingCertificationLease)
            .where(
                CodingCertificationLease.agent_id == agent_id,
                CodingCertificationLease.artifact_sha256 == artifact_sha256,
                CodingCertificationLease.screened_image_sha256 == screened_image_sha256,
                CodingCertificationLease.bench_version == bench_version,
                CodingCertificationLease.coding_contract_version
                == coding_contract_version,
                CodingCertificationLease.status.in_(_INFLIGHT),
            )
            .with_for_update()
        )
    ).all()
    for row in rows:
        await _expire_if_due(session, row, now=now)
    await session.flush()


def mark_certification_inference_grant_revoked(
    grant: CodingCertificationInferenceGrant, *, now: datetime
) -> None:
    """Terminal revocation: clears every bearer binding, keeps the accounting."""

    grant.status = "revoked"
    grant.bearer_digest = None
    grant.revoke_bearer_digest = None
    grant.broker_public_key = None
    grant.active_requests = 0
    grant.revoked_at = now
    grant.updated_at = now


async def _expire_if_due(
    session: AsyncSession,
    row: CodingCertificationLease,
    *,
    now: datetime,
) -> bool:
    """Expire one locked in-flight lease past its deadline and revoke its grant.

    ``claimed_at`` is kept, so an expired row still shows whether the attempt
    was claimed. Nothing is deleted.
    """

    if row.status not in _INFLIGHT or _aware(row.deadline) > now:
        return False
    row.status = CodingCertificationLeaseStatus.EXPIRED.value
    grants = (
        await session.scalars(
            select(CodingCertificationInferenceGrant)
            .where(
                CodingCertificationInferenceGrant.lease_id == row.lease_id,
                CodingCertificationInferenceGrant.status.in_(("pending", "active")),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for grant in grants:
        mark_certification_inference_grant_revoked(grant, now=now)
    await session.flush()
    return True


async def expire_coding_certification_lease_if_due(
    session: AsyncSession,
    *,
    lease_id: UUID,
) -> bool:
    """Commit-side helper for callers that must refuse a lease past its deadline."""

    now = await _database_now(session)
    row = await session.get(
        CodingCertificationLease,
        lease_id,
        with_for_update=True,
        populate_existing=True,
    )
    if row is None:
        return False
    return await _expire_if_due(session, row, now=now)


async def issue_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    coding_contract_version: int = 1,
) -> CodingCertificationLeaseResult:
    """Mint one canary lease if current core qualification still holds."""

    if coding_contract_version != 1:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease contract is not available"
        )
    agent = await session.get(Agent, agent_id, with_for_update=True)
    if agent is None or not _screened_image_is_complete(agent):
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    assert agent.screened_image_sha256 is not None
    assert agent.screened_image_id is not None
    assert agent.screened_image_ref is not None
    assert agent.screened_image_upload_id is not None
    await require_coding_certification_allowlisted(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        validator_hotkey=validator_hotkey,
    )
    await lock_core_qualification_bench(session, bench_version=bench_version)
    policy = await latest_core_qualification_policy(
        session, bench_version=bench_version
    )
    if policy is None:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    observation = await latest_complete_core_qualification_observation(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        policy_revision=policy.revision,
    )
    if (
        observation is None
        or not observation.qualified
        or observation.policy_checksum != policy.checksum
        or observation.weight_eligible
    ):
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    now = await _database_now(session)
    await _expire_due_leases(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        coding_contract_version=coding_contract_version,
        now=now,
    )
    inflight = await session.scalar(
        select(CodingCertificationLease)
        .where(
            CodingCertificationLease.agent_id == agent.agent_id,
            CodingCertificationLease.artifact_sha256 == agent.sha256,
            CodingCertificationLease.screened_image_sha256
            == agent.screened_image_sha256,
            CodingCertificationLease.bench_version == bench_version,
            CodingCertificationLease.coding_contract_version == coding_contract_version,
            CodingCertificationLease.status.in_(_INFLIGHT),
        )
        .with_for_update()
        .limit(1)
    )
    if inflight is not None:
        if (
            inflight.validator_hotkey == validator_hotkey
            and inflight.status == CodingCertificationLeaseStatus.ISSUED.value
        ):
            return result_from_row(inflight, idempotent=True)
        raise CodingCertificationLeaseConflictError(
            "coding certification lease already exists for this artifact"
        )
    recent_claims = int(
        await session.scalar(
            select(func.count())
            .select_from(CodingCertificationLease)
            .where(
                CodingCertificationLease.agent_id == agent.agent_id,
                CodingCertificationLease.artifact_sha256 == agent.sha256,
                CodingCertificationLease.screened_image_sha256
                == agent.screened_image_sha256,
                CodingCertificationLease.bench_version == bench_version,
                CodingCertificationLease.coding_contract_version
                == coding_contract_version,
                CodingCertificationLease.claimed_at.is_not(None),
                CodingCertificationLease.claimed_at > now - CLAIMED_ATTEMPT_WINDOW,
            )
        )
        or 0
    )
    if recent_claims >= MAX_CLAIMED_ATTEMPTS_PER_IDENTITY:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification attempt budget is exhausted"
        )
    try:
        canary = public_certification_canary()
    except CodingCertificationCanaryUnavailableError as error:
        raise CodingCertificationLeaseUnavailableError(str(error)) from error
    lease_id = uuid4()
    deadline = now + _LEASE_TTL
    authority = CodingCertificationLeaseAuthority(
        schema="dittobench-coding-certification-lease-v1",
        coding_contract_version=1,
        weight_eligible=False,
        lease_id=lease_id,
        validator_hotkey=validator_hotkey,
        agent_id=agent.agent_id,
        agent_artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        core_qualification_observation_id=observation.observation_id,
        core_qualification_policy_checksum=observation.policy_checksum,
        canary_manifest_sha256=canary.canary_manifest_sha256,
        runner_plan_sha256=canary.runner_plan_sha256,
        grader_plan_sha256=canary.grader_plan_sha256,
        resource_profile_sha256=canary.resource_profile_sha256,
        inference_policy_sha256=canary.inference_policy_sha256,
        issued_at=now,
        deadline=deadline,
    )
    row = CodingCertificationLease(
        lease_id=lease_id,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_id=agent.screened_image_id,
        screened_image_ref=agent.screened_image_ref,
        screened_image_upload_id=agent.screened_image_upload_id,
        validator_hotkey=validator_hotkey,
        bench_version=bench_version,
        coding_contract_version=1,
        core_qualification_observation_id=observation.observation_id,
        core_qualification_policy_checksum=observation.policy_checksum,
        canary_manifest_sha256=canary.canary_manifest_sha256,
        runner_plan_sha256=canary.runner_plan_sha256,
        grader_plan_sha256=canary.grader_plan_sha256,
        resource_profile_sha256=canary.resource_profile_sha256,
        inference_policy_sha256=canary.inference_policy_sha256,
        status=CodingCertificationLeaseStatus.ISSUED.value,
        weight_eligible=False,
        issued_at=now,
        deadline=deadline,
        authority=authority.model_dump(mode="json", by_alias=True),
    )
    session.add(row)
    await session.flush()
    return result_from_row(row, idempotent=False)


async def claim_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    lease_id: UUID,
) -> CodingCertificationLeaseResult:
    """Exclusive claim of an issued lease by the named validator."""

    now = await _database_now(session)
    row = await session.get(CodingCertificationLease, lease_id, with_for_update=True)
    if row is None or row.validator_hotkey != validator_hotkey:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    if await _expire_if_due(session, row, now=now):
        return result_from_row(row, idempotent=False)
    if row.status == CodingCertificationLeaseStatus.CLAIMED.value:
        return result_from_row(row, idempotent=True)
    if row.status != CodingCertificationLeaseStatus.ISSUED.value:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    row.status = CodingCertificationLeaseStatus.CLAIMED.value
    row.claimed_at = now
    await session.flush()
    return result_from_row(row, idempotent=False)


async def abort_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    lease_id: UUID,
) -> CodingCertificationLeaseResult:
    """Abort an unclaimed issued lease.

    A claimed lease cannot be aborted before its deadline, so a restart cannot
    create an immediate clean rerun; after the deadline it only expires.
    """

    now = await _database_now(session)
    row = await session.get(CodingCertificationLease, lease_id, with_for_update=True)
    if row is None or row.validator_hotkey != validator_hotkey:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    if row.status == CodingCertificationLeaseStatus.ABORTED.value:
        return result_from_row(row, idempotent=True)
    if await _expire_if_due(session, row, now=now):
        return result_from_row(row, idempotent=False)
    if row.status == CodingCertificationLeaseStatus.CLAIMED.value:
        raise CodingCertificationLeaseConflictError(
            "claimed coding certification lease cannot be aborted"
        )
    if row.status != CodingCertificationLeaseStatus.ISSUED.value:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    row.status = CodingCertificationLeaseStatus.ABORTED.value
    row.aborted_at = now
    await session.flush()
    return result_from_row(row, idempotent=False)


async def get_coding_certification_lease(
    session: AsyncSession,
    *,
    lease_id: UUID,
) -> CodingCertificationLease | None:
    return await session.get(CodingCertificationLease, lease_id)


async def list_coding_certification_leases(
    session: AsyncSession,
    *,
    agent_id: UUID | None,
    validator_hotkey: str | None,
    status: CodingCertificationLeaseStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[CodingCertificationLeaseAuditRow], int]:
    """Read-only, newest-first lease audit page. Never transitions a row."""

    filters = []
    if agent_id is not None:
        filters.append(CodingCertificationLease.agent_id == agent_id)
    if validator_hotkey is not None:
        filters.append(CodingCertificationLease.validator_hotkey == validator_hotkey)
    if status is not None:
        filters.append(CodingCertificationLease.status == status.value)
    total = int(
        await session.scalar(
            select(func.count()).select_from(CodingCertificationLease).where(*filters)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(
                CodingCertificationLease,
                CodingCertificationInferenceGrant.status,
                CodingCapabilityCertification.status,
            )
            .outerjoin(
                CodingCertificationInferenceGrant,
                CodingCertificationInferenceGrant.lease_id
                == CodingCertificationLease.lease_id,
            )
            .outerjoin(
                CodingCapabilityCertification,
                CodingCapabilityCertification.lease_id
                == CodingCertificationLease.lease_id,
            )
            .where(*filters)
            .order_by(
                CodingCertificationLease.issued_at.desc(),
                CodingCertificationLease.lease_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return (
        [
            CodingCertificationLeaseAuditRow(
                lease=lease,
                inference_grant_status=grant_status,
                receipt_status=receipt_status,
            )
            for lease, grant_status, receipt_status in rows
        ],
        total,
    )


async def authorize_coding_certification_harness_delivery(
    session: AsyncSession,
    *,
    lease_id: UUID,
    validator_hotkey: str,
) -> CodingCertificationHarnessAuthority:
    """Return the current screened image for one claimed certification lease."""

    lease = await session.get(CodingCertificationLease, lease_id, with_for_update=True)
    agent = await session.get(Agent, lease.agent_id) if lease is not None else None
    now = await _database_now(session)
    if (
        lease is None
        or agent is None
        or lease.validator_hotkey != validator_hotkey
        or lease.status != CodingCertificationLeaseStatus.CLAIMED.value
        or lease.weight_eligible
        or _aware(lease.deadline) <= now
        or lease.artifact_sha256 != agent.sha256
        or lease.screened_image_sha256 != agent.screened_image_sha256
        or agent.screened_image_sha256 is None
        or agent.screened_image_size_bytes is None
        or agent.screened_image_size_bytes <= 0
        or agent.screened_image_size_bytes > 8 << 30
        or agent.screened_image_id is None
        or agent.screened_image_ref is None
        or agent.screened_image_upload_id is None
        or agent.screening_policy_version < 9
        or agent.screened_image_id != lease.screened_image_id
        or agent.screened_image_ref != lease.screened_image_ref
        or agent.screened_image_upload_id != lease.screened_image_upload_id
        or agent.screened_image_ref != f"ditto-screen/{agent.agent_id}:latest"
    ):
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification harness is unavailable for this validator"
        )
    return CodingCertificationHarnessAuthority(
        agent_id=agent.agent_id,
        lease_id=lease.lease_id,
        deadline=_aware(lease.deadline),
        bench_version=lease.bench_version,
        agent_artifact_sha256=lease.artifact_sha256,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_size_bytes=agent.screened_image_size_bytes,
        screened_image_id=agent.screened_image_id,
        screened_image_ref=agent.screened_image_ref,
        screened_image_upload_id=agent.screened_image_upload_id,
        screening_policy_version=agent.screening_policy_version,
    )
