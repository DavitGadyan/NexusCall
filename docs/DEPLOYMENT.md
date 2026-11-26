# NexusCall — Production deployment (Kubernetes)

Status: Phase 8 assets. **Nothing in this guide has been applied to a real cluster yet.** The chart and the Terraform pass static validation (`helm lint`, `helm template | kubeconform`, `terraform validate`; see `.github/workflows/deploy-checks.yml`). Do a full staging rehearsal before production.

Decision background: [ADR-0004](adr/0004-deployment-self-hosted-kubernetes.md) (generic Kubernetes), [ADR-0009](adr/0009-vault-secrets.md) (Vault), [ADR-0011](adr/0011-celery-rabbitmq-topology.md) / [ADR-0014](adr/0014-in-process-periodic-jobs.md) (queues, periodic jobs), [ADR-0013](adr/0013-local-object-storage-seaweedfs.md) (object storage).

## 1. What gets deployed

| Layer | Where | Managed by |
|---|---|---|
| Traefik (public `websecure`, internal `internal` entrypoint), cert-manager, issuers | `nexus-edge`, `cert-manager` | `infra/terraform/addons` |
| CloudNativePG cluster `nexus-pg` (PostgreSQL 16 + pgvector, 3 instances, WAL archiving + PITR), role/database bootstrap | `nexus-data`, `cnpg-system` | `infra/terraform/addons` + `charts/platform-data` |
| RabbitMQ quorum cluster `nexus-rabbitmq` and topology (vhost, exchanges, `<queue>.dlq`, `task-dlq` policy) | `nexus-data`, `rabbitmq-system` | `infra/terraform/addons` + `charts/platform-data` |
| Redis HA (Sentinel + HAProxy), SeaweedFS S3 (or external S3), ClamAV | `nexus-data`, `nexus-platform` | `infra/terraform/addons` |
| Vault HA (Raft, TLS, audit volume, Agent Injector) | `nexus-vault` | `infra/terraform/addons` |
| Vault policies, Kubernetes auth roles, KV v2, transit, database and RabbitMQ engines, audit device | Vault | `infra/terraform/vault-config` |
| Keycloak (keycloakx) | `nexus-identity` | `infra/terraform/addons` |
| kube-prometheus-stack, Loki, Tempo, OpenTelemetry collector | `nexus-observability` | `infra/terraform/addons` |
| 16 services, web BFF, Celery workers, edge routes, migration Job | `nexuscall` (Pod Security `restricted`) | `infra/helm/nexuscall` |

### Single sources of truth
`infra/helm/generate.py` derives the deployment topology from the development sources, so the two cannot drift. CI runs `generate.py --check`.

| Source | Generated |
|---|---|
| `scripts/gen_compose.py` `SERVICES` / `WORKERS` | `values.yaml` → `services` (with production env mapping) and `workers` |
| `scripts/gen_traefik.py` `render()` | `values.yaml` → `routes` (SSE `/api/v1/notifications/stream` and WebSocket `/media/twilio` unbuffered, upload body limits, webhook routes split off) |
| `infra/vault/bootstrap.sh` `write_policy` calls | `infra/terraform/generated/topology.json` → `vault_policies` |
| `scripts/bootstrap_rabbitmq.py` `PUBLISH`, `TASK_QUEUES`, `TASK_DLQ_POLICY_PATTERN` | `topology.json` → `broker_roles`, `dlq_policy` |
| `nexus_core.celery_app.QUEUES` (+ worker queues) | `topology.json` → `task_queues`, `dead_letter_queues` |

The generator fails on anything it cannot classify, so a person has to decide each case:
- a new compose env key without a production mapping (`PROD_ENV`)
- an unknown Traefik middleware
- a service-to-service call found in the code but missing from the NetworkPolicy dependency table (`DEPENDS`)
- a change to the broker permission code

```bash
uv run python infra/helm/generate.py          # after changing any source above
uv run python infra/helm/generate.py --check  # what CI runs
```

