# ADR-0008: Transactional outbox and inbox for events

- Status: Accepted
- Date: 2026-11-25

## Decision
**Outbox (producer side)**
- Each service has an `outbox` table: `id`, `tenant_id`, `event_type`, `event_version`, `payload jsonb`, `headers`, `created_at` and `published_at`.
- Outbox rows are written in the same transaction as the business change.
- An `outbox-relay` process per service polls with `FOR UPDATE SKIP LOCKED` and publishes to the `nexus.events` topic exchange with publisher confirms, then sets `published_at`.

**Inbox (consumer side)**
- Each consumer has an `inbox` table: `consumer`, `event_id`, `received_at`, with `PRIMARY KEY (consumer, event_id)`.
- A consumer inserts the inbox row and applies its effects in one transaction. A duplicate event conflicts on the primary key and is acked as a no-op.

**Event rules**
- Events follow the build.md §6.3 envelope and are defined in `packages/event_schemas`.
- Events contain IDs and minimal metadata, never bodies or secrets.

## Consequences
- Delivery is at least once, with effectively-once side effects.
- Ordering is guaranteed only within one aggregate, via `aggregate_version` in the payload. Consumers must tolerate reordering across aggregates.
