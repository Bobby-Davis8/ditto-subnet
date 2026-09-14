"""Operator cancellation and redacted lifecycle reads for hosted Coding assignments.

Cancellation is bounded to assignments whose attempt never started. It appends
one immutable row and closes the bound private task with the existing one-way
``aborted`` close; it never deletes or rewrites assignment or evidence rows. A
started attempt owns candidate processes, relays and grants that only the
Platform worker can quiesce and account for, so it is stopped by the worker's
own abort path rather than by an operator database write.

Reads select only non-secret columns: digests, timestamps, outcomes and
accounting totals. Private selections, catalog indices, patch digests, grading
bindings, test counts, sealed-blob coordinates, settlement documents, result
bodies, grant identifiers and worker identifiers are never loaded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_control_plane import CodingHostedOperationState
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedAssignmentCancellation,
    CodingHostedAuthoringFinalization,
    CodingHostedAuthoringReservation,
    CodingHostedGradingClaim,
    CodingHostedInferenceGrant,
    CodingHostedInferenceRequest,
    CodingHostedPrivateTask,
    CodingHostedResultAcknowledgement,
    CodingHostedResultDelivery,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.db.queries.coding_hosted_admission import _now
from ditto.db.queries.coding_hosted_private import _record_close, _task

MAX_LISTED_DELIVERIES = 20
TerminalOutcome = Literal[
    "completed", "candidate_failure", "infrastructure_failure", "integrity_failure"
]


class HostedCancellationError(ValueError):
    """Refused cancellation; carries no private assignment contents."""


class HostedAssignmentNotFoundError(LookupError):
    """No hosted assignment exists for the requested evaluation."""


@dataclass(frozen=True)
class HostedCancellation:
    row: CodingHostedAssignmentCancellation
    idempotent: bool
    private_task_closed: bool


def hosted_operation_state(
    *,
    started_at: datetime | None,
    admitted_at: datetime | None,
    expires_at: datetime,
    closed_at: datetime | None,
    close_reason: str | None,
    cancelled: bool,
    now: datetime,
) -> CodingHostedOperationState:
    """One derivation for every operator view; expiry is never a stored mutation."""
    if cancelled:
        return "cancelled"
    if closed_at is not None:
        if close_reason not in {"completed", "failed", "aborted"}:
            raise ValueError("hosted private task close reason is invalid")
        return cast(CodingHostedOperationState, close_reason)
    if expires_at <= now:
        return "expired"
    if started_at is not None:
        return "running"
    if admitted_at is not None:
        return "admitted"
    return "pending_admission"


async def cancel_hosted_assignment(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    expected_assignment_sha256: str,
    actor: str,
    reason: str,
) -> HostedCancellation:
    """Append one cancellation for an unstarted assignment, inside the caller's txn.

    Lock order is assignment -> task, the same access-removal order as
    ``close_hosted_private_task``. Release and agent locks are never taken, so
    cancellation cannot deadlock admission/start (release -> agent -> assignment)
    and still works after release retirement or artifact drift.
    """
    reason, actor = reason.strip(), actor.strip()
    if len(reason) < 8 or not 1 <= len(actor) <= 120:
        raise HostedCancellationError("hosted cancellation audit is invalid")
    assignment = await session.get(
        CodingHostedAssignment,
        evaluation_id,
        with_for_update=True,
        populate_existing=True,
    )
    if assignment is None:
        raise HostedAssignmentNotFoundError("hosted assignment does not exist")
    if assignment.assignment_sha256 != expected_assignment_sha256:
        raise HostedCancellationError(
            "hosted assignment digest differs; re-read before cancelling"
        )
    existing = await session.get(
        CodingHostedAssignmentCancellation, evaluation_id, populate_existing=True
    )
    if existing is not None:
        if existing.reason != reason or existing.actor != actor:
            raise HostedCancellationError(
                "hosted assignment was already cancelled with different audit"
            )
        return HostedCancellation(existing, idempotent=True, private_task_closed=False)
    if assignment.started_at is not None:
        raise HostedCancellationError(
            "hosted attempt already started; only its worker can abort it"
        )
    session.add(
        CodingHostedAssignmentCancellation(
            evaluation_id=evaluation_id,
            assignment_sha256=assignment.assignment_sha256,
            prior_state=(
                "pending_admission" if assignment.admitted_at is None else "admitted"
            ),
            reason=reason,
            actor=actor,
        )
    )
    await session.flush()
    task = await _task(session, evaluation_id)
    closed = task is not None and await _record_close(session, task, "aborted")
    row = await session.get(
        CodingHostedAssignmentCancellation, evaluation_id, populate_existing=True
    )
    if row is None:  # pragma: no cover - the flush above inserted it
        raise HostedCancellationError("hosted cancellation was not recorded")
    return HostedCancellation(row, idempotent=False, private_task_closed=closed)


@dataclass(frozen=True)
class HostedAssignmentSummary:
    assignment: CodingHostedAssignment
    state: CodingHostedOperationState
    cancelled_at: datetime | None
    closed_at: datetime | None
    close_reason: str | None
    terminal_outcome: TerminalOutcome | None
    acknowledged: bool


@dataclass(frozen=True)
class HostedPrivateTaskStatus:
    bound_at: datetime
    selection_sha256: str
    frozen_at: datetime | None
    closed_at: datetime | None
    close_reason: str | None


@dataclass(frozen=True)
class HostedTerminalStatus:
    outcome: TerminalOutcome
    evidence_sha256: str
    reserved_at: datetime
    finalized_at: datetime | None


@dataclass(frozen=True)
class HostedInferenceTotals:
    policy_sha256: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    request_limit: int
    prompt_token_limit: int
    completion_token_limit: int
    cost_usd_micros_limit: int
    request_count: int
    reserved_count: int
    settled_count: int
    uncertain_count: int
    charged_prompt_tokens: int
    charged_completion_tokens: int
    charged_cost_usd_micros: int
    settled_prompt_tokens: int
    settled_completion_tokens: int
    settled_cost_usd_micros: int

    @property
    def verified(self) -> bool:
        return (
            self.revoked_at is not None
            and self.reserved_count == 0
            and self.uncertain_count == 0
        )


@dataclass(frozen=True)
class HostedDelivery:
    result_sha256: str
    delivered_at: datetime
    acknowledged_at: datetime | None


@dataclass(frozen=True)
class HostedAssignmentDetail:
    summary: HostedAssignmentSummary
    observed_at: datetime
    cancellation: CodingHostedAssignmentCancellation | None
    private_task: HostedPrivateTaskStatus | None
    authoring_evidence_reserved_at: datetime | None
    authoring_evidence_finalized_at: datetime | None
    grading_claimed_at: datetime | None
    terminal: HostedTerminalStatus | None
    inference: HostedInferenceTotals | None
    delivery_count: int
    acknowledged_count: int
    deliveries: tuple[HostedDelivery, ...]


def _summary_query() -> Select:
    acknowledged = (
        select(CodingHostedResultAcknowledgement.result_sha256)
        .join(
            CodingHostedResultDelivery,
            CodingHostedResultDelivery.result_sha256
            == CodingHostedResultAcknowledgement.result_sha256,
        )
        .where(
            CodingHostedResultDelivery.evaluation_id
            == CodingHostedAssignment.evaluation_id
        )
        .exists()
    )
    return (
        select(
            CodingHostedAssignment,
            CodingHostedAssignmentCancellation.cancelled_at,
            CodingHostedPrivateTask.closed_at,
            CodingHostedPrivateTask.close_reason,
            CodingHostedTerminalReservation.identity["outcome"].as_string(),
            acknowledged,
        )
        .outerjoin(
            CodingHostedAssignmentCancellation,
            CodingHostedAssignmentCancellation.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedPrivateTask,
            CodingHostedPrivateTask.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedTerminalReservation,
            CodingHostedTerminalReservation.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
    )


def _summary(row, now: datetime) -> HostedAssignmentSummary:
    assignment, cancelled_at, closed_at, close_reason, outcome, acknowledged = row
    if outcome is not None and outcome not in {
        "completed",
        "candidate_failure",
        "infrastructure_failure",
        "integrity_failure",
    }:
        raise ValueError("hosted terminal outcome is invalid")
    return HostedAssignmentSummary(
        assignment=assignment,
        state=hosted_operation_state(
            started_at=assignment.started_at,
            admitted_at=assignment.admitted_at,
            expires_at=assignment.expires_at,
            closed_at=closed_at,
            close_reason=close_reason,
            cancelled=cancelled_at is not None,
            now=now,
        ),
        cancelled_at=cancelled_at,
        closed_at=closed_at,
        close_reason=close_reason,
        terminal_outcome=cast(TerminalOutcome | None, outcome),
        acknowledged=bool(acknowledged),
    )


async def list_hosted_assignment_summaries(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[list[HostedAssignmentSummary], int, datetime]:
    """Newest first with a stable tie-break so pages never repeat or drop rows."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("hosted assignment page is invalid")
    now = await _now(session)
    total = int(
        await session.scalar(select(func.count()).select_from(CodingHostedAssignment))
        or 0
    )
    rows = (
        await session.execute(
            _summary_query()
            .order_by(
                CodingHostedAssignment.created_at.desc(),
                CodingHostedAssignment.evaluation_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [_summary(row, now) for row in rows], total, now


async def get_hosted_assignment_detail(
    session: AsyncSession, *, evaluation_id: UUID
) -> HostedAssignmentDetail:
    now = await _now(session)
    row = (
        await session.execute(
            _summary_query().where(
                CodingHostedAssignment.evaluation_id == evaluation_id
            )
        )
    ).one_or_none()
    if row is None:
        raise HostedAssignmentNotFoundError("hosted assignment does not exist")
    summary = _summary(row, now)
    cancellation = await session.get(CodingHostedAssignmentCancellation, evaluation_id)

    task_row = (
        await session.execute(
            select(
                CodingHostedPrivateTask.created_at,
                CodingHostedPrivateTask.selection_sha256,
                CodingHostedPrivateTask.frozen_at,
                CodingHostedPrivateTask.closed_at,
                CodingHostedPrivateTask.close_reason,
            ).where(CodingHostedPrivateTask.evaluation_id == evaluation_id)
        )
    ).one_or_none()
    private_task = HostedPrivateTaskStatus(*task_row) if task_row else None

    authoring_reserved_at = await session.scalar(
        select(CodingHostedAuthoringReservation.created_at).where(
            CodingHostedAuthoringReservation.evaluation_id == evaluation_id
        )
    )
    authoring_finalized_at = await session.scalar(
        select(CodingHostedAuthoringFinalization.verified_at).where(
            CodingHostedAuthoringFinalization.evaluation_id == evaluation_id
        )
    )
    grading_claimed_at = await session.scalar(
        select(CodingHostedGradingClaim.created_at).where(
            CodingHostedGradingClaim.evaluation_id == evaluation_id
        )
    )

    terminal_row = (
        await session.execute(
            select(
                CodingHostedTerminalReservation.identity["outcome"].as_string(),
                CodingHostedTerminalReservation.identity_sha256,
                CodingHostedTerminalReservation.created_at,
                CodingHostedTerminalFinalization.verified_at,
            )
            .outerjoin(
                CodingHostedTerminalFinalization,
                CodingHostedTerminalFinalization.evaluation_id
                == CodingHostedTerminalReservation.evaluation_id,
            )
            .where(CodingHostedTerminalReservation.evaluation_id == evaluation_id)
        )
    ).one_or_none()
    terminal = (
        HostedTerminalStatus(
            outcome=cast(TerminalOutcome, summary.terminal_outcome),
            evidence_sha256=terminal_row[1],
            reserved_at=terminal_row[2],
            finalized_at=terminal_row[3],
        )
        if terminal_row is not None
        else None
    )

    inference = await _inference_totals(session, evaluation_id)

    delivery_count, acknowledged_count = (
        await session.execute(
            select(
                func.count(CodingHostedResultDelivery.result_sha256),
                func.count(CodingHostedResultAcknowledgement.result_sha256),
            )
            .select_from(CodingHostedResultDelivery)
            .outerjoin(
                CodingHostedResultAcknowledgement,
                CodingHostedResultAcknowledgement.result_sha256
                == CodingHostedResultDelivery.result_sha256,
            )
            .where(CodingHostedResultDelivery.evaluation_id == evaluation_id)
        )
    ).one()
    deliveries = tuple(
        HostedDelivery(*delivery)
        for delivery in (
            await session.execute(
                select(
                    CodingHostedResultDelivery.result_sha256,
                    CodingHostedResultDelivery.created_at,
                    CodingHostedResultAcknowledgement.acknowledged_at,
                )
                .outerjoin(
                    CodingHostedResultAcknowledgement,
                    CodingHostedResultAcknowledgement.result_sha256
                    == CodingHostedResultDelivery.result_sha256,
                )
                .where(CodingHostedResultDelivery.evaluation_id == evaluation_id)
                .order_by(
                    CodingHostedResultDelivery.created_at.desc(),
                    CodingHostedResultDelivery.result_sha256.desc(),
                )
                .limit(MAX_LISTED_DELIVERIES)
            )
        ).all()
    )
    return HostedAssignmentDetail(
        summary=summary,
        observed_at=now,
        cancellation=cancellation,
        private_task=private_task,
        authoring_evidence_reserved_at=authoring_reserved_at,
        authoring_evidence_finalized_at=authoring_finalized_at,
        grading_claimed_at=grading_claimed_at,
        terminal=terminal,
        inference=inference,
        delivery_count=int(delivery_count),
        acknowledged_count=int(acknowledged_count),
        deliveries=deliveries,
    )


async def _inference_totals(
    session: AsyncSession, evaluation_id: UUID
) -> HostedInferenceTotals | None:
    grant = (
        await session.execute(
            select(
                CodingHostedInferenceGrant.grant_id,
                CodingHostedInferenceGrant.policy_sha256,
                CodingHostedInferenceGrant.created_at,
                CodingHostedInferenceGrant.expires_at,
                CodingHostedInferenceGrant.revoked_at,
                CodingHostedInferenceGrant.request_limit,
                CodingHostedInferenceGrant.prompt_limit,
                CodingHostedInferenceGrant.completion_limit,
                CodingHostedInferenceGrant.cost_limit,
            ).where(CodingHostedInferenceGrant.evaluation_id == evaluation_id)
        )
    ).one_or_none()
    if grant is None:
        return None
    request = CodingHostedInferenceRequest
    settled = request.state == "settled"

    def charged(actual, ceiling):
        # The ledger's conservative rule: a settled request charges its trusted
        # usage, a reserved or uncertain one keeps its full ceiling.
        return func.coalesce(func.sum(case((settled, actual), else_=ceiling)), 0)

    def settled_sum(actual):
        return func.coalesce(func.sum(actual).filter(settled), 0)

    totals = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(request.state == "reserved"),
                func.count().filter(settled),
                func.count().filter(request.state == "uncertain"),
                charged(request.prompt_tokens, request.prompt_ceiling),
                charged(request.completion_tokens, request.completion_ceiling),
                charged(request.cost_usd_micros, request.cost_ceiling),
                settled_sum(request.prompt_tokens),
                settled_sum(request.completion_tokens),
                settled_sum(request.cost_usd_micros),
            ).where(request.grant_id == grant.grant_id)
        )
    ).one()
    return HostedInferenceTotals(
        grant.policy_sha256,
        grant.created_at,
        grant.expires_at,
        grant.revoked_at,
        grant.request_limit,
        grant.prompt_limit,
        grant.completion_limit,
        grant.cost_limit,
        *(int(value) for value in totals),
    )


__all__ = [
    "MAX_LISTED_DELIVERIES",
    "HostedAssignmentDetail",
    "HostedAssignmentNotFoundError",
    "HostedAssignmentSummary",
    "HostedCancellation",
    "HostedCancellationError",
    "cancel_hosted_assignment",
    "get_hosted_assignment_detail",
    "hosted_operation_state",
    "list_hosted_assignment_summaries",
]
