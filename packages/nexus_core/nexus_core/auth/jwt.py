"""OIDC access-token verification against the IdP JWKS (build.md §5.1).

Checks signature, algorithm allowlist, issuer, audience, exp and nbf. Unknown
`kid` triggers one rate-limited JWKS refresh to support key rotation. Claims are
never trusted before verification.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from nexus_core.errors import Unauthorized


@dataclass(frozen=True, slots=True)
class Principal:
    sub: str
    azp: str | None
    is_service: bool
    email: str | None = None
    email_verified: bool = False
    name: str | None = None
    realm_roles: frozenset[str] = field(default_factory=frozenset)
    acr: str | None = None
    auth_time: int | None = None
    session_id: str | None = None
    raw_claims: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Principal:
        roles = frozenset(claims.get("realm_access", {}).get("roles", []))
        username = str(claims.get("preferred_username", ""))
        return cls(
            sub=str(claims["sub"]),
            azp=claims.get("azp"),
            is_service=username.startswith("service-account-") or claims.get("typ_principal") == "service",
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified", False)),
            name=claims.get("name") or username or None,
            realm_roles=roles,
            acr=claims.get("acr"),
            auth_time=claims.get("auth_time"),
            session_id=claims.get("sid"),
            raw_claims=claims,
        )

    def authenticated_within(self, seconds: int) -> bool:
        return self.auth_time is not None and (time.time() - self.auth_time) <= seconds


class JWKSCache:
    def __init__(self, jwks_url: str, *, min_refresh_interval: float = 30.0, http: httpx.AsyncClient | None = None):
        self._url = jwks_url
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._min_refresh = min_refresh_interval
        self._lock = asyncio.Lock()
        self._http = http

    def set_keys(self, jwks: dict[str, Any]) -> None:
        keys: dict[str, PyJWK] = {}
        for raw in jwks.get("keys", []):
            if raw.get("use", "sig") != "sig" or "kid" not in raw:
                continue
            try:
                keys[raw["kid"]] = PyJWK(raw)
            except jwt.PyJWKError:
                continue
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def _refresh(self) -> None:
        async with self._lock:
            if time.monotonic() - self._fetched_at < self._min_refresh and self._keys:
                return
            client = self._http or httpx.AsyncClient(timeout=5.0)
            try:
                resp = await client.get(self._url)
                resp.raise_for_status()
                self.set_keys(resp.json())
            finally:
                if self._http is None:
                    await client.aclose()

    async def get(self, kid: str) -> PyJWK:
        if kid not in self._keys:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise Unauthorized("Unknown signing key")
        return key


class TokenVerifier:
    def __init__(
        self,
        jwks: JWKSCache,
        *,
        issuer: str,
        audience: str,
        algorithms: tuple[str, ...] = ("RS256",),
        leeway: int = 30,
    ) -> None:
        if any(a.lower() == "none" or a.startswith("HS") for a in algorithms):
            raise ValueError("Only asymmetric algorithms are permitted for access tokens")
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms)
        self._leeway = leeway

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise Unauthorized("Malformed token") from exc
        alg = header.get("alg")
        if alg not in self._algorithms:
            raise Unauthorized("Token algorithm not allowed")
        kid = header.get("kid")
        if not kid:
            raise Unauthorized("Token missing key id")
        key = await self._jwks.get(kid)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_nbf": True},
            )
        except jwt.ExpiredSignatureError as exc:
            raise Unauthorized("Token expired") from exc
        except jwt.PyJWTError as exc:
            raise Unauthorized("Invalid token") from exc
        return Principal.from_claims(claims)
