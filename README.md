# NexusCall

A multi-tenant AI call center and agency platform. It covers CRM, inbound and outbound AI voice calls, call and email campaigns, AI-drafted email replies with human approval, and separate admin consoles for the platform and for each tenant.

- **Spec:** [`build.md`](build.md)
- **Architecture:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/SERVICE_MAP.md`](docs/SERVICE_MAP.md), [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md), [ADRs](docs/adr/)
- **Plan and status:** [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
- **Operations:** [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
- **Machine-readable architecture graph** (for mind-map and explorer UIs): [`docs/architecture/`](docs/architecture/)

## Quick start (local development)

Prerequisites: Docker (≥ 8 GB RAM for the full stack), [uv](https://docs.astral.sh/uv/) 0.13+, pnpm 11, Node 22, Python 3.12.

```bash
make env                         # creates .env with random, development-only secrets (gitignored)
make up PROFILE="core"           # infra + tenant/audit/notification services + web
make up PROFILE="core crm calls" # add CRM, campaigns and the call center
make up-all                      # everything, including observability
```

| URL | What |
|---|---|
| http://localhost:47000 | Web app (through the Traefik gateway) |
| http://localhost:47080 | Keycloak (realm `nexus`; dev users listed in `scripts/gen_keycloak_realm.py`) |
| http://localhost:47025 | Mailpit, which catches invitation and notification email |
| http://localhost:47672 | RabbitMQ management |
| http://localhost:47001 | Grafana (`obs` profile) |

Each service's OpenAPI document is in `contracts/openapi/`. With `ENV=development`, a service also serves `/docs`.

Local dev users share the password in `.env` (`KC_DEV_USER_PASSWORD`). Admin users are enrolled in TOTP with the documented dev-only key `NZSXQ5LTMRSXM5DPORYHGZLDOJSXIMBR`. Never reuse any of these outside a laptop.

## Commands

| Command | Purpose |
|---|---|
| `make test` | Unit, integration (testcontainers) and web tests |
| `make lint` / `make typecheck` | ruff, eslint, mypy --strict, tsc |
| `make migrate` / `make seed` | Run migrations as owner roles / load demo data |
| `make generate` | Regenerate compose, Traefik routes and the Keycloak realm from their generators |
| `make openapi` | Refresh contract snapshots (CI fails on drift) |

## Safety notes

- Provider fakes run only when `ENV` is `development` or `test`. Every service refuses `PROVIDER_MODE=fake` in staging or production.
- Real provider credentials are never put in `.env`. Add them in the UI (Integrations → Provider connections). They are written straight to Vault.
- This repository is not a compliance certification. Calling and email laws (TCPA, GDPR/ePrivacy, CAN-SPAM, CASL, recording consent and others) need legal review for every jurisdiction you operate in.
