"""Tenancy, IAM, invitations, settings, support access; RLS on every tenant-owned table.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy.dialects.postgresql import insert

from nexus_core.rls import apply_tenant_rls, grant_platform_table, mark_platform_scoped
from tenant_service.catalogue import PLATFORM_PERMISSIONS, SYSTEM_ROLES, TENANT_PERMISSIONS
from tenant_service.tables import PLATFORM_SCOPED, TENANT_TABLES, metadata, permissions, roles

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP = "tenant_app"
PLATFORM_GRANTS = {
    "users": "SELECT, INSERT, UPDATE",
    "tenants": "SELECT, INSERT, UPDATE",
    "permissions": "SELECT",
    "roles": "SELECT, INSERT, UPDATE",
    "role_permissions": "SELECT, INSERT, DELETE",
    "membership_directory": "SELECT, INSERT, UPDATE, DELETE",
    "invitation_tokens": "SELECT, INSERT, DELETE",
    "platform_role_assignments": "SELECT, INSERT, DELETE",
    "platform_settings": "SELECT, INSERT, UPDATE",
    "feature_flags": "SELECT, INSERT, UPDATE",
    "service_accounts": "SELECT, INSERT, UPDATE",
    "rls_scans": "SELECT, INSERT",
    "privileged_requests": "SELECT, INSERT, UPDATE",
}
TENANT_GRANTS = {"membership_roles": "SELECT, INSERT, DELETE", "tenant_settings": "SELECT, INSERT, UPDATE"}


def upgrade() -> None:
    bind = op.get_bind()
    new = [t for name, t in metadata.tables.items() if name in PLATFORM_SCOPED or name in TENANT_TABLES]
    metadata.create_all(bind, tables=new)
    for name, reason in PLATFORM_SCOPED.items():
        mark_platform_scoped(op, name, reason)
        grant_platform_table(op, name, APP, PLATFORM_GRANTS[name])
    for name in TENANT_TABLES:
        apply_tenant_rls(op, name, APP, privileges=TENANT_GRANTS.get(name, "SELECT, INSERT, UPDATE, DELETE"))
    # Audit trail must not be erasable through the app: no DELETE on settings history.
    sync_catalogue()


def sync_catalogue() -> None:
    for key, desc in {**TENANT_PERMISSIONS, **PLATFORM_PERMISSIONS}.items():
        op.execute(
            insert(permissions)
            .values(key=key, scope="platform" if key.startswith("platform:") else "tenant", description=desc)
            .on_conflict_do_update(index_elements=["key"], set_={"description": desc})
        )
    for role in SYSTEM_ROLES:
        op.execute(
            insert(roles)
            .values(
                key=role.key,
                name=role.name,
                scope=role.scope,
                description=role.description,
                is_system=True,
                requires_mfa=role.requires_mfa,
            )
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"name": role.name, "description": role.description, "requires_mfa": role.requires_mfa},
            )
        )
        op.execute(f"DELETE FROM role_permissions WHERE role_id = (SELECT id FROM roles WHERE key = '{role.key}')")
        for p in sorted(role.permissions):
            op.execute(
                f"INSERT INTO role_permissions (role_id, permission_key) "
                f"SELECT id, '{p}' FROM roles WHERE key = '{role.key}'"
            )


def downgrade() -> None:
    for name in reversed([*TENANT_TABLES, *PLATFORM_SCOPED]):
        op.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