## 2. Prerequisites
- Kubernetes ≥ 1.29 with a CNI that enforces NetworkPolicy (Calico, Cilium, …), a default StorageClass and a LoadBalancer implementation.
- Terraform ≥ 1.6 (validated with 1.15), Helm ≥ 3.14 (validated with 4.2), kubectl, the `vault` CLI.
- An S3-compatible bucket **outside the cluster** for PostgreSQL backups. A different failure domain from the SeaweedFS data is mandatory.
- DNS for the app host (`app.example.com`) and the identity host (`id.example.com`).
- Release images in your registry for the 16 services, `web` and `tools` (`infra/docker/tools.Dockerfile`), built by CI. Pin them by digest.
- Real provider accounts (Twilio, SES, Anthropic, Deepgram, ElevenLabs, Google), configured later through the UI. The chart refuses `providerMode` other than `real`, and services refuse fakes outside development.

## 3. Install order

```text
1  terraform -chdir=infra/terraform/addons apply -target=helm_release.cert_manager \
       -target=helm_release.traefik -target=kubernetes_namespace_v1.this
2  out-of-band Secrets (§4.1)
3  terraform -chdir=infra/terraform/addons apply                # everything else
4  vault operator init / unseal (§4.2)
5  terraform -chdir=infra/terraform/vault-config apply           # policies, roles, engines (§4.3)
6  Vault KV secrets (§4.4), Keycloak realm (§5)
7  helm upgrade --install nexuscall infra/helm/nexuscall …       # migrations run first (§6)
8  smoke test, RLS scan, alerts green (§7)
```

Step 1 is needed only on a fresh cluster. The local charts that create cert-manager and Traefik custom resources must find their CRDs. Use one state per environment: `backend.tf.example` shows S3 with a lock file, or a Kubernetes backend. Variables go in `<env>.tfvars` (see `terraform.tfvars.example`). No secret values are passed to the `addons` module.

## 4. Secrets bootstrap
Secret **values** never appear in git, Helm values, images or Terraform code. Only two sensitive Terraform variables exist. Both are bootstrap admin passwords for `vault-config`, passed as `TF_VAR_*`, and both are rotated straight after the first apply (§4.3).

### 4.1 Kubernetes Secrets created by the platform team (before step 3)
| Secret (namespace) | Keys | Used by |
|---|---|---|
| `nexus-pg-vault-admin` (nexus-data) | `username=vault_admin`, `password` | CNPG managed role. Vault's database engine connects with it |
| `keycloak-db-credentials` (nexus-data **and** nexus-identity) | `username=keycloak`, `password` | CNPG managed role and Keycloak |
| `nexus-pg-backup-s3` (nexus-data) | `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` | Barman Cloud plugin (backup bucket only) |
| `redis-auth` (nexus-data) | `password` (alphanumeric) | Redis HA. The same value goes into Vault `platform/shared/redis` |
| `seaweedfs-s3-config` (nexus-data) | S3 identities JSON: one identity per service that uses storage, least-privilege actions | SeaweedFS |
| `grafana-admin` (nexus-observability) | `admin-user`, `admin-password` | Grafana |
| `vault-ca` (nexuscall) | `ca.crt` from Secret `nexus-internal-ca` (cert-manager) | Vault agent TLS verification |

Generate passwords with `openssl rand -hex 32` (alphanumeric). Create the Secrets with `kubectl create secret generic … --from-literal`, from a shell that does not keep history, or with your sealed-secrets / external-secrets tooling.

### 4.2 Vault initialisation
```bash
kubectl -n nexus-vault exec vault-0 -- vault operator init -key-shares=5 -key-threshold=3 -format=json > init.json   # offline, split custody
kubectl -n nexus-vault exec vault-0 -- vault operator unseal   # ×3, then the same on vault-1 / vault-2 (they join via retry_join)
```
Store the unseal keys and recovery material offline, under split custody (two people). Use the root token only for step 5. Then create an operator policy and token, and run `vault token revoke -self` on the root token. To avoid manual unsealing after restarts, set `vault_seal_stanza` (transit or cloud KMS auto-unseal).

