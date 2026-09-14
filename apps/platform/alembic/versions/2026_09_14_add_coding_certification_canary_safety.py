"""recover claimed coding certification leases and add an operator allowlist

Revision ID: c7a2e5d19b43
Revises: e6f4a9c2d781
Create Date: 2026-09-14

Two default-off safety changes for the shadow contract-v1 certification path.

1. A claimed lease whose deadline has passed may now become ``expired`` while
   keeping its ``claimed_at`` audit timestamp. Previously only an unclaimed
   lease could expire, so one post-claim failure held the in-flight unique slot
   for that exact agent, artifact, image, and benchmark forever. The new
   lifecycle CHECK is strictly weaker than the old one, so every existing row
   already satisfies it. The table is small, is not a hot table, and no status
   or trigger other than this CHECK changes.

2. ``coding_certification_allowlist_revisions`` is an append-only operator
   setting. No row means the restriction is disabled and behaviour is
   unchanged. It never participates in scoring, weights, or emissions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a2e5d19b43"
down_revision: str | Sequence[str] | None = "e6f4a9c2d781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE = "coding_certification_leases_lifecycle_check"
_PRIOR_LIFECYCLE = (
    "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL) "
    "OR (status = 'claimed' AND claimed_at IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at IS NULL) "
    "OR (status = 'aborted' AND aborted_at IS NOT NULL "
    "AND aborted_at >= issued_at AND claimed_at IS NULL) "
    "OR (status = 'expired' AND claimed_at IS NULL AND aborted_at IS NULL)"
)
_RECOVERABLE_LIFECYCLE = (
    "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL) "
    "OR (status = 'claimed' AND claimed_at IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at IS NULL) "
    "OR (status = 'aborted' AND aborted_at IS NOT NULL "
    "AND aborted_at >= issued_at AND claimed_at IS NULL) "
    "OR (status = 'expired' AND aborted_at IS NULL "
    "AND (claimed_at IS NULL "
    "OR (claimed_at >= issued_at AND claimed_at < deadline)))"
)


def upgrade() -> None:
    # Both helpers apply the metadata naming convention, so the replacement
    # keeps the exact constraint name the ORM model and later migrations use.
    op.drop_constraint(_LIFECYCLE, "coding_certification_leases", type_="check")
    op.create_check_constraint(
        _LIFECYCLE, "coding_certification_leases", _RECOVERABLE_LIFECYCLE
    )

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
            "AND (enabled OR jsonb_array_length(entries) = 0)",
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


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "coding_certification_allowlist_revisions_append_only_guard "
        "ON coding_certification_allowlist_revisions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS guard_coding_certification_allowlist_append_only()"
    )
    op.drop_table("coding_certification_allowlist_revisions")
    op.drop_constraint(_LIFECYCLE, "coding_certification_leases", type_="check")
    # Claimed-then-expired rows are audit history and are never rewritten or
    # deleted. The restored stricter CHECK is NOT VALID so it binds new writes
    # without refusing the downgrade over rows the newer code legitimately made.
    op.create_check_constraint(
        _LIFECYCLE,
        "coding_certification_leases",
        _PRIOR_LIFECYCLE,
        postgresql_not_valid=True,
    )
