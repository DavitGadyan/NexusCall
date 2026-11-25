# ADR-0009: HashiCorp Vault for all secrets

- Status: Accepted
- Date: 2026-11-25

## Decision
**Paths**
- Tenant secrets (KV v2 mount `secret`): `secret/{env}/tenants/{tenant_id}/{provider}/{connection_id}`.
- Platform secrets: `secret/{env}/platform/{name}`.

**Policies**
- `integration-service` is the only service with write/delete on `secret/{env}/tenants/*`.
- Every other service reads only the provider paths it needs (see SERVICE_MAP §5).
- Database credentials come from the Vault database secrets engine (dynamic `<svc>_app` logins) in production, and from static dev passwords locally.
- The transit engine encrypts sensitive non-secret fields that need reversibility, such as the BFF session key, and handles webhook-signing key operations.

**Workload authentication**
- Local: AppRole per service, with role-id/secret-id bootstrapped by `infra/vault/bootstrap.sh` into a tmpfs volume.
- Kubernetes: the Kubernetes auth method plus the Vault Agent Injector, which renders secrets to an in-memory volume.

**What stays out of Vault-adjacent systems**
- Application databases store only `vault_ref`, provider, masked identifier (only where explicitly approved), status, `last_validated_at` and rotation metadata.
- Queue messages carry `connection_id`. Workers resolve the secret at execution time.
- The Vault audit device is enabled, and secret values are never logged. A log-redaction test enforces this.

## Consequences
- The dev-mode root token is dev-only. `bootstrap.sh` refuses to run when `ENV != development`.
