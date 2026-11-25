"""risk_results: partial unique index on (tenant_id, cache_key) for cacheable results

Concurrent identical margin computes could previously store two OK rows for the same inputs. The partial unique index makes the
second insert fail atomically; the service then returns the winner's immutable result. BLOCKED rows (audit of each attempt) are
deliberately excluded from uniqueness.

Authored by A3 VECTOR (round 2). down_revision is the lead's baseline; the lead must merge heads if other engines also add revisions.

Revision ID: a3_0002_risk_cache
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "a3_0002_risk_cache"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_risk_result_cacheable", "risk_results", ["tenant_id", "cache_key"], unique=True,
                    postgresql_where=sa.text("status IN ('OK','UNSUPPORTED')"),
                    sqlite_where=sa.text("status IN ('OK','UNSUPPORTED')"))


def downgrade() -> None:
    op.drop_index("uq_risk_result_cacheable", table_name="risk_results")