### 4.3 Vault configuration (`infra/terraform/vault-config`)
```bash
export VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=./vault-ca.crt VAULT_TOKEN=<operator token>
kubectl -n nexus-vault port-forward svc/vault 8200 &
export TF_VAR_postgres_vault_admin_password=$(kubectl -n nexus-data get secret nexus-pg-vault-admin -o jsonpath='{.data.password}' | base64 -d)
export TF_VAR_rabbitmq_vault_admin_password=$(kubectl -n nexus-data get secret vault-admin-user-credentials -o jsonpath='{.data.password}' | base64 -d)
terraform -chdir=infra/terraform/vault-config apply -var environment=production
vault write -f database/rotate-root/nexus-pg     # the password in state is now useless
```
This creates the following:

| What | Details |
|---|---|
| One policy per service | Mirrors `bootstrap.sh`, plus `database/creds/<db>-{app,inspector,platform}`, `rabbitmq/creds/<service>` and `platform/shared/redis` |
| `web-bff` | — |
| `nexus-migrations` | `database/static-creds/<db>-owner` |
| Kubernetes auth roles | Role = service name, bound to the ServiceAccount of the same name in `nexuscall`, audience `vault`, TTL 1 h / max 4 h |
| Database secrets engine | Dynamic members of `<db>_app` (NOSUPERUSER, NOBYPASSRLS, connection limit 40, 24 h lease, 7 d max) and static `<db>_owner` rotated daily |
| RabbitMQ secrets engine | Per-service users with the vhost and topic permissions of `bootstrap_rabbitmq.py` |
| Other | KV v2 `secret`, transit keys `bff-session` and `webhook-signing`, file audit device |

### 4.4 Vault KV secrets (written by an operator, read by exactly one service each)
All paths are under `secret/<env>/`, set with `vault kv put secret/production/<path> field=value`.

| Path | Fields | Notes |
|---|---|---|
| `platform/<service>/keycloak-client` | `client_secret` | One per `svc-<service>` Keycloak client, all 16 |
| `platform/<service>/s3` | `access_key_id`, `secret_access_key` | tenant, crm, voice, ai-agent, email-inbox, analytics, audit |
| `platform/shared/redis` | `password` | Same value as Secret `redis-auth` |
| `platform/call-service/stream-token` **and** `platform/voice-service/stream-token` | `secret` | **One shared value in both paths.** call-service signs media-stream tokens and voice-service verifies them. Rotate both together ([runbook](runbooks/signing-and-session-keys.md)) |
| `platform/integration-service/hmac-key` | `secret` | HMAC key for credential-test idempotency hashes (32 random bytes, hex). integration-service refuses to start in staging/production without it |
| `platform/web-bff/oidc` | `client_secret` | `web-bff` client |
| `platform/web-bff/session` | `key` | 64 hex characters |
| `platform/tenant-service/rabbitmq-ops` | `username=nexus-ops`, `password` | Copy from Secret `nexus-ops-user-credentials` (topology operator) |
| `platform/ai-gateway/anthropic`, `platform/voice-service/{deepgram,elevenlabs}`, `platform/email-service/ses`, `platform/notification-service/ses` | provider fields (BUILD_CONVENTIONS §5) | Platform defaults. Tenant credentials are written by integration-service through the UI |

## 5. Identity (Keycloak)
- Keycloak is served at `https://<identity_host>/realms/...`. The admin console is **not** routed publicly; reach it with `kubectl port-forward`.
- The development realm (`infra/keycloak/realm-nexus.json`, generated) is **not** a production realm. It contains development users and a password grant. Create the production realm from it with:
  - no users
  - no direct grants
  - the same clients (`web-bff` confidential with PKCE; `svc-<service>` client-credentials with audience `nexus-api`), each with a new secret written to Vault (§4.4)
  - MFA required for admin roles
  - access tokens of 5 minutes, and rotating refresh tokens with reuse detection (ADR-0006)
