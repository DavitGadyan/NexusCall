"""Baseline: shared SQL functions + outbox/inbox/idempotency infrastructure tables.

Revision ID: 0001
"""

from alembic import op

from analytics_service.tables import metadata
from nexus_core.rls import create_common_functions
from nexus_core.tables import create_infra_tables

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "analytics_app"
INFRA = ("outbox", "inbox", "idempotency_keys")


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    create_common_functions(op)
    bind = op.get_bind()
    for name in INFRA:
        metadata.tables[name].create(bind)
    create_infra_tables(op, APP_ROLE)


def downgrade() -> None:
    for name in reversed(INFRA):
        op.execute(f"DROP TABLE IF EXISTS {name}")
