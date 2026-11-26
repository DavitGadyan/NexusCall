# Runbook: secret rotation and compromise response

Scope: every credential NexusCall holds (build.md §19.4). Secret values are never pasted into tickets, chat, logs or shell history. Use `vault kv put … field=@file` from a tmpfs file, or `-` with stdin, and delete the file afterwards. Every rotation is audited: by the Vault audit device, and by the integration-service audit events when it runs through the UI.

## 1. Inventory and method

| Credential | Where | Overlapping? | Method |
|---|---|---|---|
| Dynamic DB users (`<db>_app` members) | Vault database engine | yes (automatic) | Leases expire; restart pods within 7 days (DEPLOYMENT §10) |
| `<db>_owner` (migrations) | Vault static role | yes | Rotated daily by Vault. Manual: `vault write -f database/rotate-role/<db>-owner` |
| `vault_admin` (DB engine root) | Vault | n/a | `vault write -f database/rotate-root/nexus-pg` |
| Broker service users | Vault RabbitMQ engine | yes (automatic) | Leases; restart pods within 7 days |
| `vault-admin` broker user | K8s Secret + Vault config | no | §3 non-overlapping |
| Redis password | K8s `redis-auth` + Vault `platform/shared/redis` | no | §3 non-overlapping |
| Keycloak service clients `svc-*`, `web-bff` | Keycloak + Vault KV | yes (Keycloak supports rotated client secrets) | §2 overlapping |
| Tenant provider credentials (Twilio, SES, Anthropic, Deepgram, ElevenLabs, Google, HubSpot) | Vault `secret/<env>/tenants/...`, written only by integration-service | depends on provider | UI: Integrations → connection → Rotate (§2). Twilio, AWS and Anthropic allow two active keys |
| Platform provider defaults | Vault `platform/<service>/<provider>` | usually yes | §2 |
| S3 identities per service | SeaweedFS config + Vault `platform/<service>/s3` | yes (add, then remove the identity) | §2 |
| Stream-token, webhook-signing, BFF session key | Vault | see [signing and session keys](signing-and-session-keys.md) | — |
| Vault unseal / recovery keys | offline, split custody | no | `vault operator rekey` ceremony, yearly or when a custodian leaves |

## 2. Overlapping rotation (preferred)
1. **Create** a second credential at the provider (or Keycloak "rotate client secret" with the old one kept valid for 24 h).
2. **Validate** it.
   - Tenant credentials: UI → Integrations → Test connection. The server-side test runs before anything is saved, and never echoes the value.
   - Platform credentials: a scoped test call from a debug pod that has only that policy.
3. **Write** it to Vault at the same path (KV v2 keeps the previous version):
   ```bash
   vault kv put secret/production/platform/ai-gateway/anthropic api_key=@/dev/shm/new_key
   ```
4. **Switch** the workloads.
   - Tenant credentials are read at execution time, with a 60 s resolver cache, so they switch on their own.
   - Env-rendered platform secrets (`keycloak-client`, `s3`, `stream-token`) need a restart: `kubectl -n nexuscall rollout restart deploy/<service>`, and its workers. The agent re-renders `/vault/secrets/env` at start.
5. **Verify health**: readiness, provider error rate on the dashboard, and one real transaction (a test call, test email or test completion).
6. **Revoke** the old credential at the provider. Then `vault kv metadata get` shows a new version, and `vault kv destroy -versions=<old>` deletes the old one.
7. **Record** it: who, when, which reference, and that the old credential is revoked. Use reference and version only, never the value.

## 3. Non-overlapping rotation (planned downtime or degradation)
Examples are the Redis password and the RabbitMQ `vault-admin` user.
1. Announce a window. Expected impact:
   - **Redis:** about 1–2 minutes of rate-limit, lock and session errors. Users may have to sign in again. Periodic jobs pause, which fails closed.
2. Set the new value in Vault **first** (`platform/shared/redis`), then in the K8s Secret `redis-auth`.
3. `kubectl -n nexus-data rollout restart statefulset/redis-ha-server deploy/redis-ha-haproxy`, then `kubectl -n nexuscall rollout restart deploy`.
4. Verify:
   - readiness green
   - web login works
   - `ratelimit` and `scheduling` metrics are flowing
5. **Rollback:** put the previous KV version back (`vault kv rollback -version=<n> secret/production/platform/shared/redis`), restore the Secret, and restart again.
6. For `vault-admin` on RabbitMQ:
   1. Update the user's password through the topology operator (Secret `vault-admin-user-credentials`).
   2. Re-run `terraform -chdir=infra/terraform/vault-config apply` with the new `TF_VAR_rabbitmq_vault_admin_password`.
   3. Existing service users are unaffected.

## 4. Suspected compromise
Start the clock and open an incident (severity 1 if tenant data or provider spend is at risk).
1. **Contain.** Revoke at the source within minutes:
   - Provider console: disable or delete the key.
   - Vault: `vault kv metadata delete secret/production/tenants/<tenant>/<provider>/<connection>` for tenant credentials, or disable the connection in the UI. The integration-service revoke action deletes the secret and emits `integration.connection.changed`.
   - Leaked Vault token or lease: `vault token revoke <accessor>`. For everything a role issued: `vault lease revoke -prefix database/creds/<db>-app` and `vault lease revoke -prefix rabbitmq/creds/<service>`.
2. **Stop dependent work.** Dependent jobs fail as permanent once the credential is gone. If abuse is ongoing (toll fraud, spam), engage the tenant or platform [kill switch](kill-switches.md) for that channel.
3. **Invalidate sessions and tokens.**
   - Keycloak: sign out all sessions of the affected users. For a client secret leak, regenerate the client secret and revoke its tokens.
   - BFF sessions: rotate the session key ([signing and session keys](signing-and-session-keys.md)) if the session store or key may be exposed.
4. **Rotate** using §2/§3. Treat a compromise as non-overlapping: the old credential is gone before the new one works.
5. **Investigate.** Use the Vault audit log (who read the path; values are HMAC'd), the audit service (`integration.*` and `support.*` events), provider logs, and egress logs.
6. **Notify:**
   - platform admins (automatic from `integration.connection.changed` / `notification.requested`)
   - the tenant owner, for tenant credentials
   - counsel, if personal data may be affected: breach-notification clocks may apply (for example 72 h under GDPR; counsel decides)
7. **Close.** Write a post-incident review and update this runbook.

## 5. Drill (twice a year, staging)
Rotate one overlapping platform credential (a Keycloak `svc-*` client) and one non-overlapping one (Redis) end to end. Simulate a compromise of a tenant Twilio connection by revoking it through the UI, and check:
- calls fail closed with `provider_not_configured`
- the tenant owner is notified
- the audit trail is complete

Record the time taken for each step.
