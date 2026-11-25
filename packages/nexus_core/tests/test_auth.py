import time

import pytest

from nexus_core.auth.jwt import JWKSCache, TokenVerifier
from nexus_core.authz import has_permission
from nexus_core.errors import Unauthorized

ISSUER = "http://idp.test/realms/nexus"
AUDIENCE = "nexus-api"


@pytest.fixture
def verifier(jwks):
    cache = JWKSCache("http://unused/jwks")
    cache.set_keys(jwks)
    return TokenVerifier(cache, issuer=ISSUER, audience=AUDIENCE, leeway=0)


async def test_valid_token(verifier, make_token):
    p = await verifier.verify(make_token(realm_access={"roles": ["platform_owner"]}))
    assert "platform_owner" in p.realm_roles
    assert not p.is_service


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "http://evil/realms/nexus"},
        {"aud": "other-api"},
        {"exp": int(time.time()) - 10},
        {"nbf": int(time.time()) + 3600},
    ],
)
async def test_rejects_bad_claims(verifier, make_token, override):
    with pytest.raises(Unauthorized):
        await verifier.verify(make_token(**override))


async def test_rejects_unknown_kid(verifier, make_token, monkeypatch):
    async def no_refresh(self):
        return None

    monkeypatch.setattr(JWKSCache, "_refresh", no_refresh)
    with pytest.raises(Unauthorized):
        await verifier.verify(make_token(_kid="rotated-away"))


async def test_rejects_hs256_and_none(verifier):
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "exp": time.time() + 60, "iat": time.time()},
        "secret",
        algorithm="HS256",
        headers={"kid": "test-kid"},
    )
    with pytest.raises(Unauthorized):
        await verifier.verify(forged)
    with pytest.raises(Unauthorized):
        await verifier.verify("eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")


def test_verifier_refuses_symmetric_algorithms():
    with pytest.raises(ValueError):
        TokenVerifier(JWKSCache("x"), issuer=ISSUER, audience=AUDIENCE, algorithms=("HS256",))


def test_service_principal_detection(make_token):
    import jwt as pyjwt

    from nexus_core.auth.jwt import Principal

    claims = pyjwt.decode(
        make_token(preferred_username="service-account-svc-crm", azp="svc-crm"), options={"verify_signature": False}
    )
    assert Principal.from_claims(claims).is_service


@pytest.mark.parametrize(
    ("granted", "required", "ok"),
    [
        ({"leads:read"}, "leads:read", True),
        ({"leads:read"}, "leads:update", False),
        ({"emails:*"}, "emails:drafts:approve", True),
        ({"emails:drafts:*"}, "emails:drafts:approve", True),
        ({"emails:drafts:review"}, "emails:drafts:approve", False),
        ({"platform:*"}, "platform:tenants:manage", True),
        ({"*"}, "audit:read", True),
    ],
)
def test_has_permission(granted, required, ok):
    assert has_permission(frozenset(granted), required) is ok
