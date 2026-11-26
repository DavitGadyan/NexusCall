# NexusCall

A multi-tenant AI call center and agency platform. It covers CRM, inbound and outbound AI voice calls, call and email campaigns, AI-drafted email replies with human approval, and separate admin consoles for the platform and for each tenant.

- **Spec:** [`build.md`](build.md)
- **Architecture:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/SERVICE_MAP.md`](docs/SERVICE_MAP.md), [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md), [ADRs](docs/adr/)
- **Plan and status:** [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
- **Operations:** [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
- **Machine-readable architecture graph** (for mind-map and explorer UIs): [`docs/architecture/`](docs/architecture/)

## Run locally

Prerequisites: Docker with at least 8 GB of memory, [uv](https://docs.astral.sh/uv/) 0.13+, pnpm 11, Node 22, Python 3.12.

```bash
make env          # first time only: creates .env with random dev-only secrets (git-ignored)
make up-all       # builds and starts everything; the first build takes about 10 minutes
make seed         # first time only: demo tenants Acme and Globex, demo users and data
make ps           # every service shows (healthy) after 1-2 minutes
```

Open **http://localhost:47000** and sign in.

| User | Password | Use it for |
|---|---|---|
| `campaigns@acme.test` | `KC_DEV_USER_PASSWORD` in `.env` | Tenant workspace (no TOTP) |
| `owner@acme.test` | same | Tenant admin console (TOTP) |
| `platform.owner@nexus.local` | same | Platform admin console (TOTP) |

TOTP users: add the dev-only key `NZSXQ5LTMRSXM5DPORYHGZLDOJSXIMBR` to an authenticator app. All dev users are listed in `scripts/gen_keycloak_realm.py`.

Less memory? Start only what you need: `make up PROFILE="core crm"` (profiles: `core`, `crm`, `calls`, `email`, `platform`, `obs`).

| URL | What |
|---|---|
| http://localhost:47000 | Web app |
| http://localhost:47080 | Keycloak (realm `nexus`) |
| http://localhost:47200 | Vault UI (token: `VAULT_DEV_ROOT_TOKEN` in `.env`) |
| http://localhost:47672 | RabbitMQ management |
| http://localhost:47001 | Grafana (`obs` profile) |

Stop with `make down` (data is kept). `make clean` deletes all local data, **including the keys in Vault**.

### Provider keys

The app calls the real providers. Keys are never put in `.env`. They are stored in Vault, and they stay there across restarts. Add each key **one** of two ways:

- **UI:** sign in as `platform.owner@nexus.local` → Platform admin → **Integrations** → add the provider.
- **Shell:** `scripts/vault_put_platform_secret.sh <path> <field>`. The value is read from a hidden prompt.

| Provider | Needed for | Vault path (`<path>`) | Fields |
|---|---|---|---|
| Anthropic | AI replies, call agent, email drafts | `ai-gateway/anthropic` | `api_key` |
| Voyage | Knowledge-base search (embeddings) | `ai-gateway/voyage` | `api_key` |
| Twilio | Inbound and outbound calls | `call-service/twilio` and `voice-service/twilio` | `account_sid`, `auth_token` |
| Deepgram | Speech-to-text on calls | `voice-service/deepgram` | `api_key` |
| ElevenLabs | Text-to-speech on calls | `voice-service/elevenlabs` | `api_key` |
| Amazon SES | Campaign email, invitations, inbound email | `email-service/ses`, `notification-service/ses`, `email-inbox-service/ses` | `region`, `access_key_id`, `secret_access_key` |
| Google OAuth app | Calendar connections for bookings | `integration-service/google_oauth_client` | `client_id`, `client_secret` |
| HubSpot OAuth app | HubSpot CRM sync | `integration-service/hubspot_oauth_client` | `client_id`, `client_secret` |

Example: `scripts/vault_put_platform_secret.sh voice-service/deepgram api_key`. Run it once for each field.

SES also needs a verified sender: set `NOTIFICATION_FROM_ADDRESS` in `.env` to an address or domain verified in SES. While the SES account is in the sandbox, it can send only to verified addresses.

A tenant can also use its own Twilio, SES or calendar account: tenant admin → **Provider connections**. A tenant key takes priority over the platform default.

**Webhooks:** Twilio and SES must reach the app from the internet. On a laptop, run `ngrok http 47000`, set `NEXUS_PUBLIC_BASE_URL` in `.env` to the ngrok HTTPS URL, then run `make up-all` again. Without this, outbound actions still work, but call status, inbound calls and inbound email do not arrive.

### Troubleshooting

- `open ~/.docker/buildx/current: permission denied`: an earlier `sudo docker` left root-owned files. In a normal terminal, run `sudo chown -R $(id -un):staff ~/.docker/buildx`.
- A service is not healthy: `make logs SERVICE=<name>`, for example `make logs SERVICE=ai-gateway`.

## Commands

| Command | Purpose |
|---|---|
| `make test` | Unit, integration (testcontainers) and web tests |
| `make lint` / `make typecheck` | ruff, eslint, mypy --strict, tsc |
| `make migrate` / `make seed` | Run migrations as owner roles / load demo data |
| `make generate` | Regenerate compose, Traefik routes and the Keycloak realm from their generators |
| `make openapi` | Refresh contract snapshots (CI fails on drift) |

## Safety notes

- The provider simulator in `services/fake_providers` is used by the automated tests only. It is not part of the running stack, and every service refuses `PROVIDER_MODE=fake` in staging or production.
- Real provider credentials are never put in `.env`. They are written straight to Vault (see Provider keys).
- This repository is not a compliance certification. Calling and email laws (TCPA, GDPR/ePrivacy, CAN-SPAM, CASL, recording consent and others) need legal review for every jurisdiction you operate in.
