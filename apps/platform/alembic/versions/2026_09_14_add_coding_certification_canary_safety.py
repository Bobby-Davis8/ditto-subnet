"""recover claimed coding certification leases and add a strict allowlist

Revision ID: c7a2e5d19b43
Revises: e6f4a9c2d781
Create Date: 2026-09-14

Default-off safety changes for the shadow contract-v1 certification path.

1. Lease lifecycle. A claimed lease whose receipt window has passed may become
   ``expired`` while keeping its ``claimed_at`` audit timestamp, so one
   post-claim failure no longer holds the in-flight unique slot for that exact
   agent, artifact, image, and benchmark forever. A lease whose receipt was
   accepted becomes ``completed``, which is terminal and never expires. Existing
   claimed leases that already carry a receipt are backfilled to ``completed``.
   An allowlist revision may abort an in-flight lease, including a claimed one,
   and records itself in ``aborted_allowlist_revision``.

2. ``coding_certification_allowlist_revisions`` is an append-only, strict
   operator setting. No row refuses every certification lease, claim, harness,
   grant, and receipt; only an enabled revision admits its exact tuples. It
   never participates in scoring, weights, or emissions.

3. ``claim_allowlist_revision`` records the revision that admitted each claim.
   The per-identity attempt budget counts only such admitted claims.

The table is small, is not a hot table, and no trigger on it changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a2e5d19b43"
down_revision: str | Sequence[str] | None = "e6f4a9c2d781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "coding_certification_leases"
_STATUS = "coding_certification_leases_status_check"
_LIFECYCLE = "coding_certification_leases_lifecycle_check"
_CLAIM_ALLOWLIST = "coding_certification_leases_claim_allowlist_check"
_CLAIM_ALLOWLIST_FK = "coding_certification_leases_claim_allowlist_fkey"
_ABORTED_ALLOWLIST_FK = "coding_certification_leases_aborted_allowlist_fkey"

_PRIOR_STATUS = "status IN ('issued', 'claimed', 'aborted', 'expired')"
_STRICT_STATUS = "status IN ('issued', 'claimed', 'completed', 'aborted', 'expired')"
_PRIOR_LIFECYCLE = (
    "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL) "
    "OR (status = 'claimed' AND claimed_at IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at IS NULL) "
    "OR (status = 'aborted' AND aborted_at IS NOT NULL "
    "AND aborted_at >= issued_at AND claimed_at IS NULL) "
    "OR (status = 'expired' AND claimed_at IS NULL AND aborted_at IS NULL)"
)
# Keep byte-identical with ``ditto.db.models.CODING_CERTIFICATION_LEASE_LIFECYCLE``.
_RECOVERABLE_LIFECYCLE = (
    "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL "
    "AND aborted_allowlist_revision IS NULL) "
    "OR (status IN ('claimed', 'completed') AND claimed_at IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at IS NULL AND aborted_allowlist_revision IS NULL) "
    "OR (status = 'aborted' AND aborted_at IS NOT NULL "
    "AND aborted_at >= issued_at AND (claimed_at IS NULL "
    "OR (aborted_allowlist_revision IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at >= claimed_at))) "
    "OR (status = 'expired' AND aborted_at IS NULL "
    "AND aborted_allowlist_revision IS NULL "
    "AND (claimed_at IS NULL "
    "OR (claimed_at >= issued_at AND claimed_at < deadline)))"
)


def upgrade() -> None:
    # Both helpers apply the metadata naming convention, so each replacement
    # keeps the exact constraint name the ORM model and later migrations use.
    op.drop_constraint(_LIFECYCLE, _TABLE, type_="check")
    op.drop_constraint(_STATUS, _TABLE, type_="check")

    op.create_table(
        "coding_certification_allowlist_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_revision >= 0 AND parent_revision < revision",
            name="coding_certification_allowlist_parent_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entries) = 'array' "
            "AND jsonb_array_length(entries) <= 16 "
            "AND enabled = (jsonb_array_length(entries) > 0)",
            name="coding_certification_allowlist_entries_check",
        ),
        sa.CheckConstraint(
            "checksum ~ '^[0-9a-f]{64}$'",
            name="coding_certification_allowlist_checksum_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="coding_certification_allowlist_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_certification_allowlist_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision",
            name="coding_certification_allowlist_parent_key",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION guard_coding_certification_allowlist_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'coding certification allowlist revisions are append-only'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'coding_certification_allowlist_append_only_guard';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER coding_certification_allowlist_revisions_append_only_guard
        BEFORE UPDATE OR DELETE ON coding_certification_allowlist_revisions
        FOR EACH ROW
        EXECUTE FUNCTION guard_coding_certification_allowlist_append_only()
        """
    )

    op.add_column(
        _TABLE, sa.Column("claim_allowlist_revision", sa.Integer(), nullable=True)
    )
    op.add_column(
        _TABLE, sa.Column("aborted_allowlist_revision", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        _CLAIM_ALLOWLIST_FK,
        _TABLE,
        "coding_certification_allowlist_revisions",
        ["claim_allowlist_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        _ABORTED_ALLOWLIST_FK,
        _TABLE,
        "coding_certification_allowlist_revisions",
        ["aborted_allowlist_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )

    # A lease whose receipt Platform already accepted is terminal. Before this
    # revision such a lease simply stayed ``claimed``.
    op.execute(
        """
        UPDATE coding_certification_leases AS lease
        SET status = 'completed'
        WHERE lease.status = 'claimed'
          AND EXISTS (
              SELECT 1 FROM coding_capability_certifications AS receipt
              WHERE receipt.lease_id = lease.lease_id
          )
        """
    )

    op.create_check_constraint(_STATUS, _TABLE, _STRICT_STATUS)
    # Only a downgrade of this revision can leave an aborted claimed lease
    # without the allowlist revision that aborted it (the column was dropped).
    # That audit history is not rewritten; the CHECK then binds new writes only.
    orphaned_abort = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM coding_certification_leases "
            "WHERE status = 'aborted' AND claimed_at IS NOT NULL)"
        )
    )
    op.create_check_constraint(
        _LIFECYCLE,
        _TABLE,
        _RECOVERABLE_LIFECYCLE,
        postgresql_not_valid=bool(orphaned_abort),
    )
    op.create_check_constraint(
        _CLAIM_ALLOWLIST,
        _TABLE,
        "claim_allowlist_revision IS NULL "
        "OR (claim_allowlist_revision > 0 AND claimed_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(_CLAIM_ALLOWLIST, _TABLE, type_="check")
    op.drop_constraint(_LIFECYCLE, _TABLE, type_="check")
    op.drop_constraint(_STATUS, _TABLE, type_="check")
    # The prior schema represented a receipted lease as ``claimed``.
    op.execute(
        "UPDATE coding_certification_leases SET status = 'claimed' "
        "WHERE status = 'completed'"
    )
    op.drop_constraint(_ABORTED_ALLOWLIST_FK, _TABLE, type_="foreignkey")
    op.drop_constraint(_CLAIM_ALLOWLIST_FK, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "aborted_allowlist_revision")
    op.drop_column(_TABLE, "claim_allowlist_revision")
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "coding_certification_allowlist_revisions_append_only_guard "
        "ON coding_certification_allowlist_revisions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS guard_coding_certification_allowlist_append_only()"
    )
    op.drop_table("coding_certification_allowlist_revisions")
    op.create_check_constraint(_STATUS, _TABLE, _PRIOR_STATUS)
    # Claimed-then-expired and allowlist-aborted claimed rows are audit history
    # and are never rewritten or deleted. The restored stricter CHECK is NOT
    # VALID so it binds new writes without refusing the downgrade over rows the
    # newer code legitimately made.
    op.create_check_constraint(
        _LIFECYCLE,
        _TABLE,
        _PRIOR_LIFECYCLE,
        postgresql_not_valid=True,
    )
