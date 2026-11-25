# ADR-0004: Self-hosted / generic Kubernetes deployment

- Status: Accepted (user decision)
- Date: 2026-11-25

## Decision
- **Local:** Docker Compose. Kubernetes is never required for development.
- **Production:** any conformant Kubernetes cluster (v1.29 or later), deployed with a Helm umbrella chart (`infra/helm/nexuscall`).
- **Stateful dependencies** run in-cluster through operators: CloudNativePG (PostgreSQL + pgvector), the RabbitMQ Cluster Operator (quorum queues), Redis (Bitnami or operator), MinIO Operator, Vault (HA Raft, Helm), and Keycloak (Operator).
- **Terraform** (`infra/terraform`) installs the cluster add-ons above, plus ingress (Traefik), cert-manager and kube-prometheus-stack, Loki and Tempo. It uses the helm/kubernetes providers so it stays cloud-agnostic.
- **External SaaS** (Twilio, SES, Anthropic, Deepgram, ElevenLabs) is reached over controlled egress.

## Consequences
- The platform team owns backup and DR for the operators. The runbook covers CNPG PITR to S3-compatible storage and Vault Raft snapshots.
- Moving to a managed cloud later only needs values overrides: managed Postgres DSNs and external MQ.
