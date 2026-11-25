"""SQLAlchemy Core tables owned by Email Campaign Service. Only this service touches them (build.md §3.2)."""

from __future__ import annotations

from sqlalchemy import MetaData

from nexus_core.tables import idempotency_table, inbox_table, outbox_table

metadata = MetaData()

outbox = outbox_table(metadata)
inbox = inbox_table(metadata)
idempotency_keys = idempotency_table(metadata)
