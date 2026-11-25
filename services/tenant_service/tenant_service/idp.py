"""IdP administration adapter (ADR-0006). Keycloak implementation + deterministic in-memory fake.

Never returns or logs passwords, tokens or client secrets except the one-time
client secret handed straight to Vault by the caller.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class IdpUser:
    id: str
    email: str
    enabled: bool
    email_verified: bool
    first_name: str | None = None
    last_name: str | None = None
    has_password: bool = False
    has_otp: bool = False
    realm_roles: set[str] = field(default_factory=set)


@dataclass
class IdpSession:
    id: str
    ip: str | None
    started: int
    last_access: int
    clients: list[str]


class IdpError(Exception):
    pass


class IdpAdmin(Protocol):
    async def find_user_by_email(self, email: str) -> IdpUser | None: ...

    async def get_user(self, user_id: str) -> IdpUser | None: ...

    async def ensure_user(self, email: str) -> tuple[IdpUser, bool]: ...

    async def send_actions_email(
        self, user_id: str, actions: list[str], redirect_uri: str, client_id: str, lifespan_seconds: int
    ) -> None: ...

    async def set_enabled(self, user_id: str, enabled: bool) -> None: ...

    async def logout(self, user_id: str) -> None: ...

    async def sessions(self, user_id: str) -> list[IdpSession]: ...

    async def set_realm_role(self, user_id: str, role: str, present: bool) -> None: ...

    async def events(self, max_results: int = 100, event_types: list[str] | None = None) -> list[dict[str, Any]]: ...

    async def create_service_client(self, client_id: str, description: str) -> str: ...

    async def rotate_client_secret(self, client_id: str) -> str: ...

    async def set_client_enabled(self, client_id: str, enabled: bool) -> None: ...

    async def health(self) -> dict[str, Any]: ...


class KeycloakAdmin:
    def __init__(self, base_url: str, realm: str, token: Callable[[], Awaitable[str]], timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._token = token
        self._timeout = timeout

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(
                method,
                f"{self._base}/admin/realms/{self._realm}{path}",
                headers={"Authorization": f"Bearer {await self._token()}"},
                **kw,
            )
        if resp.status_code >= 400 and resp.status_code != 404:
            raise IdpError(f"Keycloak admin call failed ({method} {path.split('?')[0]}): {resp.status_code}")
        return resp

    def _user(self, d: dict[str, Any], creds: list[dict[str, Any]] | None = None) -> IdpUser:
        types = {c.get("type") for c in (creds or [])}
        return IdpUser(
            id=d["id"],
            email=d.get("email", ""),
            enabled=d.get("enabled", False),
            email_verified=d.get("emailVerified", False),
            first_name=d.get("firstName"),
            last_name=d.get("lastName"),
            has_password="password" in types,
            has_otp="otp" in types,
        )

    async def find_user_by_email(self, email: str) -> IdpUser | None:
        resp = await self._req("GET", "/users", params={"email": email, "exact": "true"})
        rows = resp.json() if resp.status_code == 200 else []
        return await self.get_user(rows[0]["id"]) if rows else None

    async def get_user(self, user_id: str) -> IdpUser | None:
        resp = await self._req("GET", f"/users/{user_id}")
        if resp.status_code == 404:
            return None
        creds = (await self._req("GET", f"/users/{user_id}/credentials")).json()
        user = self._user(resp.json(), creds)
        roles = (await self._req("GET", f"/users/{user_id}/role-mappings/realm")).json()
        user.realm_roles = {r["name"] for r in roles}
        return user

    async def ensure_user(self, email: str) -> tuple[IdpUser, bool]:
        existing = await self.find_user_by_email(email)
        if existing:
            return existing, False
        resp = await self._req(
            "POST", "/users", json={"username": email, "email": email, "enabled": True, "emailVerified": False}
        )
        if resp.status_code not in (201, 409):
            raise IdpError("Could not create IdP user")
        created = await self.find_user_by_email(email)
        if created is None:
            raise IdpError("IdP user not found after creation")
        return created, True

    async def send_actions_email(
        self, user_id: str, actions: list[str], redirect_uri: str, client_id: str, lifespan_seconds: int
    ) -> None:
        await self._req(
            "PUT",
            f"/users/{user_id}/execute-actions-email",
            params={"client_id": client_id, "redirect_uri": redirect_uri, "lifespan": lifespan_seconds},
            json=actions,
        )

    async def set_enabled(self, user_id: str, enabled: bool) -> None:
        await self._req("PUT", f"/users/{user_id}", json={"enabled": enabled})

    async def logout(self, user_id: str) -> None:
        await self._req("POST", f"/users/{user_id}/logout")

    async def sessions(self, user_id: str) -> list[IdpSession]:
        rows = (await self._req("GET", f"/users/{user_id}/sessions")).json()
        return [
            IdpSession(
                r["id"],
                r.get("ipAddress"),
                r.get("start", 0),
                r.get("lastAccess", 0),
                list(r.get("clients", {}).values()),
            )
            for r in rows
        ]

    async def set_realm_role(self, user_id: str, role: str, present: bool) -> None:
        rep = (await self._req("GET", f"/roles/{role}")).json()
        await self._req("POST" if present else "DELETE", f"/users/{user_id}/role-mappings/realm", json=[rep])

    async def events(self, max_results: int = 100, event_types: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"max": max_results}
        if event_types:
            params["type"] = event_types
        rows = (await self._req("GET", "/events", params=params)).json()
        # Strip details that may contain codes/tokens; keep what operators need.
        return [
            {
                "time": r.get("time"),
                "type": r.get("type"),
                "user_id": r.get("userId"),
                "client_id": r.get("clientId"),
                "ip": r.get("ipAddress"),
                "error": r.get("error"),
            }
            for r in rows
        ]

    async def _client_uuid(self, client_id: str) -> str:
        rows = (await self._req("GET", "/clients", params={"clientId": client_id})).json()
        if not rows:
            raise IdpError("Client not found")
        return str(rows[0]["id"])

    async def create_service_client(self, client_id: str, description: str) -> str:
        await self._req(
            "POST",
            "/clients",
            json={
                "clientId": client_id,
                "description": description,
                "enabled": True,
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "protocolMappers": [
                    {
                        "name": "nexus-api-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {"included.custom.audience": "nexus-api", "access.token.claim": "true"},
                    }
                ],
            },
        )
        return await self.rotate_client_secret(client_id)

    async def rotate_client_secret(self, client_id: str) -> str:
        cid = await self._client_uuid(client_id)
        return str((await self._req("POST", f"/clients/{cid}/client-secret")).json()["value"])

    async def set_client_enabled(self, client_id: str, enabled: bool) -> None:
        cid = await self._client_uuid(client_id)
        await self._req("PUT", f"/clients/{cid}", json={"enabled": enabled})

    async def health(self) -> dict[str, Any]:
        rep = (await self._req("GET", "")).json()
        return {
            "reachable": True,
            "realm": rep.get("realm"),
            "brute_force_protected": rep.get("bruteForceProtected"),
            "otp_policy": rep.get("otpPolicyType"),
            "access_token_lifespan": rep.get("accessTokenLifespan"),
            "revoke_refresh_token": rep.get("revokeRefreshToken"),
            "password_policy": rep.get("passwordPolicy"),
        }


class FakeIdp:
    """In-memory IdP for tests and local fakes. Deterministic; never used in production."""

    def __init__(self) -> None:
        self.users: dict[str, IdpUser] = {}
        self.actions_emails: list[tuple[str, list[str], str]] = []
        self.logouts: list[str] = []
        self.clients: dict[str, dict[str, Any]] = {}

    def add_user(self, user_id: str, email: str, *, verified: bool = True, roles: set[str] | None = None) -> IdpUser:
        u = IdpUser(user_id, email, True, verified, has_password=True, realm_roles=set(roles or set()))
        self.users[user_id] = u
        return u

    async def find_user_by_email(self, email: str) -> IdpUser | None:
        return next((u for u in self.users.values() if u.email == email), None)

    async def get_user(self, user_id: str) -> IdpUser | None:
        return self.users.get(user_id)

    async def ensure_user(self, email: str) -> tuple[IdpUser, bool]:
        existing = await self.find_user_by_email(email)
        if existing:
            return existing, False
        u = IdpUser(str(uuid.uuid4()), email, True, False)
        self.users[u.id] = u
        return u, True

    async def send_actions_email(
        self, user_id: str, actions: list[str], redirect_uri: str, client_id: str, lifespan_seconds: int
    ) -> None:
        self.actions_emails.append((user_id, actions, redirect_uri))

    async def set_enabled(self, user_id: str, enabled: bool) -> None:
        self.users[user_id].enabled = enabled

    async def logout(self, user_id: str) -> None:
        self.logouts.append(user_id)

    async def sessions(self, user_id: str) -> list[IdpSession]:
        return []

    async def set_realm_role(self, user_id: str, role: str, present: bool) -> None:
        (self.users[user_id].realm_roles.add if present else self.users[user_id].realm_roles.discard)(role)

    async def events(self, max_results: int = 100, event_types: list[str] | None = None) -> list[dict[str, Any]]:
        return []

    async def create_service_client(self, client_id: str, description: str) -> str:
        self.clients[client_id] = {"enabled": True, "description": description}
        return f"fake-secret-{uuid.uuid4()}"

    async def rotate_client_secret(self, client_id: str) -> str:
        return f"fake-secret-{uuid.uuid4()}"

    async def set_client_enabled(self, client_id: str, enabled: bool) -> None:
        self.clients[client_id]["enabled"] = enabled

    async def health(self) -> dict[str, Any]:
        return {"reachable": True, "realm": "fake"}
