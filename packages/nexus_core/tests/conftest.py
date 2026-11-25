import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "http://idp.test/realms/nexus"
AUDIENCE = "nexus-api"


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(rsa_key):
    import json

    jwk = json.loads(RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk.update({"kid": "test-kid", "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


@pytest.fixture
def make_token(rsa_key):
    def _make(**overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 300,
            "nbf": now - 1,
            "azp": "web-bff",
            "preferred_username": "alice",
            "email": "alice@example.com",
            "realm_access": {"roles": []},
            "acr": "1",
            "auth_time": now,
        }
        claims.update(overrides)
        headers = {"kid": overrides.pop("_kid", "test-kid")} if "_kid" not in claims else {"kid": claims.pop("_kid")}
        return jwt.encode(claims, rsa_key, algorithm="RS256", headers=headers)

    return _make