- Manage the realm as reviewed configuration, for example with keycloak-config-cli or the Terraform Keycloak provider. That tooling is not part of this repository yet.

## 6. Application release

### Values
```bash
helm upgrade --install nexuscall infra/helm/nexuscall -n nexuscall \
  -f infra/helm/nexuscall/values-production.yaml \
  --set global.image.digest=sha256:<from CI>   # or global.image.tag=<release>
```

Things you must fill in, because the render fails otherwise:
- `global.publicBaseUrl`, `global.publicHost`, `global.oidc.issuer`, `global.image.registry` and the tag/digest
- `services.notification-service.env.NOTIFICATION_FROM_ADDRESS`
- `global.forwardedAllowIps` (pod CIDR), `ingress.tls.issuerRef`, `global.objectStore.publicEndpointUrl`
- `global.endpoints.objectstore.cidrs` if S3 is external

Render-time guards refuse any of the following:
- `env` other than staging/production
- `providerMode` other than `real`
- per-component `PROVIDER_MODE`/`ENV` overrides
- development-only env such as `MCP_ALLOW_HTTP_PRIVATE` or `MAILPIT_SMTP_HOST`
- empty, `dev` or `latest` image tags
- a namespace that does not enforce Pod Security `restricted`

### Migrations
A `pre-install,pre-upgrade` Helm hook Job runs `scripts/migrate_all.py` in the tools image, once, before any pod changes. It connects as each `<db>_owner`, with credentials rendered by a pre-populate-only Vault agent from `database/static-creds/<db>-owner`. If it fails, the release stops and the old pods keep serving. The failed Job is kept for `kubectl logs`.

Migrations must be **expand/contract** (backward compatible with the running version):
1. Add columns, tables or policies in release N.
2. Switch the code over.
3. Remove the old schema only in N+1.

This is what makes rollback and canary safe. RLS policies change only through migrations (ADR-0015).

### What every pod gets
| Area | Details |
|---|---|
| Security context | uid/gid 10001, `runAsNonRoot`, read-only root filesystem, all capabilities dropped, seccomp `RuntimeDefault`, no privilege escalation |
| ServiceAccount token | Not mounted (`automountServiceAccountToken: false`). Only the Vault agent gets a 10-minute, audience-bound projected token |
| Secrets | Rendered by the Vault agent into the in-memory `/vault/secrets/env`, sourced by the container command |
| Vault access | Through the agent's local cache listener `127.0.0.1:8200` |
| Probes | Startup and liveness on `/healthz`, readiness on `/readyz`. Web uses `/api/health`. Workers have no HTTP probe |
| Resource requests/limits | From the generated defaults, overridable per environment |
| Scheduling | Topology spread across nodes and zones, a PDB (`maxUnavailable: 1`), and an HPA (CPU 70 %; `hpa.metrics` takes a custom metric such as concurrent calls) |
| NetworkPolicies | Default-deny ingress and egress, plus DNS. Each component may reach only its `dependsOn` services (SERVICE_MAP §2, checked against the code) and its infrastructure. Public egress on 443 only for the provider-calling services (call, voice, ai-gateway, email, email-inbox, notification, tool-execution, booking, integration) |

## 7. Rollout
1. In CI (`deploy-checks.yml`): the generator check, `helm lint`, `helm template | kubeconform` (staging and production values), `terraform fmt -check` and `validate`.
2. `helm upgrade --install … --wait --timeout 15m`. Rolling updates use `maxUnavailable: 0`. A `preStop` delay lets Traefik and the endpoints drain. voice-service has a 600 s termination grace so live calls can finish. Workers have 330 s (1830 s for `worker-ai-agent`), so in-flight tasks complete. Tasks are `acks_late`, so a killed worker's task is redelivered.
3. Gates before you call the release done:
   - `k6 run tests/load/smoke.js` against the environment, under its safety flag
   - Platform → Security → RLS coverage scan: 0 issues
   - no firing `NexusServiceHighErrorRate`, `NexusPodsNotReady` or `NexusDeadLetterQueueNotEmpty` alerts
   - Vault agent init containers healthy

