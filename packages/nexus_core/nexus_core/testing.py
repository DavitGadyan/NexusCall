"""Test utilities shared by service test suites (never imported by production code).

* `ServiceDb` provisions a real PostgreSQL database with the same three-role layout
  as production (owner / app / inspector), runs the service's Alembic migrations
  as the owner, and hands out an app-role URL so RLS is always exercised.
* `TokenFactory` mints RS256 tokens and a JWKS for an in-process TokenVerifier.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from nexus_core.auth.jwt import JWKSCache, TokenVerifier

TEST_ISSUER = "http://idp.test/realms/nexus"
TEST_AUDIENCE = "nexus-api"
PASSWORD = "test-only-password"  # noqa: S105 - ephemeral container credential


@dataclass
class TokenFactory:
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    kid: str = "test-kid"

    @property
    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def verifier(self) -> TokenVerifier:
        cache = JWKSCache("http://unused.test/jwks")
        cache.set_keys(self.jwks)
        return TokenVerifier(cache, issuer=TEST_ISSUER, audience=TEST_AUDIENCE)

    def user(
        self,
        sub: str | uuid.UUID,
        email: str,
        *,
        roles: list[str] | None = None,
        mfa: bool = False,
        verified: bool = True,
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": str(sub),
            "iat": now,
            "exp": now + 300,
            "azp": "web-bff",
            "preferred_username": email,
            "email": email,
            "email_verified": verified,
            "name": email.split("@")[0],
            "realm_access": {"roles": roles or []},
            "acr": "aal2" if mfa else "aal1",
            "auth_time": now,
            **extra,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def service(self, client_id: str) -> str:
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 300,
            "azp": client_id,
            "preferred_username": f"service-account-{client_id}",
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


def auth(token: str, tenant_id: str | uuid.UUID | None = None, **headers: str) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}", **headers}
    if tenant_id is not None:
        h["X-Tenant-ID"] = str(tenant_id)
    return h


@dataclass
class ServiceDb:
    host: str
    port: int
    db: str

    def url(self, role: str) -> str:
        return f"postgresql+asyncpg://{self.db}_{role}:{PASSWORD}@{self.host}:{self.port}/{self.db}"

    @property
    def app_url(self) -> str:
        return self.url("app")

    @property
    def owner_url(self) -> str:
        return self.url("owner")


async def provision_async(
    container: Any, db: str, *, extensions: tuple[str, ...] = ("pgcrypto", "btree_gist")
) -> ServiceDb:
    import asyncpg

    host, port = container.get_container_host_ip(), int(container.get_exposed_port(5432))
    su = await asyncpg.connect(
        user=container.username, password=container.password, host=host, port=port, database=container.dbname
    )
    try:
        for role, extra in (("owner", ""), ("app", ""), ("inspector", ""), ("platform", "")):
            name = f"{db}_{role}"
            exists = await su.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
            if not exists:
                await su.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{PASSWORD}' NOSUPERUSER NOBYPASSRLS {extra}")
        if not await su.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db):
            await su.execute(f"CREATE DATABASE {db} OWNER {db}_owner")
    finally:
        await su.close()
    conn = await asyncpg.connect(
        user=container.username, password=container.password, host=host, port=port, database=db
    )
    try:
        await conn.execute(f"""
            REVOKE ALL ON DATABASE {db} FROM PUBLIC;
            GRANT CONNECT ON DATABASE {db} TO {db}_app, {db}_inspector, {db}_platform;
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            ALTER SCHEMA public OWNER TO {db}_owner;
            GRANT USAGE ON SCHEMA public TO {db}_app, {db}_inspector, {db}_platform;
            ALTER DEFAULT PRIVILEGES FOR ROLE {db}_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {db}_app;
        """)
        for ext in extensions:
            await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    finally:
        await conn.close()
    return ServiceDb(host, port, db)


def provision_and_migrate(container: Any, db: str, service_dir: Path, **kw: Any) -> ServiceDb:
    """Sync helper for session fixtures (Alembic's async env uses its own event loop)."""
    import asyncio

    sdb = asyncio.run(provision_async(container, db, **kw))
    run_migrations(service_dir, sdb.owner_url)
    return sdb


def run_migrations(service_dir: Path, owner_url: str) -> None:
    from alembic.config import main as alembic_main

    os.environ["MIGRATION_DATABASE_URL"] = owner_url
    try:
        alembic_main(argv=["-c", str(service_dir / "alembic.ini"), "upgrade", "head"])
    except SystemExit as exc:  # alembic exits on error
        if exc.code:
            raise RuntimeError(f"migrations failed for {service_dir.name}") from exc


def start_postgres(image: str = "pgvector/pgvector:0.8.0-pg16") -> Any:
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(image, driver=None)
    container.start()
    return container
