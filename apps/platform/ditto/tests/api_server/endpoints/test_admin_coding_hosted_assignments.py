"""Admin tests for the trusted hosted-v2 assignment operator path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.endpoints import admin_coding_hosted_assignments as endpoint
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingHostedAssignment,
    CodingHostedPrivateTask,
)
from ditto.db.queries.coding_private_v2_releases import insert_private_v2_release
from ditto.tests.api_server.endpoints.test_admin_coding_private_v2_releases import (
    _HEADERS,
    _install,
    _publication_receipt,
    _registration,
)

_URL = "/api/v1/admin/coding-hosted-assignments"
_BENCH = 12
_VALIDATOR = "5" + "V" * 47
_ARTIFACT = "a" * 64
_IMAGE = "b" * 64


async def _seed(
    maker: async_sessionmaker[AsyncSession], *, certified: bool = True
) -> tuple[UUID, UUID, str]:
    receipt = _publication_receipt(Ed25519PrivateKey.generate())
    registration = _registration(receipt)
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        release = await insert_private_v2_release(
            session,
            registration=registration,
            receipt=receipt,
            reason="synthetic registration",
            actor="test-operator",
        )
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="synthetic-miner",
                name="synthetic-agent",
                sha256=_ARTIFACT,
                status=AgentStatus.EVALUATING,
                screened_image_sha256=_IMAGE,
                screened_image_size_bytes=1,
                screened_image_id="sha256:" + "c" * 64,
                screened_image_ref="synthetic-image",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
            )
        )
        await session.flush()
        if certified:
            session.add(
                CodingCapabilityCertification(
                    certification_row_id=uuid4(),
                    agent_id=agent_id,
                    artifact_sha256=_ARTIFACT,
                    screened_image_sha256=_IMAGE,
                    validator_hotkey=_VALIDATOR,
                    bench_version=_BENCH,
                    settlement_generation=1,
                    settlement_inference_grant_sha256="5a" * 32,
                    settlement_provider_receipt_set_sha256="5b" * 32,
                    ticket_deadline=now + timedelta(hours=1),
                    coding_contract_version=2,
                    certification_id="operator-path-cert-001",
                    status="certified",
                    failure_stage=None,
                    failure_code=None,
                    certification_sha256="55" * 32,
                    canary_manifest_sha256="56" * 32,
                    transcript_object_key="sha256/" + "57" * 32,
                    frozen_submission_object_key="sha256/" + "58" * 32,
                    issued_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(hours=2),
                    weight_eligible=False,
                    receipt={},
                    signature="59" * 64,
                    created_at=now,
                )
            )
    return agent_id, release.row.release_row_id, registration.registration_sha256


def _subject(agent_id: UUID, release_row_id: UUID) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "release_row_id": str(release_row_id),
        "catalog_index": 0,
        "validator_hotkey": _VALIDATOR,
        "policy_sha256": "2" * 64,
        "execution_profile_sha256": "3" * 64,
        "grading_profile_sha256": "4" * 64,
        "max_patch_bytes": 1 << 20,
    }


async def _count(maker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture(autouse=True)
def _fixed_bench_version(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _active(_session: AsyncSession, **_kwargs: object) -> int:
        return _BENCH

    monkeypatch.setattr(endpoint, "active_bench_version", _active)


@pytest.mark.asyncio
async def test_hosted_assignment_endpoints_require_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(session_maker)
    body = _subject(agent_id, release_row_id)
    assert (await client.post(f"{_URL}/preview", json=body)).status_code == 401
    assert (await client.post(_URL, json=body)).status_code in (401, 422)


@pytest.mark.asyncio
async def test_preview_derives_authority_server_side_and_writes_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, registration_sha256 = await _seed(session_maker)

    preview = await client.post(
        f"{_URL}/preview", headers=_HEADERS, json=_subject(agent_id, release_row_id)
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["cache-control"] == "no-store"
    plan = preview.json()
    assert plan["artifact_sha256"] == _ARTIFACT
    assert plan["screened_image_sha256"] == _IMAGE
    assert plan["registration_sha256"] == registration_sha256
    assert plan["bench_version"] == _BENCH
    assert plan["selection"]["catalog_index"] == 0
    assert plan["authority"]["selection_sha256"] == plan["selection_sha256"]
    assert plan["confirmation"] == (
        "CREATE SHADOW CODING HOSTED ASSIGNMENT "
        f"{plan['evaluation_id']} {plan['assignment_sha256']}"
    )
    assert plan["shadow_only"] is True and plan["weight_eligible"] is False
    assert await _count(session_maker, CodingHostedAssignment) == 0
    assert await _count(session_maker, CodingHostedPrivateTask) == 0


@pytest.mark.asyncio
async def test_preview_refuses_subject_without_active_certification(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(session_maker, certified=False)

    refused = await client.post(
        f"{_URL}/preview", headers=_HEADERS, json=_subject(agent_id, release_row_id)
    )
    assert refused.status_code == 409
    assert "certification" in refused.json()["message"]


@pytest.mark.asyncio
async def test_create_requires_exact_confirmation_then_creates_and_binds(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(session_maker)
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

    wrong = await client.post(
        _URL, headers=_HEADERS, json={**create, "confirmation": "CREATE ASSIGNMENT"}
    )
    assert wrong.status_code == 422
    assert await _count(session_maker, CodingHostedAssignment) == 0

    tampered = await client.post(
        _URL,
        headers=_HEADERS,
        json={**create, "confirmed_assignment_sha256": "f" * 64},
    )
    assert tampered.status_code == 422
    assert await _count(session_maker, CodingHostedAssignment) == 0

    created = await client.post(_URL, headers=_HEADERS, json=create)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["assignment_sha256"] == plan["assignment_sha256"]

    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, UUID(plan["evaluation_id"]))
        task = await session.get(CodingHostedPrivateTask, UUID(plan["evaluation_id"]))
    assert row is not None and row.assignment_sha256 == plan["assignment_sha256"]
    assert row.artifact_sha256 == _ARTIFACT
    assert row.shadow_only is True and row.weight_eligible is False
    assert task is not None and task.selection_sha256 == plan["selection_sha256"]
    assert str(task.authoring_grant_id) == body["authoring_grant_id"]

    replay = await client.post(_URL, headers=_HEADERS, json=create)
    assert replay.status_code == 200, replay.text
    assert replay.json()["authoring_grant_id"] == body["authoring_grant_id"]
    assert await _count(session_maker, CodingHostedAssignment) == 1
    assert await _count(session_maker, CodingHostedPrivateTask) == 1