### Canary (built into the chart)
```yaml
canary:
  ai-gateway: { tag: "1.4.0-rc1", replicas: 1, weight: 10 }
```
This renders `ai-gateway-canary` (Deployment and Service, label `nexuscall.io/track: canary`) and a weighted `TraefikService`. 10 % of **edge** traffic goes to the canary. In-cluster calls keep using the stable Service. For a worker, a canary takes a share of the queue in proportion to its replicas.

1. Promote by setting the stable tag and removing the `canary` entry.
2. Abort by removing the entry.
3. Watch error rate and latency by `track` while it runs.

The migration Job runs once for the whole release, so canary code must work with the same schema.

### Blue-green (documented alternative)
For changes that cannot be canaried (major Traefik routing changes, a new IdP client):
1. Install a second release (`nexuscall-green`) in its own namespace with its own ServiceAccounts and Vault roles (`app_namespace` variable).
2. Point it at the same data services. Migrations must already be expand-compatible.
3. Switch the `Host()` route, or DNS, from blue to green.
4. Keep blue scaled for the rollback window, then uninstall it.

Two releases double the consumers on the same queues, which is fine (idempotent tasks), and double the periodic jobs, which is fine (Redis-locked, ADR-0014).

## 8. Rollback
- **Application:** `helm rollback nexuscall <revision> --wait`. This is safe only because migrations are expand/contract. The old code ignores new columns. Do not run Alembic downgrades in production unless a reviewed down-migration exists and you have a fresh backup.
- **Bad migration:** the Job fails, so the release stops and the old pods keep serving. Fix it forward in a new release. If data was damaged, restore with PITR ([backup and restore drill](runbooks/backup-restore.md)).
- **Add-ons:** pin back `chart_versions` and `terraform apply`. Stateful operator upgrades (CNPG, RabbitMQ, Vault) follow their own upgrade notes, and need a backup taken first.

## 9. Operations
Runbooks are in [`docs/runbooks/`](runbooks/):
- [backup and restore drill](runbooks/backup-restore.md)
- [secret rotation and compromise](runbooks/secret-rotation.md)
- [signing and session keys](runbooks/signing-and-session-keys.md)
- [break-glass](runbooks/break-glass.md)
- [DLQ](runbooks/dlq-handling.md)
- [provider outage](runbooks/provider-outage.md)
- [kill switches](runbooks/kill-switches.md)

Compliance mapping: [COMPLIANCE_CHECKLIST.md](COMPLIANCE_CHECKLIST.md).

## 10. Known gaps (read before production)
1. **Credential lifetime.** Services read `DATABASE_URL` and `AMQP_URL` once, at start. Dynamic database and broker leases have a 7-day maximum, after which Vault revokes the user and terminates its sessions. **Every pod must be restarted within 7 days.** Any normal release does this; otherwise run `kubectl rollout restart deploy -n nexuscall` weekly. A proper fix needs file-based credential reload in `nexus_core` (integration request).
2. **In-cluster TLS.** These links are TLS: PostgreSQL (`hostssl` only), Vault, Keycloak's database connection and the public edge. Redis, AMQP and in-cluster HTTP between services are plaintext inside NetworkPolicy-restricted networks. Enabling `amqps` (`global.rabbitmq.scheme`), Redis TLS or a service mesh with mTLS is still open.
3. **Third-party chart values** (Traefik, Vault, keycloakx, redis-ha, SeaweedFS, Loki, Tempo, OTel, kube-prometheus-stack) were not rendered against their charts. Only the Terraform configuration was validated. Run `terraform plan` on staging and fix any key renames. `chart_versions` were current on 2026-11-25.
4. ClamAV runs as root-capable in `nexus-platform` (Pod Security `baseline`), as the upstream image requires. Observability runs `privileged`, which node-exporter and log collection need.
5. A production Keycloak realm definition is not included (§5).
