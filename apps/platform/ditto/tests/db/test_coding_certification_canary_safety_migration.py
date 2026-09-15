"""Data round trip for the coding-certification canary-safety migration."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ditto.tests import pgharness
from ditto.tests.db.queries import test_coding_certification_canary_safety as canary

_REVISION = "c7a2e5d19b43"
_PARENT = "e6f4a9c2d781"


def _alembic(target: pgharness.Dsn, action: str, revision: str) -> None:
    from alembic.config import Config

    from alembic import command

    previous = {key: os.environ.get(key) for key in target.env}
    os.environ.update(target.env)
    try:
        cfg = Config(str(pgharness._REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(pgharness._REPO_ROOT / "alembic"))
        getattr(command, action)(cfg, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _seed(dsn: pgharness.Dsn) -> dict[str, UUID]:
    # A scratch database, not this worker's: the migration is downgraded here.
    engine = create_async_engine(dsn.sqlalchemy)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            agent = await canary._qualified_agent(session)
            receipted = await canary._issue_and_claim(session, agent)
            await canary._record_receipt(session, receipted)
            other = await canary._qualified_agent(session, evidence="22")
            aborted = await canary._issue_and_claim(session, other)
            await canary._set_allowlist(session, [canary._entry(agent)])
            third = await canary._qualified_agent(session, evidence="33")
            expired = await canary._issue_and_claim(session, third)
            await canary._backdate(session, expired)
            assert await canary._issue(session, third) != expired
    finally:
        await engine.dispose()
    return {"receipted": receipted, "aborted": aborted, "expired": expired}


async def _state(dsn: pgharness.Dsn, ids: dict[str, UUID]) -> tuple[dict, bool]:
    conn = await asyncpg.connect(dsn.asyncpg)
    try:
        statuses = {
            name: await conn.fetchval(
                "SELECT status FROM coding_certification_leases WHERE lease_id = $1",
                lease_id,
            )
            for name, lease_id in ids.items()
        }
        validated = await conn.fetchval(
            "SELECT bool_and(convalidated) FROM pg_constraint "
            "WHERE conrelid = 'coding_certification_leases'::regclass "
            "AND contype = 'c'"
        )
        return statuses, bool(validated)
    finally:
        await conn.close()


async def test_canary_safety_migration_round_trips_with_lease_history(
    postgres_admin_dsn: pgharness.Dsn,
) -> None:
    name = f"{pgharness.DB_PREFIX}canary_migration_{uuid4().hex[:12]}"
    database = await asyncio.to_thread(
        pgharness.provision_worker_database, postgres_admin_dsn, name
    )
    try:
        ids = await _seed(database.dsn)
        assert await _state(database.dsn, ids) == (
            {"receipted": "completed", "aborted": "aborted", "expired": "expired"},
            True,
        )

        await asyncio.to_thread(_alembic, database.dsn, "downgrade", _PARENT)
        # The prior schema has no completed status and no allowlist abort.
        statuses, _ = await _state(database.dsn, ids)
        assert statuses == {
            "receipted": "claimed",
            "aborted": "expired",
            "expired": "expired",
        }

        await asyncio.to_thread(_alembic, database.dsn, "upgrade", _REVISION)
        # Receipted leases are backfilled, and every lease CHECK is validated.
        assert await _state(database.dsn, ids) == (
            {"receipted": "completed", "aborted": "expired", "expired": "expired"},
            True,
        )
    finally:
        await asyncio.to_thread(
            pgharness.drop_worker_database, postgres_admin_dsn, name
        )
