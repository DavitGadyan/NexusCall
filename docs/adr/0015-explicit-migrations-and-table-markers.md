# ADR-0015: Explicit migrations, model-declared table markers and a global migration guard

- Status: Accepted
- Date: 2026-11-25

## Context
A migration that creates tables from the live SQLAlchemy metadata changes its meaning whenever the model changes. That breaks the upgrade path build.md §12.2 requires. We also need every exemption from tenant RLS to be visible and reviewable.

## Decision
- **Migrations are frozen.** They contain explicit `op.create_table` / `op.add_column` calls. `scripts/gen_migration.py <service> "<title>"` generates them with Alembic autogenerate against a throwaway PostgreSQL. RLS, grants and seed data are then added by hand with the `nexus_core.rls` helpers.
- **Table markers live in the model** as table comments:
  - `comment="platform_scoped: <reason>"` for platform-global tables (no tenant RLS)
  - `comment="append_only: <reason>"` for immutable history (SELECT/INSERT policies only, plus triggers that block UPDATE/DELETE/TRUNCATE)
  - Services also list platform tables in `PLATFORM_SCOPED = {table: reason}`.
- **`tests/security/test_migrations.py` runs for every service.** It migrates an empty database as the owner role and fails when:
  - the schema drifts from `tables.py` (comments included)
  - a table has neither complete forced RLS nor a declared marker
  - a tenant table has a nullable `tenant_id`
  - the runtime role is a superuser, can bypass RLS, or owns tables
- The platform RLS dashboard reads the same catalog data through each service's read-only inspector role.

## Consequences
- Table definitions exist twice (model and migration). The drift test keeps them in step.
- Adding a platform-scoped table needs a reason, written in code and reviewed.
