"""Postgres tests for certification lease recovery and the strict allowlist."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_certification_admin import (
    CodingCertificationAllowlistEntry,
    coding_certification_allowlist_checksum,
)
from ditto.api_models.coding_certification_leases import (
    CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS,
    CodingCertificationLeaseStatus,
)
from ditto.api_models.coding_inference import CodingInferencePolicy
from ditto.db.models import (
    CODING_CERTIFICATION_LEASE_LIFECYCLE,
    Agent,
    CodingCapabilityCertification,
    CodingCertificationAllowlistRevision,
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries.coding_certification_allowlist import (
    CodingCertificationAllowlistRefusedError,
    CodingCertificationAllowlistRevisionConflictError,
    active_coding_certification_allowlist,
    allowlist_revision_from_row,
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
    RECEIPT_GRACE,
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    abort_coding_certification_lease,
    abort_unlisted_coding_certification_leases,
    authorize_coding_certification_harness_delivery,
    claim_coding_certification_lease,
    complete_coding_certification_lease,
    database_now,
    expire_coding_certification_lease_if_due,
    issue_coding_certification_lease,
    lease_is_due,
    list_coding_certification_leases,
    lock_coding_certification_lease,
    restamp_admitted_coding_certification_leases,
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
    admit_certification_tuples,
)

_ROOT = Path(__file__).parents[6]
_POLICY_PATH = (
    _ROOT
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_MIGRATION = (
    Path(__file__).parents[4]
    / "alembic/versions/2026_09_14_add_coding_certification_canary_safety.py"
)
_BROKER_KEY = "A" * 43


def _policy() -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate(
        json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["policy"]
    )


async def _qualified_agent(
    session: AsyncSession,
    evidence: str = "11",
    *,
    admit: tuple[str, ...] = (_VALIDATOR, _OTHER_VALIDATOR),
) -> Agent:
    agent = await _seed_agent(session)
    await _seed_observation(session, agent, evidence_sha256=evidence * 32)
    if admit:
        await admit_certification_tuples(
            session, *((agent.agent_id, agent.sha256, v) for v in admit)
        )
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
    assert claimed.row.claim_allowlist_revision is not None
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
    """Move a claimed lease (and its grant) into the past, CHECK-valid.

    The default puts the deadline 5 minutes back, past the receipt window.
    """

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
) -> tuple[int, int]:
    """Append a revision exactly as the admin write does; (aborted, revoked)."""

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
        allowlist = await active_coding_certification_allowlist(session)
        aborted = await abort_unlisted_coding_certification_leases(
            session, allowlist=allowlist
        )
        await restamp_admitted_coding_certification_leases(session, allowlist=allowlist)
        revoked = await revoke_unlisted_coding_certification_inference_grants(
            session, allowlist=allowlist
        )
    return aborted, revoked


async def _append_corrupt_revision(session: AsyncSession, agent: Agent) -> None:
    """A stored revision whose checksum does not bind its entries."""

    agent_id, artifact_sha256 = agent.agent_id, agent.sha256
    async with session.begin():
        current = await session.scalar(
            select(func.max(CodingCertificationAllowlistRevision.revision))
        )
        session.add(
            CodingCertificationAllowlistRevision(
                parent_revision=int(current or 0),
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


def _entry(
    agent: Agent, validator: str = _VALIDATOR
) -> CodingCertificationAllowlistEntry:
    return CodingCertificationAllowlistEntry(
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        validator_hotkey=validator,
    )


async def _record_receipt(
    session: AsyncSession, lease_id: UUID, *, validator: str = _VALIDATOR
) -> None:
    """Persist a failed receipt and complete its lease, as the receipt write does."""

    async with session.begin():
        lease = await session.get(
            CodingCertificationLease, lease_id, with_for_update=True
        )
        assert lease is not None
        issued = datetime.now(UTC)
        session.add(
            CodingCapabilityCertification(
                certification_row_id=uuid4(),
                agent_id=lease.agent_id,
                artifact_sha256=lease.artifact_sha256,
                screened_image_sha256=lease.screened_image_sha256,
                validator_hotkey=validator,
                bench_version=lease.bench_version,
                lease_id=lease.lease_id,
                ticket_deadline=lease.deadline,
                coding_contract_version=1,
                certification_id=f"cert-{lease.lease_id}",
                status="failed",
                failure_stage="grade",
                failure_code="public_canary_failed",
                certification_sha256="ab" * 32,
                canary_manifest_sha256=lease.canary_manifest_sha256,
                transcript_object_key=None,
                frozen_submission_object_key=None,
                issued_at=issued,
                expires_at=issued + timedelta(hours=1),
                weight_eligible=False,
                receipt={"status": "failed"},
                signature="ab" * 64,
            )
        )
        await session.flush()
        complete_coding_certification_lease(lease)


def test_migration_lifecycle_matches_the_model() -> None:
    spec = importlib.util.spec_from_file_location("canary_safety", _MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration._RECOVERABLE_LIFECYCLE == CODING_CERTIFICATION_LEASE_LIFECYCLE


def test_receipt_window_is_the_only_extension_of_a_claimed_lease() -> None:
    grace = timedelta(seconds=CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS)
    assert grace == RECEIPT_GRACE
    deadline = datetime(2026, 9, 15, 12, tzinfo=UTC)

    def due(status: str, at: datetime) -> bool:
        lease = SimpleNamespace(status=status, deadline=deadline)
        return lease_is_due(cast(CodingCertificationLease, lease), now=at)

    assert due("issued", deadline) is True
    assert due("issued", deadline - timedelta(microseconds=1)) is False
    assert due("claimed", deadline) is False
    assert due("claimed", deadline + RECEIPT_GRACE - timedelta(microseconds=1)) is False
    assert due("claimed", deadline + RECEIPT_GRACE) is True
    for terminal in ("completed", "aborted", "expired"):
        assert due(terminal, deadline + timedelta(days=1)) is False


async def test_claimed_lease_expiry_releases_slot_revokes_grant_and_keeps_audit(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    lease_id = await _issue_and_claim(session, agent)
    grant_id = await _live_grant(session, lease_id)

    # Before the receipt window closes the claimed lease still holds the slot.
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


async def test_claim_abort_and_explicit_expiry_after_receipt_window(
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
        gate = await lock_coding_certification_lease(
            session, lease_id=live, validator_hotkey=_VALIDATOR
        )
        assert (
            await expire_coding_certification_lease_if_due(
                session, lease=gate.lease, now=gate.now
            )
            is False
        )
    assert (await _lease(session, live)).status == "claimed"


async def test_receipt_window_keeps_the_lease_but_not_harness_or_grants(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    lease_id = await _issue_and_claim(session, agent)
    grant_id = await _live_grant(session, lease_id)
    # Deadline 30 seconds ago: inside the receipt window.
    await _backdate(session, lease_id, ago=timedelta(minutes=20, seconds=30))

    async with session.begin():
        with pytest.raises(CodingCertificationLeaseNotAvailableError):
            await authorize_coding_certification_harness_delivery(
                session, lease_id=lease_id, validator_hotkey=_VALIDATOR
            )
    async with session.begin():
        with pytest.raises(CodingInferenceGrantNotAvailableError):
            await activate_coding_certification_inference_grant(
                session,
                grant_id=grant_id,
                validator_hotkey=_VALIDATOR,
                broker_public_key=_BROKER_KEY,
                policy=_policy(),
            )
    async with session.begin():
        retried = await claim_coding_certification_lease(
            session, validator_hotkey=_VALIDATOR, lease_id=lease_id
        )
        with pytest.raises(CodingCertificationLeaseConflictError):
            await abort_coding_certification_lease(
                session, validator_hotkey=_VALIDATOR, lease_id=lease_id
            )
    assert retried.idempotent is True
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseConflictError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )
    assert (await _lease(session, lease_id)).status == "claimed"

    await _backdate(session, lease_id)
    assert await _issue(session, agent) != lease_id
    assert (await _lease(session, lease_id)).status == "expired"


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


async def test_attempt_budget_ignores_claims_no_allowlist_admitted(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    # Three claims by another validator made before the strict allowlist named
    # anyone: they carry no admitting revision.
    for _ in range(MAX_CLAIMED_ATTEMPTS_PER_IDENTITY):
        lease_id = await _issue_and_claim(session, agent, validator=_OTHER_VALIDATOR)
        await _backdate(session, lease_id)
        async with session.begin():
            await session.execute(
                update(CodingCertificationLease)
                .where(CodingCertificationLease.lease_id == lease_id)
                .values(claim_allowlist_revision=None)
            )
    await _set_allowlist(session, [_entry(agent)])

    canary = []
    for _ in range(MAX_CLAIMED_ATTEMPTS_PER_IDENTITY):
        lease_id = await _issue_and_claim(session, agent)
        await _backdate(session, lease_id)
        canary.append(lease_id)
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
    assert await _lease_count(session) == 2 * MAX_CLAIMED_ATTEMPTS_PER_IDENTITY


async def test_allowlist_refuses_every_tuple_by_default_and_when_refuse_all(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session, admit=())
    async with session.begin():
        default = await active_coding_certification_allowlist(session)
    assert default.revision == 0 and default.tuples == frozenset()
    for validator in (_VALIDATOR, _OTHER_VALIDATOR):
        async with session.begin():
            with pytest.raises(CodingCertificationAllowlistRefusedError):
                await issue_coding_certification_lease(
                    session,
                    validator_hotkey=validator,
                    agent_id=agent.agent_id,
                    bench_version=_BENCH_VERSION,
                )
    assert await _lease_count(session) == 0

    await _set_allowlist(session, [_entry(agent)])
    live = await _issue_and_claim(session, agent)

    # A refuse-all revision cannot reopen anything; it aborts what is in flight.
    assert await _set_allowlist(session, [], enabled=False) == (1, 0)
    async with session.begin():
        refuse_all = await active_coding_certification_allowlist(session)
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )
    assert refuse_all.revision == 2 and refuse_all.tuples == frozenset()
    aborted = await _lease(session, live)
    assert aborted.status == "aborted" and aborted.aborted_allowlist_revision == 2
    assert aborted.claimed_at is not None and aborted.claim_allowlist_revision == 1

    # "Enabled" with no tuples is not a representable revision.
    with pytest.raises(IntegrityError):
        async with session.begin():
            await insert_coding_certification_allowlist_revision(
                session,
                expected_revision=2,
                enabled=True,
                entries=[],
                reason="attempt an open enabled revision",
                actor="operator@example.com",
            )


async def test_enabled_allowlist_refuses_unlisted_issue_before_any_row(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session, admit=())

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

    await _set_allowlist(session, [], enabled=False)
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


async def test_shared_gate_refuses_claim_harness_and_grants_for_a_refused_tuple(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session, admit=(_VALIDATOR,))
    claimed = await _issue_and_claim(session, agent)
    grant_id = await _live_grant(session, claimed)
    other = await _qualified_agent(session, evidence="22", admit=(_VALIDATOR,))
    issued = await _issue(session, other)

    # Another validator's lease is simply unavailable, not an allowlist refusal.
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseNotAvailableError):
            await claim_coding_certification_lease(
                session, validator_hotkey=_OTHER_VALIDATOR, lease_id=issued
            )

    # A corrupt revision is appended without the admin write's abort pass, so
    # the in-flight leases stay in flight and only the gate protects them.
    await _append_corrupt_revision(session, agent)

    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await claim_coding_certification_lease(
                session, validator_hotkey=_VALIDATOR, lease_id=issued
            )
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await claim_coding_certification_lease(
                session, validator_hotkey=_VALIDATOR, lease_id=claimed
            )
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await authorize_coding_certification_harness_delivery(
                session, lease_id=claimed, validator_hotkey=_VALIDATOR
            )
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await activate_coding_certification_inference_grant(
                session,
                grant_id=grant_id,
                validator_hotkey=_VALIDATOR,
                broker_public_key=_BROKER_KEY,
                policy=_policy(),
            )
    # The refused exchange terminally revoked the live grant and committed it.
    revoked = await _grant(session, grant_id)
    assert revoked.status == "revoked" and revoked.bearer_digest is None
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await ensure_coding_certification_inference_grant(
                session,
                lease_id=claimed,
                validator_hotkey=_VALIDATOR,
                policy=_policy(),
            )
    assert (await _lease(session, issued)).status == "issued"
    assert (await _lease(session, claimed)).status == "claimed"


async def test_tightening_aborts_unlisted_leases_and_locks_only_refused_rows(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    held = await _qualified_agent(session, evidence="44", admit=(_VALIDATOR,))
    held_lease = await _issue(session, held)
    canary = await _qualified_agent(session, admit=(_VALIDATOR,))
    other = await _qualified_agent(session, evidence="22", admit=(_VALIDATOR,))
    canary_lease = await _issue_and_claim(session, canary)
    other_lease = await _issue_and_claim(session, other)
    canary_grant = await _live_grant(session, canary_lease)
    other_grant = await _live_grant(session, other_lease)
    pending_agent = await _qualified_agent(session, evidence="33", admit=(_VALIDATOR,))
    pending_lease = await _issue(session, pending_agent)
    stamped_before = (await _lease(session, canary_lease)).claim_allowlist_revision

    async with session_maker() as holder, holder.begin():
        # Hold a listed in-flight lease and the listed canary grant. A
        # tightening that locked every live row and filtered in Python would
        # wait here and time out. (The listed claimed canary lease itself is
        # re-stamped, so it is locked by design and not held here.)
        await holder.execute(
            select(CodingCertificationLease)
            .where(CodingCertificationLease.lease_id == held_lease)
            .with_for_update()
        )
        await holder.execute(
            select(CodingCertificationInferenceGrant)
            .where(CodingCertificationInferenceGrant.grant_id == canary_grant)
            .with_for_update()
        )
        async with session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '3s'"))
            current = await session.scalar(
                select(func.max(CodingCertificationAllowlistRevision.revision))
            )
            row = await insert_coding_certification_allowlist_revision(
                session,
                expected_revision=int(current or 0),
                enabled=True,
                entries=[_entry(canary), _entry(held)],
                reason="restrict certification to the team canary",
                actor="operator@example.com",
            )
            allowlist = await active_coding_certification_allowlist(session)
            aborted = await abort_unlisted_coding_certification_leases(
                session, allowlist=allowlist
            )
            restamped = await restamp_admitted_coding_certification_leases(
                session, allowlist=allowlist
            )
            revoked = await revoke_unlisted_coding_certification_inference_grants(
                session, allowlist=allowlist
            )
    assert (aborted, restamped, revoked) == (2, 1, 0)

    assert (await _grant(session, canary_grant)).status == "active"
    canary_row = await _lease(session, canary_lease)
    assert canary_row.status == "claimed"
    # The relay admits inference only while this stamp is the latest revision.
    assert stamped_before is not None and stamped_before < row.revision
    assert canary_row.claim_allowlist_revision == row.revision
    assert (await _lease(session, held_lease)).status == "issued"
    other_row = await _lease(session, other_lease)
    assert other_row.status == "aborted"
    assert other_row.aborted_allowlist_revision == row.revision
    assert other_row.claimed_at is not None and other_row.aborted_at is not None
    other_revoked = await _grant(session, other_grant)
    assert other_revoked.status == "revoked" and other_revoked.bearer_digest is None
    pending_row = await _lease(session, pending_lease)
    assert pending_row.status == "aborted" and pending_row.claimed_at is None

    # The identity is free again, but only for an admitted tuple.
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=other.agent_id,
                bench_version=_BENCH_VERSION,
            )

    # A grant left live on a no-longer-in-flight lease is still revoked in SQL.
    async with session.begin():
        await session.execute(
            update(CodingCertificationInferenceGrant)
            .where(CodingCertificationInferenceGrant.grant_id == other_grant)
            .values(status="pending", generation=0, revoked_at=None)
        )
    assert await _set_allowlist(session, [_entry(canary)]) == (1, 1)

    async with session.begin():
        replay = await ensure_coding_certification_inference_grant(
            session,
            lease_id=canary_lease,
            validator_hotkey=_VALIDATOR,
            policy=_policy(),
        )
    assert replay.idempotent is True and replay.grant.status == "active"

    async with session.begin():
        page = await list_coding_certification_leases(
            session,
            agent_id=None,
            validator_hotkey=_VALIDATOR,
            status=None,
            limit=2,
            offset=0,
        )
        tail = await list_coding_certification_leases(
            session,
            agent_id=None,
            validator_hotkey=_VALIDATOR,
            status=CodingCertificationLeaseStatus.ABORTED,
            limit=5,
            offset=0,
        )
    assert page.total == 4 and len(page.rows) == 2
    assert [row.lease.lease_id for row in page.rows] == [pending_lease, other_lease]
    assert [row.inference_grant_status for row in page.rows] == [None, "revoked"]
    assert tail.total == 3
    assert page.now.tzinfo is not None


async def test_allowlist_revisions_are_append_only_revisioned_and_fail_closed(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session, admit=())
    agent_id = agent.agent_id
    await _set_allowlist(session, [_entry(agent)])
    # Rolled-back transactions below expire ``agent``; keep a detached copy.
    snapshot = cast(
        Agent, SimpleNamespace(agent_id=agent.agent_id, sha256=agent.sha256)
    )

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

    # A revision whose checksum does not bind its entries never widens access,
    # and reads back as invalid rather than as the entries it stores.
    await _append_corrupt_revision(session, snapshot)
    async with session.begin():
        allowlist = await active_coding_certification_allowlist(session)
        stored = await session.scalar(
            select(CodingCertificationAllowlistRevision).where(
                CodingCertificationAllowlistRevision.revision == 2
            )
        )
        assert stored is not None
        shown = allowlist_revision_from_row(stored)
    assert allowlist.revision == 2 and allowlist.tuples == frozenset()
    assert shown.enabled is True
    assert shown.integrity == "invalid" and shown.effective == "refuse_all"
    assert shown.entries == []
    async with session.begin():
        with pytest.raises(CodingCertificationAllowlistRefusedError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent_id,
                bench_version=_BENCH_VERSION,
            )
    assert await _lease_count(session) == 0


async def test_receipted_lease_is_terminal_and_its_identity_never_reissues(
    session: AsyncSession,
) -> None:
    agent = await _qualified_agent(session)
    lease_id = await _issue_and_claim(session, agent)
    await _record_receipt(session, lease_id)
    await _backdate(session, lease_id)

    stored = await _lease(session, lease_id)
    assert stored.status == CodingCertificationLeaseStatus.COMPLETED.value
    async with session.begin():
        now = await database_now(session)
    assert lease_is_due(stored, now=now) is False

    for operation in (
        claim_coding_certification_lease,
        abort_coding_certification_lease,
    ):
        async with session.begin():
            with pytest.raises(CodingCertificationLeaseNotAvailableError):
                await operation(session, validator_hotkey=_VALIDATOR, lease_id=lease_id)
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseNotAvailableError):
            await authorize_coding_certification_harness_delivery(
                session, lease_id=lease_id, validator_hotkey=_VALIDATOR
            )
    # The receipt is terminal for the identity, whichever validator asks.
    for validator in (_VALIDATOR, _OTHER_VALIDATOR):
        async with session.begin():
            with pytest.raises(
                CodingCertificationLeaseNotAvailableError, match="terminal receipt"
            ):
                await issue_coding_certification_lease(
                    session,
                    validator_hotkey=validator,
                    agent_id=agent.agent_id,
                    bench_version=_BENCH_VERSION,
                )
    # Tightening never rewrites a completed lease either.
    assert await _set_allowlist(session, [], enabled=False) == (0, 0)

    assert await _lease_count(session) == 1
    final = await _lease(session, lease_id)
    assert final.status == "completed" and final.claimed_at is not None
    async with session.begin():
        page = await list_coding_certification_leases(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=None,
            status=CodingCertificationLeaseStatus.COMPLETED,
            limit=5,
            offset=0,
        )
    assert page.total == 1 and page.rows[0].receipt_status == "failed"

    # A lease that ended without a receipt does not burn a different identity.
    fresh = await _qualified_agent(session, evidence="22")
    lost = await _issue_and_claim(session, fresh)
    await _backdate(session, lost)
    assert await _issue(session, fresh) != lost


async def test_gate_reads_the_database_clock_after_waiting_for_the_lease_lock(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent = await _qualified_agent(session)
    lease_id = await _issue_and_claim(session, agent)

    async with session_maker() as holder:
        await holder.begin()
        await holder.execute(
            select(CodingCertificationLease)
            .where(CodingCertificationLease.lease_id == lease_id)
            .with_for_update()
        )
        before = await database_now(holder)

        async def gated() -> datetime:
            async with session.begin():
                gate = await lock_coding_certification_lease(
                    session, lease_id=lease_id, validator_hotkey=_VALIDATOR
                )
                return gate.now

        waiting = asyncio.create_task(gated())
        await asyncio.sleep(1.2)
        assert not waiting.done()
        await holder.rollback()
        observed = await waiting
    assert observed - before >= timedelta(seconds=1)


async def test_refused_issue_is_decided_before_locking_the_agent_row(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent = await _qualified_agent(session, admit=())
    agent_id = agent.agent_id
    async with session_maker() as holder, holder.begin():
        await holder.execute(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        async with session.begin():
            # Taking the agent row lock first would wait on the holder and
            # time out instead of refusing.
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            with pytest.raises(CodingCertificationAllowlistRefusedError):
                await issue_coding_certification_lease(
                    session,
                    validator_hotkey=_VALIDATOR,
                    agent_id=agent_id,
                    bench_version=_BENCH_VERSION,
                )
    assert await _lease_count(session) == 0
