"""Postgres tests for claimed-lease recovery and the certification allowlist."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification import (
    CodingCapabilityCertificationReceipt,
)
from ditto.api_models.coding_certification_admin import (
    CodingCertificationAllowlistEntry,
    coding_certification_allowlist_checksum,
)
from ditto.api_models.coding_certification_leases import CodingCertificationLeaseStatus
from ditto.api_models.coding_inference import CodingInferencePolicy
from ditto.db.models import (
    Agent,
    CodingCertificationAllowlistRevision,
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries.coding_certification_allowlist import (
    CodingCertificationAllowlistRefusedError,
    CodingCertificationAllowlistRevisionConflictError,
    active_coding_certification_allowlist,
    insert_coding_certification_allowlist_revision,
)
from ditto.db.queries.coding_certification_inference_grants import (
    activate_coding_certification_inference_grant,
    ensure_coding_certification_inference_grant,
    revoke_unlisted_coding_certification_inference_grants,
)
from ditto.db.queries.coding_certification_leases import (
    CLAIMED_ATTEMPT_WINDOW,
    MAX_CLAIMED_ATTEMPTS_PER_IDENTITY,
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    abort_coding_certification_lease,
    claim_coding_certification_lease,
    expire_coding_certification_lease_if_due,
    issue_coding_certification_lease,
    list_coding_certification_leases,
)
from ditto.db.queries.coding_certifications import (
    coding_certification_lease_accepts_receipt,
)
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantNotAvailableError,
)
from ditto.tests.db.queries.test_coding_certification_leases import (
    _BENCH_VERSION,
    _OTHER_VALIDATOR,
    _VALIDATOR,
    _seed_agent,
    _seed_observation,
)

_POLICY_PATH = (
    Path(__file__).parents[6]
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_BROKER_KEY = "A" * 43


def _policy() -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate(
        json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["policy"]
    )


async def _qualified_agent(session: AsyncSession, evidence: str = "11") -> Agent:
    agent = await _seed_agent(session)
    await _seed_observation(session, agent, evidence_sha256=evidence * 32)
    return agent


async def _issue(
    session: AsyncSession, agent: Agent, *, validator: str = _VALIDATOR
) -> UUID:
    async with session.begin():
        issued = await issue_coding_certification_lease(
            session,
            validator_hotkey=validator,
            agent_id=agent.agent_id,
            bench_version=_BENCH_VERSION,
        )
    return issued.row.lease_id


async def _issue_and_claim(
    session: AsyncSession, agent: Agent, *, validator: str = _VALIDATOR
) -> UUID:
    lease_id = await _issue(session, agent, validator=validator)
    async with session.begin():
        claimed = await claim_coding_certification_lease(
            session, validator_hotkey=validator, lease_id=lease_id
        )
    assert claimed.row.status == CodingCertificationLeaseStatus.CLAIMED.value
    return lease_id


async def _live_grant(
    session: AsyncSession, lease_id: UUID, *, validator: str = _VALIDATOR
) -> UUID:
    async with session.begin():
        offered = await ensure_coding_certification_inference_grant(
            session,
            lease_id=lease_id,
            validator_hotkey=validator,
            policy=_policy(),
        )
    async with session.begin():
        activated = await activate_coding_certification_inference_grant(
            session,
            grant_id=offered.grant.grant_id,
            validator_hotkey=validator,
            broker_public_key=_BROKER_KEY,
            policy=_policy(),
        )
    assert activated.grant.status == "active"
    return offered.grant.grant_id


async def _backdate(
    session: AsyncSession, lease_id: UUID, *, ago: timedelta = timedelta(minutes=25)
) -> None:
    """Move a claimed lease (and its grant) wholly into the past, CHECK-valid."""

    issued_at = datetime.now(UTC) - ago
    async with session.begin():
        await session.execute(
            update(CodingCertificationLease)
            .where(CodingCertificationLease.lease_id == lease_id)
            .values(
                issued_at=issued_at,
                claimed_at=issued_at + timedelta(minutes=1),
                deadline=issued_at + timedelta(minutes=20),
            )
        )
        await session.execute(
            update(CodingCertificationInferenceGrant)
            .where(CodingCertificationInferenceGrant.lease_id == lease_id)
            .values(
                created_at=issued_at + timedelta(minutes=2),
                expires_at=issued_at + timedelta(minutes=20),
            )
        )


async def _lease(session: AsyncSession, lease_id: UUID) -> CodingCertificationLease:
    async with session.begin():
        row = await session.get(
            CodingCertificationLease, lease_id, populate_existing=True
        )
    assert row is not None
    return row


async def _grant(
    session: AsyncSession, grant_id: UUID
) -> CodingCertificationInferenceGrant:
    async with session.begin():
        row = await session.get(
            CodingCertificationInferenceGrant, grant_id, populate_existing=True
        )
    assert row is not None
    return row


async def _lease_count(session: AsyncSession) -> int:
    async with session.begin():
        return int(
            await session.scalar(
                select(func.count()).select_from(CodingCertificationLease)
            )
            or 0
        )


async def _set_allowlist(
    session: AsyncSession,
    entries: list[CodingCertificationAllowlistEntry],
    *,
    enabled: bool = True,
) -> int:
    async with session.begin():
        current = await session.scalar(
            select(func.max(CodingCertificationAllowlistRevision.revision))
        )
        await insert_coding_certification_allowlist_revision(
            session,
            expected_revision=int(current or 0),
            enabled=enabled,
            entries=entries,
            reason="restrict certification to the team canary",
            actor="operator@example.com",
        )
        return await revoke_unlisted_coding_certification_inference_grants(session)


def _entry(
    agent: Agent, validator: str = _VALIDATOR
) -> CodingCertificationAllowlistEntry:
    return CodingCertificationAllowlistEntry(
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        validator_hotkey=validator,
    )


async def test_claimed_lease_expiry_releases_slot_revokes_grant_and_keeps_audit(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    lease_id = await _issue_and_claim(session, agent)
    grant_id = await _live_grant(session, lease_id)

    # Before the deadline the claimed lease still holds the identity.
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseConflictError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_OTHER_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )

    await _backdate(session, lease_id)
    async with session.begin():
        with pytest.raises(CodingInferenceGrantNotAvailableError):
            await ensure_coding_certification_inference_grant(
                session,
                lease_id=lease_id,
                validator_hotkey=_VALIDATOR,
                policy=_policy(),
            )

    replacement = await _issue(session, agent, validator=_OTHER_VALIDATOR)
    assert replacement != lease_id

    expired = await _lease(session, lease_id)
    assert expired.status == CodingCertificationLeaseStatus.EXPIRED.value
    assert expired.claimed_at is not None
    assert expired.aborted_at is None
    grant = await _grant(session, grant_id)
    assert grant.status == "revoked"
    assert grant.revoked_at is not None
    assert grant.bearer_digest is None
    assert grant.revoke_bearer_digest is None
    assert grant.broker_public_key is None
    assert grant.generation == 1
    assert await _lease_count(session) == 2

    async with session.begin():
        with pytest.raises(CodingInferenceGrantNotAvailableError):
            await activate_coding_certification_inference_grant(
                session,
                grant_id=grant_id,
                validator_hotkey=_VALIDATOR,
                broker_public_key=_BROKER_KEY,
                policy=_policy(),
            )


async def test_claim_abort_and_explicit_expiry_after_deadline(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    claimed = await _issue_and_claim(session, agent)
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseConflictError):
            await abort_coding_certification_lease(
                session, validator_hotkey=_VALIDATOR, lease_id=claimed
            )
    await _backdate(session, claimed)
    async with session.begin():
        retried = await claim_coding_certification_lease(
            session, validator_hotkey=_VALIDATOR, lease_id=claimed
        )
    assert retried.row.status == CodingCertificationLeaseStatus.EXPIRED.value
    assert (await _lease(session, claimed)).status == "expired"

    aborted_late = await _issue_and_claim(session, agent)
    await _backdate(session, aborted_late)
    async with session.begin():
        result = await abort_coding_certification_lease(
            session, validator_hotkey=_VALIDATOR, lease_id=aborted_late
        )
    stored = await _lease(session, aborted_late)
    assert result.row.status == stored.status == "expired"
    assert stored.aborted_at is None and stored.claimed_at is not None

    live = await _issue_and_claim(session, agent)
    async with session.begin():
        assert (
            await expire_coding_certification_lease_if_due(session, lease_id=live)
            is False
        )
    assert (await _lease(session, live)).status == "claimed"


async def test_claimed_attempt_budget_bounds_reruns_per_identity(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    leases = []
    for _ in range(MAX_CLAIMED_ATTEMPTS_PER_IDENTITY):
        lease_id = await _issue_and_claim(session, agent)
        await _backdate(session, lease_id)
        leases.append(lease_id)
    async with session.begin():
        with pytest.raises(
            CodingCertificationLeaseNotAvailableError, match="attempt budget"
        ):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )
    # Aging one claimed attempt out of the rolling window reopens the identity.
    await _backdate(
        session,
        leases[0],
        ago=CLAIMED_ATTEMPT_WINDOW + timedelta(minutes=30),
    )
    reopened = await _issue(session, agent)
    assert reopened not in leases


async def test_allowlist_is_disabled_by_default_and_disabled_revision_preserves_issue(
    session: AsyncSession,
) -> None:
    async with session.begin():
        assert await active_coding_certification_allowlist(session) is None
    first = await _qualified_agent(session)
    assert await _issue(session, first)

    assert await _set_allowlist(session, [], enabled=False) == 0
    async with session.begin():
        assert await active_coding_certification_allowlist(session) is None
    second = await _qualified_agent(session, evidence="22")
    assert await _issue(session, second)


async def test_enabled_allowlist_refuses_unlisted_issue_before_any_row(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)

    async def refused(validator: str = _VALIDATOR) -> None:
        async with session.begin():
            with pytest.raises(
                CodingCertificationAllowlistRefusedError,
                match="coding certification is not allowlisted",
            ):
                await issue_coding_certification_lease(
                    session,
                    validator_hotkey=validator,
                    agent_id=agent.agent_id,
                    bench_version=_BENCH_VERSION,
                )
        assert await _lease_count(session) == 0

    await _set_allowlist(session, [])
    await refused()

    await _set_allowlist(session, [_entry(agent, _OTHER_VALIDATOR)])
    await refused()

    wrong_artifact = CodingCertificationAllowlistEntry(
        agent_id=agent.agent_id,
        artifact_sha256="99" * 32,
        validator_hotkey=_VALIDATOR,
    )
    await _set_allowlist(session, [wrong_artifact])
    await refused()

    await _set_allowlist(session, [wrong_artifact, _entry(agent)])
    await refused(_OTHER_VALIDATOR)
    assert await _issue(session, agent)
    assert await _lease_count(session) == 1


async def test_enabled_allowlist_refuses_grants_and_revokes_unlisted_live_grants(
    session: AsyncSession,
) -> None:
    canary = await _qualified_agent(session)
    other = await _qualified_agent(session, evidence="22")
    canary_lease = await _issue_and_claim(session, canary)
    other_lease = await _issue_and_claim(session, other)
    canary_grant = await _live_grant(session, canary_lease)
    other_grant = await _live_grant(session, other_lease)
    pending_agent = await _qualified_agent(session, evidence="33")
    pending_lease = await _issue_and_claim(session, pending_agent)

    assert await _set_allowlist(session, [_entry(canary)]) == 1
    assert (await _grant(session, canary_grant)).status == "active"
    revoked = await _grant(session, other_grant)
    assert revoked.status == "revoked" and revoked.bearer_digest is None

    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await activate_coding_certification_inference_grant(
                session,
                grant_id=other_grant,
                validator_hotkey=_VALIDATOR,
                broker_public_key=_BROKER_KEY,
                policy=_policy(),
            )
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await ensure_coding_certification_inference_grant(
                session,
                lease_id=pending_lease,
                validator_hotkey=_VALIDATOR,
                policy=_policy(),
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CodingCertificationInferenceGrant)
                .where(CodingCertificationInferenceGrant.lease_id == pending_lease)
            )
            == 0
        )

    # The listed canary tuple keeps working end to end.
    async with session.begin():
        replay = await ensure_coding_certification_inference_grant(
            session,
            lease_id=canary_lease,
            validator_hotkey=_VALIDATOR,
            policy=_policy(),
        )
    assert replay.idempotent is True and replay.grant.status == "active"

    async with session.begin():
        rows, total = await list_coding_certification_leases(
            session,
            agent_id=None,
            validator_hotkey=_VALIDATOR,
            status=CodingCertificationLeaseStatus.CLAIMED,
            limit=3,
            offset=0,
        )
        tail, tail_total = await list_coding_certification_leases(
            session,
            agent_id=None,
            validator_hotkey=_VALIDATOR,
            status=None,
            limit=2,
            offset=2,
        )
    assert total == tail_total == 3 and len(rows) == 3 and len(tail) == 1
    assert [row.lease.lease_id for row in rows] == [
        pending_lease,
        other_lease,
        canary_lease,
    ]
    assert tail[0].lease.lease_id == canary_lease
    assert [row.inference_grant_status for row in rows] == [None, "revoked", "active"]
    assert all(row.receipt_status is None for row in rows)


async def test_allowlist_revisions_are_append_only_revisioned_and_fail_closed(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    agent_id, artifact_sha256 = agent.agent_id, agent.sha256
    await _set_allowlist(session, [_entry(agent)])

    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRevisionConflictError):
            await insert_coding_certification_allowlist_revision(
                session,
                expected_revision=0,
                enabled=False,
                entries=[],
                reason="stale operator write attempt",
                actor="operator@example.com",
            )
    for statement in (
        "UPDATE coding_certification_allowlist_revisions SET enabled = false",
        "DELETE FROM coding_certification_allowlist_revisions",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin():
                await session.execute(text(statement))

    # A revision whose checksum does not bind its entries never widens access.
    async with session.begin():
        session.add(
            CodingCertificationAllowlistRevision(
                parent_revision=1,
                enabled=True,
                entries=[
                    {
                        "agent_id": str(agent_id),
                        "artifact_sha256": artifact_sha256,
                        "validator_hotkey": _VALIDATOR,
                    }
                ],
                checksum=coding_certification_allowlist_checksum(
                    enabled=True, entries=[]
                ),
                reason="tampered revision fixture",
                actor="test",
            )
        )
    async with session.begin():
        assert await active_coding_certification_allowlist(session) == frozenset()
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent_id,
                bench_version=_BENCH_VERSION,
            )
    assert await _lease_count(session) == 0


def test_receipt_acceptance_requires_a_live_claimed_deadline() -> None:
    now = datetime.now(UTC)
    lease = SimpleNamespace(
        status="claimed",
        validator_hotkey=_VALIDATOR,
        agent_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        bench_version=_BENCH_VERSION,
        coding_contract_version=1,
        weight_eligible=False,
        deadline=now + timedelta(minutes=1),
    )
    receipt = SimpleNamespace(coding_contract_version=1, weight_eligible=False)

    def accepts(at: datetime) -> bool:
        return coding_certification_lease_accepts_receipt(
            cast(CodingCertificationLease, lease),
            validator_hotkey=_VALIDATOR,
            agent_id=lease.agent_id,
            artifact_sha256=lease.artifact_sha256,
            screened_image_sha256=lease.screened_image_sha256,
            bench_version=_BENCH_VERSION,
            receipt=cast(CodingCapabilityCertificationReceipt, receipt),
            now=at,
        )

    assert accepts(now) is True
    assert accepts(lease.deadline) is False
    assert accepts(now + timedelta(minutes=5)) is False
