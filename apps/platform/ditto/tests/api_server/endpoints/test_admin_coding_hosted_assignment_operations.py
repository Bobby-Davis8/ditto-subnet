"""Admin cancel and redacted lifecycle views for hosted-v2 shadow assignments."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedAssignmentCancellation,
    CodingHostedAuthoringFinalization,
    CodingHostedAuthoringReservation,
    CodingHostedGradingClaim,
    CodingHostedPrivateTask,
    CodingHostedResultAcknowledgement,
    CodingHostedResultDelivery,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.db.queries.coding_hosted_private import close_hosted_private_task
from ditto.tests.api_server.endpoints.test_admin_coding_hosted_assignments import (
    _fixed_bench_version as _fixed_bench_version,
)
from ditto.tests.api_server.endpoints.test_admin_coding_hosted_assignments import (
    _seed as _seed_subject,
)
from ditto.tests.api_server.endpoints.test_admin_coding_hosted_assignments import (
    _subject,
)
from ditto.tests.api_server.endpoints.test_admin_coding_private_v2_releases import (
    _HEADERS,
    _install,
    _publication_receipt,
    _registration,
)
from ditto.tests.api_server.test_coding_hosted_inference import (
    fixture as inference_fixture,
)
from ditto.tests.api_server.test_coding_hosted_inference import (
    reserve,
    settlement,
)
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request, _seed
from ditto.tests.db.queries.test_coding_hosted_private import _freeze, _prepared

_URL = "/api/v1/admin/coding-hosted-assignments"
_REASON = "operator cancelled the unstarted canary assignment"


def _cancel_body(evaluation_id: UUID, digest: str, **changes: object) -> dict:
    return {
        "expected_assignment_sha256": digest,
        "reason": _REASON,
        "actor": "peyton@omniaura.ai",
        "confirmation": (
            f"CANCEL SHADOW CODING HOSTED ASSIGNMENT {evaluation_id} {digest}"
        ),
        **changes,
    }


async def _count(maker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.asyncio
async def test_views_and_cancel_require_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    authority, _, _, _ = await _prepared(session_maker, start=False)
    evaluation = authority.evaluation_id
    assert (await client.get(_URL)).status_code == 401
    assert (await client.get(f"{_URL}/{evaluation}")).status_code == 401
    cancel = await client.post(
        f"{_URL}/{evaluation}/cancel",
        json=_cancel_body(evaluation, authority.digest()),
    )
    assert cancel.status_code == 401
    assert await _count(session_maker, CodingHostedAssignmentCancellation) == 0


@pytest.mark.asyncio
async def test_cancel_requires_exact_confirmation_and_digest_then_replays(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    authority, _, _, _ = await _prepared(session_maker, start=False)
    await _admit(session_maker, _request(authority))
    evaluation, digest = authority.evaluation_id, authority.digest()
    url = f"{_URL}/{evaluation}/cancel"

    wrong_phrase = await client.post(
        url,
        headers=_HEADERS,
        json=_cancel_body(evaluation, digest, confirmation="CANCEL ASSIGNMENT"),
    )
    assert wrong_phrase.status_code == 422
    other = uuid4()
    wrong_evaluation = await client.post(
        url,
        headers=_HEADERS,
        json=_cancel_body(
            evaluation,
            digest,
            confirmation=f"CANCEL SHADOW CODING HOSTED ASSIGNMENT {other} {digest}",
        ),
    )
    assert wrong_evaluation.status_code == 422
    stale = await client.post(
        url, headers=_HEADERS, json=_cancel_body(evaluation, "f" * 64)
    )
    assert stale.status_code == 409
    missing = await client.post(
        f"{_URL}/{other}/cancel", headers=_HEADERS, json=_cancel_body(other, digest)
    )
    assert missing.status_code == 404
    short = await client.post(
        url, headers=_HEADERS, json=_cancel_body(evaluation, digest, reason="  short ")
    )
    assert short.status_code == 422
    assert await _count(session_maker, CodingHostedAssignmentCancellation) == 0

    cancelled = await client.post(
        url, headers=_HEADERS, json=_cancel_body(evaluation, digest)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.headers["cache-control"] == "no-store"
    body = cancelled.json()
    assert body["idempotent"] is False and body["private_task_closed"] is True
    assert body["cancellation"]["prior_state"] == "admitted"
    assert body["cancellation"]["actor"] == "peyton@omniaura.ai"
    view = body["assignment"]
    assert view["state"] == "cancelled" and view["cancellable"] is False
    assert view["cancelled_at"] == body["cancellation"]["cancelled_at"]
    assert view["private_task"]["close_reason"] == "aborted"
    assert view["started_at"] is None and view["admitted_at"] is not None
    assert view["shadow_only"] is True and view["weight_eligible"] is False

    replay = await client.post(
        url, headers=_HEADERS, json=_cancel_body(evaluation, digest)
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["cancellation"] == body["cancellation"]
    conflict = await client.post(
        url,
        headers=_HEADERS,
        json=_cancel_body(evaluation, digest, reason="a different operator reason"),
    )
    assert conflict.status_code == 409
    assert await _count(session_maker, CodingHostedAssignmentCancellation) == 1
    assert await _count(session_maker, CodingHostedAssignment) == 1


@pytest.mark.asyncio
async def test_started_attempt_cannot_be_cancelled_by_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    authority, _, _, _ = await _prepared(session_maker)
    evaluation, digest = authority.evaluation_id, authority.digest()
    refused = await client.post(
        f"{_URL}/{evaluation}/cancel",
        headers=_HEADERS,
        json=_cancel_body(evaluation, digest),
    )
    assert refused.status_code == 409
    assert "worker" in refused.json()["message"]
    view = (await client.get(f"{_URL}/{evaluation}", headers=_HEADERS)).json()
    assert view["state"] == "running" and view["cancellable"] is False
    assert view["cancellation"] is None
    assert await _count(session_maker, CodingHostedAssignmentCancellation) == 0


@pytest.mark.asyncio
async def test_operator_create_replay_is_refused_after_cancel(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed_subject(session_maker)
    subject = _subject(agent_id, release_row_id)
    plan = (await client.post(f"{_URL}/preview", headers=_HEADERS, json=subject)).json()
    create = {
        **subject,
        "evaluation_id": plan["evaluation_id"],
        "attempt_id": plan["attempt_id"],
        "deadline_unix": plan["deadline_unix"],
        "confirmed_assignment_sha256": plan["assignment_sha256"],
        "reason": "synthetic operator canary assignment",
        "actor": "test-operator",
        "confirmation": plan["confirmation"],
    }
    assert (await client.post(_URL, headers=_HEADERS, json=create)).status_code == 200
    evaluation = UUID(plan["evaluation_id"])
    cancelled = await client.post(
        f"{_URL}/{evaluation}/cancel",
        headers=_HEADERS,
        json=_cancel_body(evaluation, plan["assignment_sha256"]),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancellation"]["prior_state"] == "pending_admission"

    replay = await client.post(_URL, headers=_HEADERS, json=create)
    assert replay.status_code == 409
    assert "cancelled" in replay.json()["message"]
    control = await client.get("/api/v1/admin/coding-control-plane", headers=_HEADERS)
    assert control.status_code == 200, control.text
    assert control.json()["native_operations"][0]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_list_pages_newest_first_without_overlap(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    receipt = _publication_receipt(Ed25519PrivateKey.generate())
    bundle = (_registration(receipt), receipt)
    created = []
    for _ in range(3):
        created.append(await _seed(session_maker, registration_bundle=bundle))
        await asyncio.sleep(0.01)
    newest_first = [str(authority.evaluation_id) for authority in reversed(created)]

    assert (await client.get(f"{_URL}?limit=101", headers=_HEADERS)).status_code == 422
    assert (await client.get(f"{_URL}?offset=-1", headers=_HEADERS)).status_code == 422
    first = await client.get(f"{_URL}?limit=2", headers=_HEADERS)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    page = first.json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)
    second = (await client.get(f"{_URL}?limit=2&offset=2", headers=_HEADERS)).json()
    assert second["total"] == 3 and len(second["assignments"]) == 1
    rows = page["assignments"] + second["assignments"]
    assert [row["evaluation_id"] for row in rows] == newest_first
    assert {row["state"] for row in rows} == {"pending_admission"}
    assert all(row["acknowledged"] is False for row in rows)
    assert all(row["terminal_outcome"] is None for row in rows)
    assert all("authority" not in row and "selection" not in row for row in rows)


@pytest.mark.asyncio
async def test_detail_redacts_private_state_and_sums_accounting(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    authority, worker, policy, _, ledger, grant = await inference_fixture(session_maker)
    evaluation = authority.evaluation_id

    settled = await reserve(ledger, grant)
    assert await ledger.settle(settlement(authority, policy, settled))
    uncertain = await reserve(ledger, grant, prompt_tokens=50, cost_usd_micros=60)
    await ledger.mark_uncertain(grant_id=grant, request_id=uncertain.request_id)
    frozen = await _freeze(session_maker, authority, worker)

    running = (await client.get(f"{_URL}/{evaluation}", headers=_HEADERS)).json()
    assert running["state"] == "running" and running["terminal"] is None
    accounting = running["inference"]
    assert (
        accounting["request_count"],
        accounting["reserved_count"],
        accounting["settled_count"],
        accounting["uncertain_count"],
    ) == (2, 0, 1, 1)
    # Settled usage (20/10/10) plus the uncertain request's full ceilings.
    assert (
        accounting["charged_prompt_tokens"],
        accounting["charged_completion_tokens"],
        accounting["charged_cost_usd_micros"],
    ) == (70, 110, 70)
    assert (
        accounting["settled_prompt_tokens"],
        accounting["settled_completion_tokens"],
        accounting["settled_cost_usd_micros"],
    ) == (20, 10, 10)
    assert (
        accounting["prompt_token_limit"],
        accounting["completion_token_limit"],
        accounting["cost_usd_micros_limit"],
    ) == (200, 200, 1000)
    assert accounting["revoked_at"] is not None and accounting["verified"] is False
    assert accounting["policy_sha256"] == policy.digest()

    secrets = await _seed_terminal_chain(session_maker, authority, worker)
    detail = await client.get(f"{_URL}/{evaluation}", headers=_HEADERS)
    assert detail.status_code == 200, detail.text
    assert detail.headers["cache-control"] == "no-store"
    view = detail.json()
    assert view["state"] == "completed" and view["cancellable"] is False
    assert view["terminal"]["outcome"] == "completed"
    assert view["terminal"]["evidence_sha256"] == secrets["terminal_sha256"]
    assert view["terminal"]["finalized_at"] is not None
    assert view["terminal_outcome"] == "completed" and view["acknowledged"] is True
    assert view["grading_claimed_at"] is not None
    assert view["authoring_evidence_reserved_at"] is not None
    assert view["authoring_evidence_finalized_at"] is not None
    assert view["private_task"]["close_reason"] == "completed"
    assert view["private_task"]["frozen_at"] is not None
    assert (view["delivery_count"], view["acknowledged_count"]) == (2, 1)
    assert view["deliveries_truncated"] is False
    acknowledged = {
        row["result_sha256"]: row["acknowledged_at"] for row in view["deliveries"]
    }
    assert acknowledged[secrets["acknowledged"]] is not None
    assert acknowledged[secrets["unacknowledged"]] is None
    assert view["policy_sha256"] == authority.policy_sha256
    assert view["selection_sha256"] == authority.selection_sha256
    assert view["deadline_unix"] == authority.deadline_unix

    listed = (await client.get(_URL, headers=_HEADERS)).json()["assignments"][0]
    assert listed["state"] == "completed" and listed["terminal_outcome"] == "completed"
    assert listed["acknowledged"] is True

    text = detail.text + json.dumps(listed)
    for private in (
        str(worker),
        str(grant),
        str(secrets["authoring_grant_id"]),
        str(secrets["grading_grant_id"]),
        str(secrets["claim_id"]),
        frozen.patch_sha256,
        "e" * 64,  # private schedule commitment inside the selection
        settled.locked_request_sha256,
        "hidden-grader-command-marker",
        "sealed-blob-object-marker",
        "delivery-body-signature-marker",
        "987654",
    ):
        assert private not in text
    for key in (
        "catalog_index",
        "selection_authority",
        "binding",
        "identity",
        "blob",
        "body",
        "settlement",
        "grant_id",
        "worker_id",
        "frozen_patch_sha256",
        "max_patch_bytes",
    ):
        assert f'"{key}"' not in text


async def _seed_terminal_chain(
    maker: async_sessionmaker[AsyncSession], authority, worker: UUID
) -> dict[str, object]:
    """Append synthetic sealed-evidence rows through the real insert triggers."""

    evaluation = authority.evaluation_id
    async with maker() as session, session.begin():
        assignment = await session.get(CodingHostedAssignment, evaluation)
        task = await session.get(CodingHostedPrivateTask, evaluation)
        assert assignment is not None and task is not None
        deadline = int(assignment.expires_at.timestamp())
        source = {
            "evaluation_id": str(evaluation),
            "attempt_id": str(authority.attempt_id),
            "worker_id": str(worker),
            "assignment_sha256": assignment.assignment_sha256,
            "artifact_sha256": assignment.artifact_sha256,
            "profile_capability_id": f"hosted-{authority.attempt_id}",
            "deadline_unix": deadline,
        }
        session.add(
            CodingHostedAuthoringReservation(
                evaluation_id=evaluation,
                identity_sha256="a1" * 32,
                identity={
                    "schema": "dittobench-coding-authoring-evidence-v2",
                    "weight_eligible": False,
                    "publication_deadline_unix": deadline + 86400,
                    "source": source,
                },
            )
        )
        await session.flush()
        session.add(
            CodingHostedAuthoringFinalization(
                evaluation_id=evaluation, probe_sha256="a2" * 32
            )
        )
        await session.flush()
        claim_id = uuid4()
        session.add(
            CodingHostedGradingClaim(
                evaluation_id=evaluation,
                claim_id=claim_id,
                binding={
                    "source": source,
                    "frozen_patch_sha256": task.frozen_patch_sha256,
                    "grading_profile_sha256": assignment.authority[
                        "grading_profile_sha256"
                    ],
                    "authoring_evidence_sha256": "a3" * 32,
                    "groups": [
                        {
                            "group": "hidden",
                            "total": 987654,
                            "command_id": "hidden-grader-command-marker",
                        }
                    ],
                },
            )
        )
        await session.flush()
        session.add(
            CodingHostedTerminalReservation(
                evaluation_id=evaluation,
                identity_sha256="a4" * 32,
                identity={
                    "claim_id": str(claim_id),
                    "authoring_evidence_sha256": "a3" * 32,
                    "grading_profile_sha256": assignment.authority[
                        "grading_profile_sha256"
                    ],
                    "weight_eligible": False,
                    "outcome": "completed",
                    "blob": {"object_id": "sealed-blob-object-marker"},
                },
            )
        )
        await session.flush()
        session.add(
            CodingHostedTerminalFinalization(
                evaluation_id=evaluation, probe_sha256="a5" * 32
            )
        )
        await session.flush()
        for digest in ("b1" * 32, "b2" * 32):
            session.add(
                CodingHostedResultDelivery(
                    result_sha256=digest,
                    evaluation_id=evaluation,
                    validator_hotkey=assignment.validator_hotkey,
                    body={"signature": "delivery-body-signature-marker"},
                )
            )
        await session.flush()
        session.add(
            CodingHostedResultAcknowledgement(
                result_sha256="b1" * 32, request_sha256="b3" * 32
            )
        )
        await close_hosted_private_task(
            session,
            evaluation_id=evaluation,
            attempt_id=authority.attempt_id,
            worker_id=worker,
            reason="completed",
        )
        grants = (task.authoring_grant_id, task.grading_grant_id)
    return {
        "terminal_sha256": "a4" * 32,
        "claim_id": claim_id,
        "acknowledged": "b1" * 32,
        "unacknowledged": "b2" * 32,
        "authoring_grant_id": grants[0],
        "grading_grant_id": grants[1],
    }
