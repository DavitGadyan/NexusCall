# ADR-0006: Keycloak as IdP with a Next.js BFF session

- Status: Accepted (default assumption, reversible)
- Date: 2026-11-25

## Decision
**Keycloak**
- Keycloak 26.x, realm `nexus`.
- Browser login uses Authorization Code + PKCE through the confidential client `web-bff`.
- Access tokens last 5 minutes. Refresh tokens rotate, with reuse detection enabled.
- MFA (OTP or WebAuthn) is required for platform roles and tenant owners/admins.
- Step-up uses the `acr` levels `aal1` and `aal2` with `max_age`.

**Where authorization data lives**
- Tenant membership and roles are **not** stored as Keycloak groups. They live in tenant-service, keyed by the Keycloak `sub`, so a user can hold a different role in each tenant.
- The only Keycloak realm roles are `platform_owner` and `platform_operator`. They are mirrored in tenant-service for audit.

**BFF session**
- Next.js route handlers keep tokens in an encrypted Redis session (AES-GCM, key from Vault).
- The browser gets an opaque `__Host-nx_sid` cookie (HttpOnly, Secure, SameSite=Lax) and a CSRF double-submit token.

**Service-to-service calls**
- Each service has a Keycloak client `svc-<name>` and uses the client-credentials grant with an audience-restricted token.
- Internal endpoints check the token's `azp` against an allowlist.

## Consequences
- tenant-service wraps the Keycloak Admin API behind an `IdpAdmin` adapter, which handles invitation user creation, session revocation and service accounts.
- Auth0 or Entra ID can replace Keycloak later through OIDC discovery and a different `IdpAdmin` adapter.
