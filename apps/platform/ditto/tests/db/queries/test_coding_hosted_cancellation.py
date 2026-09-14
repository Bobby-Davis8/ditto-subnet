"""Bounded, append-only cancellation of unstarted hosted Coding assignments."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hosted_inference import HostedInferenceLedger
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedAssignmentCancellation,
    CodingHostedInferenceGrant,
    CodingHostedPrivateTask,
)
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    HostedAdmissionView,
    create_hosted_assignment,
    start_hosted_attempt,
)
from ditto.db.queries.coding_hosted_operations import (
    HostedAssignmentNotFoundError,
    HostedCancellation,
    HostedCancellationError,
    cancel_hosted_assignment,
    get_hosted_assignment_detail,
    hosted_operation_state,
    list_hosted_assignment_summaries,
)
from ditto.db.queries.coding_hosted_private import bind_hosted_private_task
from ditto.tests.api_server.endpoints.test_admin_coding_private_v2_releases import (
    _publication_receipt,
    _registration,
)
from ditto.tests.api_server.test_coding_hosted_inference import native_policy
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request, _seed
from ditto.tests.db.queries.test_coding_hosted_private import _prepared, _store

REASON = "operator cancelled the unstarted canary assignment"
ACTOR = "peyton@omniaura.ai"


async def _cancel(maker, authority, *, reason=REASON, actor=ACTOR, digest=None):
    async with maker() as session, session.begin():
        return await cancel_hosted_assignment(
            session,
            evaluation_id=authority.evaluation_id,
            expected_assignment_sha256=digest or authority.digest(),
            actor=actor,
            reason=reason,
        )


async def _start(maker, authority, worker=None):
    async with maker() as session, session.begin():
        return await start_hosted_attempt(
            session,
            evaluation_id=authority.evaluation_id,
            expected_attempt_id=authority.attempt_id,
            worker_id=worker or uuid4(),
        )


async def _snapshot(maker, authority):
    async with maker() as session:
        assignment = await session.get(CodingHostedAssignment, authority.evaluation_id)
        task = await session.get(CodingHostedPrivateTask, authority.evaluation_id)
        cancellation = await session.get(
            CodingHostedAssignmentCancellation, authority.evaluation_id
        )
        return assignment, task, cancellation


def _profile():
    return coding_canonical_json_bytes(
        {
            "schema": "dittobench-coding-hosted-authoring-profile-v2",
            "budgets": {
                "model_input_tokens": 200,
                "model_output_tokens": 200,
                "workspace_tool_calls": 5,
                "wall_time_seconds": 600,
            },
        },
        maximum_bytes=16384,
        label="synthetic profile",
    )


async def test_pending_assignment_cancel_blocks_admission_and_binding(session_maker):
    authority, selection, _, _ = await _prepared(session_maker, start=False, bind=False)
    before, _, _ = await _snapshot(session_maker, authority)

    result = await _cancel(session_maker, authority)
    assert not result.idempotent and not result.private_task_closed
    assert result.row.prior_state == "pending_admission"
    assert result.row.assignment_sha256 == authority.digest()
    assert (result.row.reason, result.row.actor) == (REASON, ACTOR)

    with pytest.raises(HostedAdmissionError, match="cancelled"):
        await _admit(session_maker, _request(authority))
    with pytest.raises(HostedAdmissionError, match="cancelled"):
        await _admit(session_maker, _request(authority, operation="status"))
    with pytest.raises(HostedAdmissionError, match="cancelled"):
        async with session_maker() as session, session.begin():
            await bind_hosted_private_task(session, selection=selection)
    with pytest.raises(HostedAdmissionError, match="cancelled"):
        async with session_maker() as session, session.begin():
            await create_hosted_assignment(
                session,
                authority=authority,
                confirmed_assignment_sha256=authority.digest(),
                actor="test",
                reason="synthetic shadow test",
            )
    # PostgreSQL refuses a private task bound behind the Python guard's back.
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                insert(CodingHostedPrivateTask).values(
                    evaluation_id=authority.evaluation_id,
                    selection_sha256=selection.digest(),
                    selection_authority=selection.projection(),
                    catalog_index=selection.catalog_index,
                    max_patch_bytes=selection.max_patch_bytes,
                    authoring_grant_id=uuid4(),
                    grading_grant_id=uuid4(),
                )
            )
    after, task, cancellation = await _snapshot(session_maker, authority)
    assert task is None and cancellation is not None
    # The assignment row is retained exactly as approved.
    assert after is not None and before is not None
    assert (after.authority, after.assignment_sha256, after.admitted_at) == (
        before.authority,
        before.assignment_sha256,
        None,
    )


async def test_admitted_unstarted_cancel_closes_task_and_blocks_every_authority(
    session_maker,
):
    policy = native_policy()
    profile = _profile()
    authority, _, _, grants = await _prepared(
        session_maker,
        start=False,
        policy_sha256=policy.digest(),
        execution_profile_sha256=hashlib.sha256(profile).hexdigest(),
    )
    await _admit(session_maker, _request(authority))

    result = await _cancel(session_maker, authority)
    assert result.row.prior_state == "admitted" and result.private_task_closed
    assignment, task, _ = await _snapshot(session_maker, authority)
    assert assignment is not None and assignment.admitted_at is not None
    assert assignment.started_at is None and assignment.worker_id is None
    assert task is not None and task.close_reason == "aborted"
    assert task.closed_at is not None and task.frozen_at is None

    worker = uuid4()
    with pytest.raises(HostedAdmissionError, match="cancelled"):
        await _start(session_maker, authority, worker)
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )
    ledger = HostedInferenceLedger(sessions=session_maker, worker_id=worker)
    with pytest.raises(HostedAdmissionError, match="cancelled"):
        await ledger.issue(
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            assignment_sha256=authority.digest(),
            policy=policy,
            execution_profile=profile,
        )
    # The database independently refuses the start boundary and an inference
    # grant even if a future caller bypasses the query layer.
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(CodingHostedAssignment)
                .where(CodingHostedAssignment.evaluation_id == authority.evaluation_id)
                .values(started_at=func.clock_timestamp(), worker_id=worker)
            )
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                insert(CodingHostedInferenceGrant).values(
                    grant_id=uuid4(),
                    evaluation_id=authority.evaluation_id,
                    attempt_id=authority.attempt_id,
                    worker_id=worker,
                    assignment_sha256=authority.digest(),
                    policy_sha256=policy.digest(),
                    execution_profile_sha256=hashlib.sha256(profile).hexdigest(),
                    policy=policy.model_dump(mode="json", by_alias=True),
                    request_limit=1,
                    prompt_limit=1,
                    completion_limit=1,
                    cost_limit=1,
                    expires_at=now.replace(year=now.year + 1),
                    shadow_only=True,
                    weight_eligible=False,
                )
            )
    assignment, _, _ = await _snapshot(session_maker, authority)
    assert assignment is not None and assignment.started_at is None


async def test_started_attempt_is_refused_by_query_and_database(session_maker):
    authority, _, worker, _ = await _prepared(session_maker)
    with pytest.raises(HostedCancellationError, match="started"):
        await _cancel(session_maker, authority)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                insert(CodingHostedAssignmentCancellation).values(
                    evaluation_id=authority.evaluation_id,
                    assignment_sha256=authority.digest(),
                    prior_state="admitted",
                    reason=REASON,
                    actor=ACTOR,
                )
            )
    assignment, task, cancellation = await _snapshot(session_maker, authority)
    assert cancellation is None
    assert assignment is not None and assignment.worker_id == worker
    assert task is not None and task.closed_at is None


async def test_replay_is_idempotent_and_conflicts_never_rewrite(session_maker):
    authority, _, _, _ = await _prepared(session_maker, start=False)
    first = await _cancel(session_maker, authority)
    replay = await _cancel(session_maker, authority, reason=f"  {REASON} ")
    assert replay.idempotent and not replay.private_task_closed
    assert replay.row.cancelled_at == first.row.cancelled_at
    for changes in (
        {"reason": "a different operator reason"},
        {"actor": "brian@omniaura.ai"},
        {"digest": "f" * 64},
    ):
        with pytest.raises(HostedCancellationError):
            await _cancel(session_maker, authority, **changes)
    with pytest.raises(HostedAssignmentNotFoundError):
        await _cancel(session_maker, replace(authority, evaluation_id=uuid4()))
    for audit in ({"reason": "short"}, {"actor": "   "}):
        with pytest.raises(HostedCancellationError, match="audit"):
            await _cancel(session_maker, authority, **audit)
    _, _, cancellation = await _snapshot(session_maker, authority)
    assert cancellation is not None
    assert (cancellation.reason, cancellation.actor) == (REASON, ACTOR)
    assert cancellation.cancelled_at == first.row.cancelled_at


async def test_cancellation_and_assignment_rows_are_append_only(session_maker):
    authority, _, _, _ = await _prepared(session_maker, start=False)
    await _cancel(session_maker, authority)
    table = CodingHostedAssignmentCancellation
    for statement in (
        update(table)
        .where(table.evaluation_id == authority.evaluation_id)
        .values(reason="rewritten cancellation reason"),
        delete(table).where(table.evaluation_id == authority.evaluation_id),
        delete(CodingHostedPrivateTask).where(
            CodingHostedPrivateTask.evaluation_id == authority.evaluation_id
        ),
        delete(CodingHostedAssignment).where(
            CodingHostedAssignment.evaluation_id == authority.evaluation_id
        ),
        update(CodingHostedAssignment)
        .where(CodingHostedAssignment.evaluation_id == authority.evaluation_id)
        .values(admitted_at=func.clock_timestamp(), admission_request_sha256="e" * 64),
    ):
        with pytest.raises(IntegrityError):
            async with session_maker() as session, session.begin():
                await session.execute(statement)
    assignment, task, cancellation = await _snapshot(session_maker, authority)
    assert assignment is not None and assignment.admitted_at is None
    assert task is not None and task.close_reason == "aborted"
    assert cancellation is not None and cancellation.reason == REASON
    async with session_maker() as session:
        triggers = set(
            (
                await session.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                        "AND tgenabled <> 'D' AND tgname LIKE '%cancellation%'"
                    )
                )
            ).scalars()
        )
    assert triggers == {
        "coding_hosted_assignment_cancellations_insert",
        "coding_hosted_assignment_cancellations_immutable",
        "coding_hosted_assignments_cancellation_guard",
        "coding_hosted_private_tasks_cancellation_guard",
    }


async def test_concurrent_cancel_and_start_cross_exactly_one_boundary(session_maker):
    receipt = _publication_receipt(Ed25519PrivateKey.generate())
    bundle = (_registration(receipt), receipt)
    for _ in range(4):
        authority, _, _, _ = await _prepared(
            session_maker, start=False, registration_bundle=bundle
        )
        await _admit(session_maker, _request(authority))
        results = await asyncio.gather(
            _cancel(session_maker, authority),
            _start(session_maker, authority),
            return_exceptions=True,
        )
        cancelled, started = results
        assignment, task, cancellation = await _snapshot(session_maker, authority)
        assert assignment is not None and task is not None
        if isinstance(cancelled, HostedCancellation):
            assert isinstance(started, HostedAdmissionError)
            assert cancellation is not None and assignment.started_at is None
            assert task.close_reason == "aborted"
        else:
            assert isinstance(cancelled, HostedCancellationError)
            assert isinstance(started, HostedAdmissionView) and started.newly_started
            assert cancellation is None and assignment.started_at is not None
            assert task.closed_at is None


async def test_expired_unstarted_assignment_is_visible_and_still_cancellable(
    session_maker,
):
    authority = await _seed(session_maker, approve=False)
    async with session_maker() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
    authority = replace(authority, deadline_unix=int(now.timestamp()) + 2)
    async with session_maker() as session, session.begin():
        await create_hosted_assignment(
            session,
            authority=authority,
            confirmed_assignment_sha256=authority.digest(),
            actor="test",
            reason="synthetic expiring approval",
        )
    await asyncio.sleep(2.2)
    async with session_maker() as session:
        expired = await get_hosted_assignment_detail(
            session, evaluation_id=authority.evaluation_id
        )
    assert expired.summary.state == "expired"
    assert expired.summary.assignment.started_at is None
    assert expired.observed_at >= expired.summary.assignment.expires_at
    with pytest.raises(HostedAdmissionError, match="expired"):
        await _admit(session_maker, _request(authority))
    result = await _cancel(session_maker, authority)
    assert result.row.prior_state == "pending_admission"
    async with session_maker() as session:
        cancelled = await get_hosted_assignment_detail(
            session, evaluation_id=authority.evaluation_id
        )
        summaries, total, _ = await list_hosted_assignment_summaries(
            session, limit=1, offset=0
        )
    assert cancelled.summary.state == "cancelled"
    assert cancelled.cancellation is not None
    assert total == 1 and summaries[0].state == "cancelled"
    closed = {
        "started_at": None,
        "admitted_at": None,
        "expires_at": expired.summary.assignment.expires_at,
        "closed_at": expired.observed_at,
        "close_reason": "aborted",
        "now": expired.observed_at,
    }
    # Cancellation outranks the close reason; a close outranks expiry.
    assert hosted_operation_state(**closed, cancelled=True) == "cancelled"
    assert hosted_operation_state(**closed, cancelled=False) == "aborted"
