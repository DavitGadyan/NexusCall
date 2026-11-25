# ADR-0007: Database-per-service with forced PostgreSQL RLS

- Status: Accepted
- Date: 2026-11-25

## Decision
**One database per service**
- Each service gets its own logical database: one Postgres 16 cluster locally, CloudNativePG clusters in production.

**Roles per service database**
| Role | Use | Privileges |
|---|---|---|
| `<svc>_owner` | migrations | owns schema `app` |
| `<svc>_app` | runtime | `NOSUPERUSER NOBYPASSRLS NOCREATEROLE`; DML only, granted per table |
| `<svc>_inspector` | RLS dashboard | catalog read + `pg_policies` |
| `<svc>_platform` | break-glass path only | gated by support-grant policies |

**Tenant tables**
- Every tenant-owned table has `tenant_id uuid NOT NULL` and `UNIQUE (tenant_id, id)`.
- Child tables use composite foreign keys.
- `ENABLE` + `FORCE ROW LEVEL SECURITY`.
- Separate policies for SELECT, INSERT, UPDATE and DELETE, all keyed on `current_setting('app.tenant_id', true)::uuid`.
- A trigger prevents changes to `tenant_id`.

**Session context**
- All access goes through `nexus_core.db.tenant_session()`, which opens a transaction and runs `SET LOCAL`.
- Connections are reset when returned to the pool.

**Enforcement and testing**
- An Alembic migration lint (CI) fails if any table with a `tenant_id` column lacks RLS + FORCE, or if any table without `tenant_id` is missing from the `PLATFORM_SCOPED_TABLES` allowlist.
- Tests connect as `<svc>_app`, never as superuser.

## Consequences
- No cross-service joins are possible. Analytics uses event-fed read models.
- Migrations run once per deploy as a Job under `<svc>_owner`.
