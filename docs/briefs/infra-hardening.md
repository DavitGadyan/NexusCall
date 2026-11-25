# Brief: production deployment and hardening (Phase 8)

**Paths you own:** `infra/helm/**`, `infra/terraform/**`, `tests/load/**`, `docs/runbooks/**`, `docs/DEPLOYMENT.md`, `docs/COMPLIANCE_CHECKLIST.md`, `.github/workflows/deploy-checks.yml`.
**Do not edit:** services, packages, `docker-compose.yml`, `docs/RUNBOOK.md`, other docs.
**Read:** build.md §9.2, §10, §14 Phase 8, §19.1, §19.4, §20.3, §23; ADR-0004, 0009, 0011, 0013, 0014; `docs/SERVICE_MAP.md` (dependencies); `scripts/gen_compose.py` (services, env); `infra/vault/bootstrap.sh` (policies); `scripts/bootstrap_rabbitmq.py` (broker permissions).
**Tools installed:** helm 4, terraform 1.15, kubectl, kubeconform. k6 is not installed.

## Helm umbrella chart `infra/helm/nexuscall`
- One reusable template set renders these for every entry in `values.yaml`: Deployment, Service, ServiceAccount, HPA, PodDisruptionBudget, NetworkPolicy.
- `values.yaml` holds the 16 services, web, and a `workers` list (name, owning image, command, queues, replicas).
- Probes: `/healthz` and `/readyz`.
- Security context:
  - runAsNonRoot, uid 10001
  - readOnlyRootFilesystem
  - drop ALL capabilities
  - seccomp RuntimeDefault
  - no ServiceAccount token unless needed
- Resource requests and limits.
- Migration pre-upgrade Job: the tools image, running `scripts/migrate_all.py` with owner credentials from Vault.
- Vault Agent Injector annotations: role = the service name. Secrets are rendered to an in-memory volume. DB credentials come from the database secrets engine.
- Traefik IngressRoute: TLS, rate limit, security headers, and separate webhook and media (WebSocket) routes.
- NetworkPolicies:
  - Default-deny.
  - Explicit allows taken from the SERVICE_MAP dependency table.
  - External egress only for the services that call providers.
  - DNS allowed.
- Namespace Pod Security level `restricted`.
- The chart must `fail` when `PROVIDER_MODE` is `fake`.
- Example values for staging and production.
- A canary or blue-green strategy, in the chart or documented.

## Terraform `infra/terraform`
Cluster add-ons through the helm and kubernetes providers:
- Traefik, cert-manager.
- CloudNativePG, with a Cluster (pgvector image, backups and PITR to S3-compatible storage).
- RabbitMQ Cluster Operator, with a quorum cluster.
- Redis.
- Object storage: SeaweedFS, or external S3 through variables.
- Vault:
  - HA raft
  - kubernetes auth roles and policies per service, mirroring `bootstrap.sh`
  - database secrets engine
  - audit device
- Keycloak.
- kube-prometheus-stack, Loki, Tempo, OTel collector.

Use variables, keep secrets out of the code, and include a remote state example.

## Validation
Run and report on:
- `helm lint`
- `helm template ... | kubeconform -strict -ignore-missing-schemas`
- `terraform fmt -check`
- `terraform init -backend=false && terraform validate`. If this fails only because of network or provider download, say so.

Add a CI workflow that runs the same checks.

## Load tests `tests/load`
- k6 scripts:
  - a health and latency smoke test
  - an authenticated read mix
  - a campaign launch and fake-call soak profile
  - an email campaign profile
- Parametrize by `BASE_URL` and a token.
- A README with the targets (200 concurrent calls, 100k emails a day) and how to run against the fake providers.
- The scripts are not executed here. You may check the syntax with the `grafana/k6` image.

## Docs
- `docs/DEPLOYMENT.md`: prerequisites, install order, secrets bootstrap, migrations, rollout and rollback.
- `docs/runbooks/`:
  - backup and restore drill (Postgres, Vault, object store)
  - secret rotation, overlapping and non-overlapping, and compromise response
  - break-glass exercise
  - DLQ handling
  - provider outage
  - kill switches
  - signing-key and session-key rotation
- `docs/COMPLIANCE_CHECKLIST.md`: control → where it is implemented → evidence → open legal questions per jurisdiction (TCPA, GDPR/ePrivacy, CAN-SPAM, CASL, recording consent). State explicitly that it is not legal advice and not a certification.

This brief has no Python service work, so the pytest steps in `_common.md` do not apply. The validation commands above are your definition of done.
