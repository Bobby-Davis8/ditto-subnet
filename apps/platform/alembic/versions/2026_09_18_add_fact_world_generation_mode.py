"""Version private generation identity and reserve both fact-world draws.

Revision ID: c2198f0a6e43
Revises: ae67182d095c
"""

import sqlalchemy as sa

from alembic import op

revision = "c2198f0a6e43"
down_revision = "ae67182d095c"
branch_labels = None
depends_on = None

_TABLES = ("private_benchmark_datasets", "private_benchmark_preparations")
_LEGACY_SALT = (
    "octet_length(surface_salt) = 8 AND "
    "surface_salt <> decode('0000000000000000', 'hex')"
)
_FACT_SALT = (
    "octet_length(surface_salt) = 16 AND "
    "substring(surface_salt from 1 for 8) <> decode('0000000000000000', 'hex') AND "
    "substring(surface_salt from 9 for 8) <> decode('0000000000000000', 'hex') AND "
    "substring(surface_salt from 1 for 8) <> substring(surface_salt from 9 for 8) AND "
    "substring(surface_salt from 1 for 8) <> int8send(seed)"
)


def _salt(expression: str) -> None:
    name = op.f("ck_private_benchmark_preparations_salt")
    op.drop_constraint(name, _TABLES[1], type_="check")
    op.create_check_constraint(name, _TABLES[1], expression)


def _identity_trigger(include_mode: bool) -> None:
    old = ", OLD.generation_mode" if include_mode else ""
    new = ", NEW.generation_mode" if include_mode else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION protect_private_benchmark_preparation_identity()
        RETURNS trigger AS $$ BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'private preparation history is retained';
          END IF;
          IF ROW(OLD.preparation_id, OLD.identity_sha256, OLD.scope,
                 OLD.bench_version, OLD.seed, OLD.run_size,
                 OLD.transform_profile_sha256, OLD.surface_salt, OLD.created_at{old})
             IS DISTINCT FROM
             ROW(NEW.preparation_id, NEW.identity_sha256, NEW.scope,
                 NEW.bench_version, NEW.seed, NEW.run_size,
                 NEW.transform_profile_sha256, NEW.surface_salt, NEW.created_at{new})
             OR OLD.state = 'ready' THEN
            RAISE EXCEPTION 'private preparation identity or ready result is immutable';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "generation_mode",
                sa.Text(),
                nullable=False,
                server_default="legacy-rewrite",
            ),
        )
    op.create_check_constraint(
        "generation_mode",
        _TABLES[0],
        "generation_mode IN ('legacy-rewrite', 'fact-world-v1')",
    )
    _salt(
        f"(generation_mode = 'legacy-rewrite' AND {_LEGACY_SALT}) OR "
        f"(generation_mode = 'fact-world-v1' AND {_FACT_SALT})"
    )
    _identity_trigger(True)


def downgrade() -> None:
    # Retained fact-world rows must not silently become legacy identities.
    for table in _TABLES:
        op.execute(f"""
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM {table}
                         WHERE generation_mode <> 'legacy-rewrite') THEN
                RAISE EXCEPTION 'cannot downgrade retained fact-world generation';
              END IF;
            END $$;
        """)
    _identity_trigger(False)
    _salt(_LEGACY_SALT)
    op.drop_constraint(
        op.f("ck_private_benchmark_datasets_generation_mode"), _TABLES[0], type_="check"
    )
    for table in _TABLES:
        op.drop_column(table, "generation_mode")
