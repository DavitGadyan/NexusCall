#!/usr/bin/env python3
"""Generate infra/keycloak/realm-nexus.json (DEVELOPMENT realm).

Secrets are NOT embedded: Keycloak resolves ${ENV_VAR} placeholders at import
time from the container environment (.env, gitignored). Dev user IDs are fixed
so tenant-service seed data can reference them deterministically.

Step-up: a level-of-authentication browser flow. LoA 1 = password, LoA 2 =
password + OTP. Clients map LoA levels to ACR names `aal1`/`aal2`; services require
`acr=aal2` for high-risk actions (build.md §20.2).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "infra" / "keycloak" / "realm-nexus.json"

SERVICES = [
    "tenant-service",
    "crm-service",
    "campaign-service",
    "call-service",
    "voice-service",
    "ai-agent-service",
    "ai-gateway",
    "mcp-registry",
    "tool-execution",
    "booking-service",
    "email-service",
    "email-inbox-service",
    "integration-service",
    "notification-service",
    "analytics-service",
    "audit-service",
]

# Dev TOTP secret (raw Keycloak secret string). Authenticator key = base32(raw). DEV ONLY.
DEV_TOTP_RAW = "nexusdevtotpsecret01"

DEV_USERS = [
    # (id, email, first, last, realm_roles, has_otp)
    ("00000000-0000-4000-8000-000000000001", "platform.owner@nexus.local", "Paula", "Owner", ["platform_owner"], True),
    (
        "00000000-0000-4000-8000-000000000002",
        "platform.operator@nexus.local",
        "Oscar",
        "Operator",
        ["platform_operator"],
        True,
    ),
    ("00000000-0000-4000-8000-000000000101", "owner@acme.test", "Ada", "Acme-Owner", [], True),
    ("00000000-0000-4000-8000-000000000102", "admin@acme.test", "Alan", "Acme-Admin", [], True),
    ("00000000-0000-4000-8000-000000000103", "campaigns@acme.test", "Cara", "Campaigns", [], False),
    ("00000000-0000-4000-8000-000000000104", "agent@acme.test", "Ari", "Agent", [], False),
    ("00000000-0000-4000-8000-000000000105", "email@acme.test", "Emma", "Email", [], True),
    ("00000000-0000-4000-8000-000000000106", "analyst@acme.test", "Ana", "Analyst", [], False),
    ("00000000-0000-4000-8000-000000000201", "owner@globex.test", "Gus", "Globex-Owner", [], True),
    ("00000000-0000-4000-8000-000000000301", "multi@both.test", "Max", "Multi", [], True),
]


FLOW_IDS = {
    "browser-step-up": "6d1b7a1e-0000-4000-8000-00000000f001",
    "step-up-forms": "6d1b7a1e-0000-4000-8000-00000000f002",
    "loa-1": "6d1b7a1e-0000-4000-8000-00000000f003",
    "loa-2": "6d1b7a1e-0000-4000-8000-00000000f004",
}


def audience_mapper() -> dict:
    return {
        "name": "nexus-api-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.custom.audience": "nexus-api",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "introspection.token.claim": "true",
        },
    }


def service_client(name: str) -> dict:
    return {
        "clientId": f"svc-{name}",
        "name": f"{name} service identity",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "secret": "${KC_DEV_SERVICE_CLIENT_SECRET}",
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "frontchannelLogout": False,
        "attributes": {"access.token.lifespan": "300", "client.secret.creation.time": "0"},
        "protocolMappers": [audience_mapper()],
    }


def otp_credential() -> dict:
    return {
        "type": "otp",
        "userLabel": "dev-authenticator",
        "secretData": json.dumps({"value": DEV_TOTP_RAW}),
        "credentialData": json.dumps(
            {"subType": "totp", "digits": 6, "counter": 0, "period": 30, "algorithm": "HmacSHA1"}
        ),
    }


def user(uid: str, email: str, first: str, last: str, roles: list[str], otp: bool) -> dict:
    creds = [{"type": "password", "value": "${KC_DEV_USER_PASSWORD}", "temporary": False}]
    if otp:
        creds.append(otp_credential())
    return {
        "id": uid,
        "username": email,
        "email": email,
        "emailVerified": True,
        "firstName": first,
        "lastName": last,
        "enabled": True,
        "credentials": creds,
        "realmRoles": ["default-roles-nexus", *roles],
    }


def loa_flow() -> list[dict]:
    def cond(level: int, alias: str) -> dict:
        return {
            "id": FLOW_IDS[alias],
            "alias": alias,
            "description": f"LoA {level}",
            "providerId": "basic-flow",
            "topLevel": False,
            "builtIn": False,
            "authenticationExecutions": [
                {
                    "authenticatorConfig": f"{alias}-config",
                    "authenticator": "conditional-level-of-authentication",
                    "requirement": "REQUIRED",
                    "priority": 0,
                    "autheticatorFlow": False,
                    "userSetupAllowed": False,
                },
                {
                    "authenticator": "auth-username-password-form" if level == 1 else "auth-otp-form",
                    "requirement": "REQUIRED",
                    "priority": 1,
                    "autheticatorFlow": False,
                    "userSetupAllowed": False,
                },
            ],
        }

    return [
        {
            "id": FLOW_IDS["browser-step-up"],
            "alias": "browser-step-up",
            "description": "Browser flow with level-of-authentication step-up",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
            "authenticationExecutions": [
                {
                    "authenticator": "auth-cookie",
                    "requirement": "ALTERNATIVE",
                    "priority": 10,
                    "autheticatorFlow": False,
                    "userSetupAllowed": False,
                },
                {
                    "requirement": "ALTERNATIVE",
                    "priority": 20,
                    "autheticatorFlow": True,
                    "flowAlias": "step-up-forms",
                    "userSetupAllowed": False,
                },
            ],
        },
        {
            "id": FLOW_IDS["step-up-forms"],
            "alias": "step-up-forms",
            "description": "LoA subflows",
            "providerId": "basic-flow",
            "topLevel": False,
            "builtIn": False,
            "authenticationExecutions": [
                {
                    "requirement": "CONDITIONAL",
                    "priority": 10,
                    "autheticatorFlow": True,
                    "flowAlias": "loa-1",
                    "userSetupAllowed": False,
                },
                {
                    "requirement": "CONDITIONAL",
                    "priority": 20,
                    "autheticatorFlow": True,
                    "flowAlias": "loa-2",
                    "userSetupAllowed": False,
                },
            ],
        },
        cond(1, "loa-1"),
        cond(2, "loa-2"),
    ]


def realm() -> dict:
    clients = [
        {
            "clientId": "web-bff",
            "name": "NexusCall Web (BFF)",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": "${KC_WEB_BFF_CLIENT_SECRET}",
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "implicitFlowEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": ["${NEXUS_WEB_ORIGIN}/api/auth/callback"],
            "webOrigins": ["${NEXUS_WEB_ORIGIN}"],
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris": "${NEXUS_WEB_ORIGIN}/",
                "acr.loa.map": json.dumps({"aal1": 1, "aal2": 2}),
                "default.acr.values": "aal1",
                "minimum.acr.value": "aal1",
                "access.token.lifespan": "300",
                "use.refresh.tokens": "true",
            },
            "authenticationFlowBindingOverrides": {"browser": FLOW_IDS["browser-step-up"]},
            "protocolMappers": [audience_mapper()],
        },
        *[service_client(s) for s in SERVICES],
    ]
    users = [user(*u) for u in DEV_USERS]
    users.append(
        {
            "username": "service-account-svc-tenant-service",
            "enabled": True,
            "serviceAccountClientId": "svc-tenant-service",
            "clientRoles": {"realm-management": ["manage-users", "view-users", "query-users", "view-events", "manage-clients", "view-clients", "view-realm"]},
            "realmRoles": ["default-roles-nexus"],
        }
    )
    return {
        "realm": "nexus",
        "enabled": True,
        "displayName": "NexusCall (development)",
        "sslRequired": "external",
        "registrationAllowed": False,
        "resetPasswordAllowed": True,
        "rememberMe": False,
        "verifyEmail": True,
        "loginWithEmailAllowed": True,
        "duplicateEmailsAllowed": False,
        "bruteForceProtected": True,
        "failureFactor": 5,
        "waitIncrementSeconds": 60,
        "maxFailureWaitSeconds": 900,
        "permanentLockout": False,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 36000,
        "revokeRefreshToken": True,
        "refreshTokenMaxReuse": 0,
        "passwordPolicy": "length(12) and notUsername and notEmail",
        "otpPolicyType": "totp",
        "otpPolicyAlgorithm": "HmacSHA1",
        "otpPolicyDigits": 6,
        "otpPolicyPeriod": 30,
        "eventsEnabled": True,
        "adminEventsEnabled": True,
        "adminEventsDetailsEnabled": False,
        "enabledEventTypes": [
            "LOGIN",
            "LOGIN_ERROR",
            "LOGOUT",
            "CODE_TO_TOKEN",
            "REFRESH_TOKEN_ERROR",
            "UPDATE_TOTP",
            "REMOVE_TOTP",
            "UPDATE_PASSWORD",
        ],
        "eventsExpiration": 2592000,
        "roles": {
            "realm": [
                {"name": "platform_owner", "description": "Platform owner (tightly controlled)"},
                {"name": "platform_operator", "description": "Platform operator"},
            ]
        },
        "clients": clients,
        "users": users,
        "authenticationFlows": loa_flow(),
        "authenticatorConfig": [
            {"alias": "loa-1-config", "config": {"loa-condition-level": "1", "loa-max-age": "36000"}},
            {"alias": "loa-2-config", "config": {"loa-condition-level": "2", "loa-max-age": "300"}},
        ],
        "smtpServer": {
            "host": "mailpit",
            "port": "1025",
            "from": "no-reply@nexus.local",
            "fromDisplayName": "NexusCall (dev)",
        },
    }


def main() -> None:
    OUT.write_text(json.dumps(realm(), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
