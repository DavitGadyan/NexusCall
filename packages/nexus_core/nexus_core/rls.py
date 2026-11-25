"""RLS migration helpers and coverage inspection (build.md §5.3, §20.1).

Policies are always defined in version-controlled Alembic migrations through
these helpers; the admin UI only *reads* coverage (no policy editing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TENANT_EXPR = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"

COMMON_FUNCTIONS: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION app_prevent_tenant_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'tenant_id is immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$
""",
    """
CREATE OR REPLACE FUNCTION app_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$
""",
    """
CREATE OR REPLACE FUNCTION app_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table is append-only' USING ERRCODE = '42501';
END $$
""",
)


def create_common_functions(op: Any) -> None:
    for stmt in COMMON_FUNCTIONS:
        op.execute(stmt)


def _check(identifier: str) -> str:
    if not _IDENT.match(identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier!r}")
    return identifier


def tenant_rls_statements(
    table: str, app_role: str, *, privileges: str = "SELECT, INSERT, UPDATE, DELETE"
) -> list[str]:
    """SQL that enables + forces RLS with per-command policies for a tenant-owned table."""
    t, r = _check(table), _check(app_role)
    return [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {t}_tenant_select ON {t} FOR SELECT USING (tenant_id = {TENANT_EXPR})",
        f"CREATE POLICY {t}_tenant_insert ON {t} FOR INSERT WITH CHECK (tenant_id = {TENANT_EXPR})",
        f"CREATE POLICY {t}_tenant_update ON {t} FOR UPDATE USING (tenant_id = {TENANT_EXPR})"
        f" WITH CHECK (tenant_id = {TENANT_EXPR})",
        f"CREATE POLICY {t}_tenant_delete ON {t} FOR DELETE USING (tenant_id = {TENANT_EXPR})",
        f"CREATE TRIGGER {t}_tenant_immutable BEFORE UPDATE OF tenant_id ON {t}"
        f" FOR EACH ROW EXECUTE FUNCTION app_prevent_tenant_change()",
        f"GRANT {privileges} ON {t} TO {r}",
    ]


def apply_tenant_rls(op: Any, table: str, app_role: str, **kw: Any) -> None:
    for stmt in tenant_rls_statements(table, app_role, **kw):
        op.execute(stmt)


def grant_platform_table(op: Any, table: str, app_role: str, privileges: str = "SELECT") -> None:
    op.execute(f"GRANT {privileges} ON {_check(table)} TO {_check(app_role)}")


@dataclass(frozen=True, slots=True)
class TableCoverage:
    schema: str
    table: str
    has_tenant_id: bool
    rls_enabled: bool
    rls_forced: bool
    policies: tuple[str, ...]
    commands: tuple[str, ...]
    platform_scoped: bool

    @property
    def status(self) -> str:
        if self.platform_scoped:
            # Explicitly reviewed exemption (catalogues, transport tables). Must carry a reason comment.
            return "platform_scoped"
        if not self.has_tenant_id:
            return "missing_tenant_id"
        if not self.rls_enabled:
            return "rls_disabled"
        if not self.rls_forced:
            return "rls_not_forced"
        if not {"SELECT", "INSERT", "UPDATE", "DELETE"} <= set(self.commands) and "ALL" not in self.commands:
            return "incomplete_policies"
        return "ok"


COVERAGE_SQL = text(
    """
    SELECT n.nspname AS schema, c.relname AS table,
           EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                   AND NOT a.attisdropped) AS has_tenant_id,
           c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS rls_forced,
           COALESCE(array_agg(p.policyname ORDER BY p.policyname)
                    FILTER (WHERE p.policyname IS NOT NULL), '{}') AS policies,
           COALESCE(array_agg(DISTINCT p.cmd) FILTER (WHERE p.cmd IS NOT NULL), '{}') AS commands,
           COALESCE(obj_description(c.oid, 'pg_class') LIKE '%platform_scoped%', false) AS platform_scoped
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_policies p ON p.schemaname = n.nspname AND p.tablename = c.relname
    WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND c.relname <> 'alembic_version'
    GROUP BY n.nspname, c.relname, c.oid, c.relrowsecurity, c.relforcerowsecurity
    ORDER BY n.nspname, c.relname
    """
)

ROLE_SQL = text(
    """
    SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb
    FROM pg_roles WHERE rolname = current_user OR rolname = :app_role
    """
)


async def inspect_coverage(conn: AsyncConnection) -> list[TableCoverage]:
    rows = (await conn.execute(COVERAGE_SQL)).mappings().all()
    return [
        TableCoverage(
            schema=r["schema"],
            table=r["table"],
            has_tenant_id=r["has_tenant_id"],
            rls_enabled=r["rls_enabled"],
            rls_forced=r["rls_forced"],
            policies=tuple(r["policies"]),
            commands=tuple(r["commands"]),
            platform_scoped=bool(r["platform_scoped"]),
        )
        for r in rows
    ]


async def inspect_role(conn: AsyncConnection, app_role: str) -> list[dict[str, Any]]:
    rows = (await conn.execute(ROLE_SQL, {"app_role": app_role})).mappings().all()
    return [dict(r) for r in rows]


def mark_platform_scoped(op: Any, table: str, reason: str) -> None:
    """Platform-global tables carry an explicit, reviewable marker comment."""
    safe_reason = reason.replace("'", "''")
    op.execute(f"COMMENT ON TABLE {_check(table)} IS 'platform_scoped: {safe_reason}'")
