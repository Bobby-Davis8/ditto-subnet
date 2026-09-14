"""Trusted operator path that creates and binds one hosted-v2 shadow assignment.

The operator names a subject (agent, registered release, catalog index, validator and
approved profile digests). The artifact digest, screened image digest, registration
digest, bench version, schedule commitment and selection are derived here from locked
Platform state, never accepted from the request. Preview returns the exact authority;
create re-derives it and requires the operator to confirm that digest.

Cancel appends one immutable cancellation for an assignment whose attempt never
started; a started attempt is stopped only by its worker's abort path. The list and
detail reads return lifecycle, digests, terminal outcome, inference accounting and
result delivery status without private task, grading or settlement contents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES
from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted_assignment_admin import (
    AdminHostedAssignmentCancellationRecord,
    AdminHostedAssignmentCancelled,
    AdminHostedAssignmentCancelRequest,
    AdminHostedAssignmentCreated,
    AdminHostedAssignmentCreateRequest,
    AdminHostedAssignmentDetail,
    AdminHostedAssignmentList,
    AdminHostedAssignmentPlan,
    AdminHostedAssignmentPreviewRequest,
    AdminHostedAssignmentSubject,
    AdminHostedAssignmentSummary,
    AdminHostedInferenceAccounting,
    AdminHostedPrivateTaskStatus,
    AdminHostedResultDelivery,
    AdminHostedTerminalStatus,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.coding_hosted_private import HostedTaskSelection
from ditto.db.models import (
    Agent,
    CodingHostedAssignmentCancellation,
    CodingPrivateV2Release,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.coding_certifications import active_validator_coding_certification
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    HostedAssignmentAuthority,
    _now,
    create_hosted_assignment,
)
from ditto.db.queries.coding_hosted_operations import (
    HostedAssignmentDetail,
    HostedAssignmentNotFoundError,
    HostedAssignmentSummary,
    HostedCancellationError,
    cancel_hosted_assignment,
    get_hosted_assignment_detail,
    list_hosted_assignment_summaries,
)
from ditto.db.queries.coding_hosted_private import (
    HostedPrivateTaskError,
    bind_hosted_private_task,
)

router = APIRouter(prefix="/admin/coding-hosted-assignments", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

CODING_CONTRACT_VERSION = 2
# The only supported capability certification lease, receipt and persistence path
# is typed to contract v1 (coding_certification.py, coding_certifications.py), so
# a hosted-v2 subject is gated on its current v1 capability certification.
CERTIFICATION_CONTRACT_VERSION = 1


def canary_schedule_sha256(
    *,
    registration_sha256: str,
    catalog_index: int,
    evaluation_id: UUID,
    attempt_id: UUID,
) -> str:
    """Single-arm schedule commitment: the operator chose exactly this one index."""

    return coding_canonical_sha256(
        {
            "schema": "dittobench-coding-hosted-canary-schedule-v1",
            "coding_contract_version": CODING_CONTRACT_VERSION,
            "shadow_only": True,
            "weight_eligible": False,
            "registration_sha256": registration_sha256,
            "catalog_index": catalog_index,
            "evaluation_id": str(evaluation_id),
            "attempt_id": str(attempt_id),
        },
        maximum_bytes=4096,
        label="hosted canary schedule",
    )


def _confirmation(evaluation_id: UUID, assignment_sha256: str) -> str:
    return f"CREATE SHADOW CODING HOSTED ASSIGNMENT {evaluation_id} {assignment_sha256}"


def _cancel_confirmation(evaluation_id: UUID, assignment_sha256: str) -> str:
    return f"CANCEL SHADOW CODING HOSTED ASSIGNMENT {evaluation_id} {assignment_sha256}"


async def _plan(
    session: AsyncSession,
    subject: AdminHostedAssignmentSubject,
    *,
    evaluation_id: UUID,
    attempt_id: UUID,
    deadline_unix: int,
) -> tuple[AdminHostedAssignmentPlan, HostedAssignmentAuthority, HostedTaskSelection]:
    release = await session.get(CodingPrivateV2Release, subject.release_row_id)
    if release is None:
        raise HTTPException(status_code=404, detail="private v2 release not found")
    agent = await session.get(Agent, subject.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if (
        agent.screened_image_sha256 is None
        or agent.status not in SCOREABLE_AGENT_STATUSES
    ):
        raise HTTPException(
            status_code=409, detail="agent has no scoreable screened image"
        )
    now = await _now(session)
    deadline = datetime.fromtimestamp(deadline_unix, UTC)
    if not now < deadline or deadline - now > timedelta(hours=1):
        raise HTTPException(status_code=409, detail="assignment deadline is invalid")
    bench_version = await active_bench_version(session)
    certification = await active_validator_coding_certification(
        session,
        agent=agent,
        validator_hotkey=subject.validator_hotkey,
        bench_version=bench_version,
        coding_contract_version=CERTIFICATION_CONTRACT_VERSION,
        active_through=deadline,
    )
    if certification is None:
        raise HTTPException(
            status_code=409,
            detail="agent lacks an active coding certification through the deadline",
        )
    schedule_sha256 = canary_schedule_sha256(
        registration_sha256=release.registration_sha256,
        catalog_index=subject.catalog_index,
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
    )
    selection = HostedTaskSelection(
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
        registration_sha256=release.registration_sha256,
        artifact_sha256=agent.sha256,
        schedule_sha256=schedule_sha256,
        catalog_index=subject.catalog_index,
        max_patch_bytes=subject.max_patch_bytes,
    )
    authority = HostedAssignmentAuthority(
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
        release_row_id=subject.release_row_id,
        registration_sha256=release.registration_sha256,
        agent_id=subject.agent_id,
        validator_hotkey=subject.validator_hotkey,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        selection_sha256=selection.digest(),
        policy_sha256=subject.policy_sha256,
        execution_profile_sha256=subject.execution_profile_sha256,
        grading_profile_sha256=subject.grading_profile_sha256,
        deadline_unix=deadline_unix,
    )
    try:
        projection = authority.projection()
        selection_projection = selection.projection()
    except (HostedAdmissionError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    digest = authority.digest()
    plan = AdminHostedAssignmentPlan(
        evaluation_id=evaluation_id,
        attempt_id=attempt_id,
        deadline_unix=deadline_unix,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        registration_sha256=release.registration_sha256,
        bench_version=bench_version,
        certification_row_id=certification.certification_row_id,
        schedule_sha256=schedule_sha256,
        selection_sha256=selection.digest(),
        selection=selection_projection,
        assignment_sha256=digest,
        authority=projection,
        confirmation=_confirmation(evaluation_id, digest),
    )
    return plan, authority, selection


@router.post("/preview", response_model=AdminHostedAssignmentPlan)
async def preview_hosted_assignment(
    payload: AdminHostedAssignmentPreviewRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminHostedAssignmentPlan:
    """Derive the exact authority without writing anything."""

    response.headers["Cache-Control"] = "no-store"
    now = await _now(session)
    plan, _, _ = await _plan(
        session,
        payload,
        evaluation_id=uuid4(),
        attempt_id=uuid4(),
        deadline_unix=int(now.timestamp()) + payload.lease_seconds,
    )
    return plan


@router.post("", response_model=AdminHostedAssignmentCreated)
async def create_hosted_assignment_endpoint(
    payload: AdminHostedAssignmentCreateRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminHostedAssignmentCreated:
    """Re-derive the previewed authority, then create and bind it atomically."""

    response.headers["Cache-Control"] = "no-store"
    async with session.begin():
        plan, authority, selection = await _plan(
            session,
            payload,
            evaluation_id=payload.evaluation_id,
            attempt_id=payload.attempt_id,
            deadline_unix=payload.deadline_unix,
        )
        if (
            payload.confirmed_assignment_sha256 != plan.assignment_sha256
            or payload.confirmation != plan.confirmation
        ):
            raise HTTPException(
                status_code=422,
                detail=f'confirmation must equal "{plan.confirmation}"',
            )
        try:
            await create_hosted_assignment(
                session,
                authority=authority,
                confirmed_assignment_sha256=plan.assignment_sha256,
                actor=payload.actor,
                reason=payload.reason,
            )
            grants = await bind_hosted_private_task(session, selection=selection)
        except (HostedAdmissionError, HostedPrivateTaskError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    return AdminHostedAssignmentCreated(
        **plan.model_dump(),
        authoring_grant_id=grants.authoring_grant_id,
        grading_grant_id=grants.grading_grant_id,
    )


def _cancellation_record(
    row: CodingHostedAssignmentCancellation,
) -> AdminHostedAssignmentCancellationRecord:
    return AdminHostedAssignmentCancellationRecord.model_validate(
        {
            "assignment_sha256": row.assignment_sha256,
            "prior_state": row.prior_state,
            "reason": row.reason,
            "actor": row.actor,
            "cancelled_at": row.cancelled_at,
        }
    )


def _summary_fields(summary: HostedAssignmentSummary) -> dict[str, object]:
    row = summary.assignment
    return {
        "evaluation_id": row.evaluation_id,
        "attempt_id": row.attempt_id,
        "release_row_id": row.release_row_id,
        "registration_sha256": row.registration_sha256,
        "agent_id": row.agent_id,
        "validator_hotkey": row.validator_hotkey,
        "artifact_sha256": row.artifact_sha256,
        "screened_image_sha256": row.screened_image_sha256,
        "assignment_sha256": row.assignment_sha256,
        "state": summary.state,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "admitted_at": row.admitted_at,
        "started_at": row.started_at,
        "cancelled_at": summary.cancelled_at,
        "closed_at": summary.closed_at,
        "close_reason": summary.close_reason,
        "terminal_outcome": summary.terminal_outcome,
        "acknowledged": summary.acknowledged,
        "registered_actor": row.actor,
        "registered_reason": row.reason,
    }


def _detail(detail: HostedAssignmentDetail) -> AdminHostedAssignmentDetail:
    summary = detail.summary
    row = summary.assignment
    inference = detail.inference
    return AdminHostedAssignmentDetail.model_validate(
        {
            **_summary_fields(summary),
            "observed_at": detail.observed_at,
            "deadline_unix": row.authority["deadline_unix"],
            "selection_sha256": row.authority["selection_sha256"],
            "policy_sha256": row.authority["policy_sha256"],
            "execution_profile_sha256": row.authority["execution_profile_sha256"],
            "grading_profile_sha256": row.authority["grading_profile_sha256"],
            "admission_request_sha256": row.admission_request_sha256,
            "cancellable": row.started_at is None and detail.cancellation is None,
            "cancellation": (
                _cancellation_record(detail.cancellation)
                if detail.cancellation is not None
                else None
            ),
            "private_task": (
                AdminHostedPrivateTaskStatus.model_validate(
                    {
                        "bound_at": detail.private_task.bound_at,
                        "selection_sha256": detail.private_task.selection_sha256,
                        "frozen_at": detail.private_task.frozen_at,
                        "closed_at": detail.private_task.closed_at,
                        "close_reason": detail.private_task.close_reason,
                    }
                )
                if detail.private_task is not None
                else None
            ),
            "authoring_evidence_reserved_at": detail.authoring_evidence_reserved_at,
            "authoring_evidence_finalized_at": detail.authoring_evidence_finalized_at,
            "grading_claimed_at": detail.grading_claimed_at,
            "terminal": (
                AdminHostedTerminalStatus(
                    outcome=detail.terminal.outcome,
                    evidence_sha256=detail.terminal.evidence_sha256,
                    reserved_at=detail.terminal.reserved_at,
                    finalized_at=detail.terminal.finalized_at,
                )
                if detail.terminal is not None
                else None
            ),
            "inference": (
                AdminHostedInferenceAccounting(
                    policy_sha256=inference.policy_sha256,
                    issued_at=inference.issued_at,
                    expires_at=inference.expires_at,
                    revoked_at=inference.revoked_at,
                    request_limit=inference.request_limit,
                    prompt_token_limit=inference.prompt_token_limit,
                    completion_token_limit=inference.completion_token_limit,
                    cost_usd_micros_limit=inference.cost_usd_micros_limit,
                    request_count=inference.request_count,
                    reserved_count=inference.reserved_count,
                    settled_count=inference.settled_count,
                    uncertain_count=inference.uncertain_count,
                    charged_prompt_tokens=inference.charged_prompt_tokens,
                    charged_completion_tokens=inference.charged_completion_tokens,
                    charged_cost_usd_micros=inference.charged_cost_usd_micros,
                    settled_prompt_tokens=inference.settled_prompt_tokens,
                    settled_completion_tokens=inference.settled_completion_tokens,
                    settled_cost_usd_micros=inference.settled_cost_usd_micros,
                    verified=inference.verified,
                )
                if inference is not None
                else None
            ),
            "delivery_count": detail.delivery_count,
            "acknowledged_count": detail.acknowledged_count,
            "deliveries": [
                AdminHostedResultDelivery(
                    result_sha256=delivery.result_sha256,
                    delivered_at=delivery.delivered_at,
                    acknowledged_at=delivery.acknowledged_at,
                )
                for delivery in detail.deliveries
            ],
            "deliveries_truncated": detail.delivery_count > len(detail.deliveries),
        }
    )


@router.get("", response_model=AdminHostedAssignmentList)
async def list_hosted_assignments(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminHostedAssignmentList:
    """Page redacted hosted assignment lifecycles, newest first."""

    response.headers["Cache-Control"] = "no-store"
    summaries, total, now = await list_hosted_assignment_summaries(
        session, limit=limit, offset=offset
    )
    return AdminHostedAssignmentList(
        total=total,
        limit=limit,
        offset=offset,
        observed_at=now,
        assignments=[
            AdminHostedAssignmentSummary.model_validate(_summary_fields(summary))
            for summary in summaries
        ],
    )


@router.get("/{evaluation_id}", response_model=AdminHostedAssignmentDetail)
async def get_hosted_assignment(
    evaluation_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminHostedAssignmentDetail:
    """Read one lifecycle, terminal outcome, accounting and delivery status."""

    response.headers["Cache-Control"] = "no-store"
    try:
        detail = await get_hosted_assignment_detail(
            session, evaluation_id=evaluation_id
        )
    except HostedAssignmentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _detail(detail)


@router.post("/{evaluation_id}/cancel", response_model=AdminHostedAssignmentCancelled)
async def cancel_hosted_assignment_endpoint(
    evaluation_id: UUID,
    payload: AdminHostedAssignmentCancelRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminHostedAssignmentCancelled:
    """Append one cancellation for an assignment whose attempt never started."""

    response.headers["Cache-Control"] = "no-store"
    expected = _cancel_confirmation(evaluation_id, payload.expected_assignment_sha256)
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=422, detail=f'confirmation must equal "{expected}"'
        )
    try:
        async with session.begin():
            result = await cancel_hosted_assignment(
                session,
                evaluation_id=evaluation_id,
                expected_assignment_sha256=payload.expected_assignment_sha256,
                actor=payload.actor,
                reason=payload.reason,
            )
            cancellation = _cancellation_record(result.row)
    except HostedAssignmentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except HostedCancellationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="hosted assignment changed concurrently; re-read before cancelling",
        ) from error
    detail = await get_hosted_assignment_detail(session, evaluation_id=evaluation_id)
    return AdminHostedAssignmentCancelled(
        idempotent=result.idempotent,
        private_task_closed=result.private_task_closed,
        cancellation=cancellation,
        assignment=_detail(detail),
    )
