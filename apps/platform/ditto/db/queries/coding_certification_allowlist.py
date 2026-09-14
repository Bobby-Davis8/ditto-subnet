"""Append-only operator allowlist for the shadow coding-certification path.

No revision means disabled: lease issue and certification inference grants keep
their existing eligibility rules. An enabled revision admits only its exact
``(agent_id, artifact_sha256, validator_hotkey)`` tuples, and an enabled
revision with no entries refuses all of them. A stored revision that cannot be
parsed or whose checksum disagrees refuses everything (fail closed).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_admin import (
    CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES,
    CodingCertificationAllowlistEntry,
    CodingCertificationAllowlistRevision,
    canonical_coding_certification_allowlist_entries,
    coding_certification_allowlist_checksum,
)
from ditto.db.models import (
    CodingCertificationAllowlistRevision as CodingCertificationAllowlistRevisionRow,
)

CODING_CERTIFICATION_NOT_ALLOWLISTED = "coding certification is not allowlisted"
_LOCK_KEY = "ditto:coding-certification-allowlist"


class CodingCertificationAllowlistRefusedError(RuntimeError):
    """The enabled operator allowlist does not name this exact tuple."""

    def __init__(self) -> None:
        super().__init__(CODING_CERTIFICATION_NOT_ALLOWLISTED)


class CodingCertificationAllowlistRevisionConflictError(RuntimeError):
    """The caller's expected revision is no longer current."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(
            "coding certification allowlist changed; re-read it and submit "
            f"expected_revision={current_revision}"
        )
        self.current_revision = current_revision


async def _lock(session: AsyncSession, *, shared: bool) -> None:
    """Serialize allowlist writes with every read that authorizes on it."""

    if session.get_bind().dialect.name != "postgresql":  # pragma: no cover
        return
    lock = func.pg_advisory_xact_lock_shared if shared else func.pg_advisory_xact_lock
    await session.execute(select(lock(func.hashtextextended(_LOCK_KEY, 0))))


async def latest_coding_certification_allowlist(
    session: AsyncSession,
) -> CodingCertificationAllowlistRevisionRow | None:
    return await session.scalar(
        select(CodingCertificationAllowlistRevisionRow)
        .order_by(CodingCertificationAllowlistRevisionRow.revision.desc())
        .limit(1)
    )


async def list_coding_certification_allowlist_revisions(
    session: AsyncSession,
    *,
    limit: int,
) -> Sequence[CodingCertificationAllowlistRevisionRow]:
    return list(
        await session.scalars(
            select(CodingCertificationAllowlistRevisionRow)
            .order_by(CodingCertificationAllowlistRevisionRow.revision.desc())
            .limit(limit)
        )
    )


def allowlist_entries_from_row(
    row: CodingCertificationAllowlistRevisionRow,
) -> list[CodingCertificationAllowlistEntry] | None:
    """Parse and verify one stored revision, or ``None`` if it is not exact."""

    if (
        not isinstance(row.entries, list)
        or len(row.entries) > CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES
    ):
        return None
    try:
        entries = [
            CodingCertificationAllowlistEntry.model_validate(item)
            for item in row.entries
        ]
    except (TypeError, ValidationError):
        return None
    if len({entry.key() for entry in entries}) != len(entries):
        return None
    if not row.enabled and entries:
        return None
    if (
        coding_certification_allowlist_checksum(enabled=row.enabled, entries=entries)
        != row.checksum
    ):
        return None
    return entries


def allowlist_revision_from_row(
    row: CodingCertificationAllowlistRevisionRow,
) -> CodingCertificationAllowlistRevision:
    entries = allowlist_entries_from_row(row)
    return CodingCertificationAllowlistRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        enabled=row.enabled,
        entries=entries if entries is not None else [],
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def default_coding_certification_allowlist() -> CodingCertificationAllowlistRevision:
    return CodingCertificationAllowlistRevision(
        revision=0,
        parent_revision=0,
        enabled=False,
        entries=[],
        checksum=coding_certification_allowlist_checksum(enabled=False, entries=[]),
        reason="Built-in default: coding certification allowlist disabled",
        actor="platform",
        created_at=None,
    )


async def active_coding_certification_allowlist(
    session: AsyncSession,
) -> frozenset[tuple[str, str, str]] | None:
    """Return the enforced tuple set, or ``None`` when the restriction is off.

    Takes the shared allowlist lock for the rest of the transaction, so a
    concurrent revision cannot commit between this read and the caller's write.
    """

    await _lock(session, shared=True)
    row = await latest_coding_certification_allowlist(session)
    if row is None:
        return None
    entries = allowlist_entries_from_row(row)
    if entries is None:
        # A corrupt or drifted revision must never widen access.
        return frozenset()
    if not row.enabled:
        return None
    return frozenset(entry.key() for entry in entries)


async def require_coding_certification_allowlisted(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    validator_hotkey: str,
) -> None:
    allowed = await active_coding_certification_allowlist(session)
    if allowed is None:
        return
    if (str(agent_id), artifact_sha256, validator_hotkey) not in allowed:
        raise CodingCertificationAllowlistRefusedError()


async def insert_coding_certification_allowlist_revision(
    session: AsyncSession,
    *,
    expected_revision: int,
    enabled: bool,
    entries: list[CodingCertificationAllowlistEntry],
    reason: str,
    actor: str,
) -> CodingCertificationAllowlistRevisionRow:
    """Append one complete revision under the exclusive allowlist lock."""

    await _lock(session, shared=False)
    current = await latest_coding_certification_allowlist(session)
    current_revision = current.revision if current is not None else 0
    if expected_revision != current_revision:
        raise CodingCertificationAllowlistRevisionConflictError(current_revision)
    canonical = canonical_coding_certification_allowlist_entries(entries)
    row = CodingCertificationAllowlistRevisionRow(
        parent_revision=current_revision,
        enabled=enabled,
        entries=[
            {
                "agent_id": agent_id,
                "artifact_sha256": artifact_sha256,
                "validator_hotkey": validator_hotkey,
            }
            for agent_id, artifact_sha256, validator_hotkey in (
                entry.key() for entry in canonical
            )
        ],
        checksum=coding_certification_allowlist_checksum(
            enabled=enabled, entries=canonical
        ),
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row
