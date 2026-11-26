# NexusCall load tests (k6)

These scripts have not been run against a stack yet. They were only parsed with `k6 inspect`. Treat the thresholds as targets to confirm, not as measured results.

## Targets (docs/IMPLEMENTATION_PLAN.md §1, decision 8)

| Target | Profile | Pass criteria |
|---|---|---|
| Edge and service health | `smoke.js` | errors < 1 %, edge p95 < 300 ms, API p95 < 500 ms |
| Dashboard reads, 100 VUs | `read_mix.js` | errors < 1 %, p95 < 500 ms, p99 < 1.5 s |
| **200 concurrent calls** (fake telephony) | `campaign_soak.js` | live calls reach ≥ 90 % of the target, **never exceed the cap**, idempotent launch, API p95 < 500 ms |
| **100,000 emails per day** (fake SES) | `email_campaign.js` | sustained `RATE_PER_MINUTE` (default 300/min, 5× the daily average of ~70/min), API p95 < 700 ms, no duplicate sends |

## Safety rules
- **Local targets only.** `lib/common.js` refuses any `BASE_URL` that is not localhost (or `*.localhost` / `host.docker.internal`). Only for a staging environment you own, with provider sandbox accounts, set `ALLOW_NON_LOCAL_TARGET=i-own-this-non-production-target`. Never point these scripts at production.
- **Fake providers only for the campaign profiles.** `campaign_soak.js` and `email_campaign.js` launch real campaigns, so every contact in the segment is dialled or mailed. They refuse to start unless `FAKE_PROVIDERS_CONFIRMED=yes`. Check `PROVIDER_MODE=fake` in your `.env` first.
- Use demo data and `@example.test` / `+1 555 01xx` contacts only. Tokens are passed on the command line. Never commit one.

## Prerequisites
1. k6 ≥ 1.0 (`brew install k6`, or use the image: `docker run --rm -i --network host -v "$PWD/tests/load:/scripts" grafana/k6:1.4.2 run /scripts/smoke.js`).
2. A local stack with the profiles under test, for example `make up PROFILE="core crm calls"` for calls or `PROFILE="core crm email"` for email, plus `make seed`. This needs a machine with enough memory. The full stack takes ~8 GB of Docker memory.
3. **A token.** Use the dev realm's direct grant, which is enabled only in the development realm, for a demo user:
   ```bash
   TOKEN=$(curl -s -d grant_type=password -d client_id=web-bff -d client_secret="$KC_WEB_BFF_CLIENT_SECRET" \
     -d username=owner@acme.test -d password="$KC_DEV_USER_PASSWORD" -d scope=openid \
     http://localhost:47080/realms/nexus/protocol/openid-connect/token | jq -r .access_token)
   ```
   If the realm forbids the password grant, sign in through the UI and copy the access token from the BFF session (development only). Access tokens last 5 minutes. For longer runs, refresh the token and restart the run.

## Running
```bash
k6 run tests/load/smoke.js
k6 run -e TOKEN=$TOKEN tests/load/read_mix.js
k6 run -e TOKEN=$TOKEN -e FAKE_PROVIDERS_CONFIRMED=yes -e TARGET_CONCURRENCY=200 -e SOAK=30m tests/load/campaign_soak.js
k6 run -e TOKEN=$TOKEN -e FAKE_PROVIDERS_CONFIRMED=yes -e RATE_PER_MINUTE=300 -e SOAK=20m tests/load/email_campaign.js
```
Common parameters are `BASE_URL` (default `http://localhost:47000`), `TOKEN` and `TENANT_ID` (default: the acme demo tenant). Each script's header comment lists its own parameters.

### Reaching 200 concurrent calls
The defaults are deliberately low, so a soak needs explicit, audited settings changes on a **local** stack:
1. Platform → Settings → Limits: raise `max_concurrent_calls_per_tenant` to ≥ 200 (step-up required).
2. Tenant admin → Communication: raise `max_concurrent_calls` to 200 and `max_daily_calls` above the contact count.
3. Give the segment ≥ 1,000 contactable test contacts. Import a CSV (`POST /api/v1/imports`) of fictitious numbers `+1 NXX 555 01xx` across several area codes, with emails `@example.test`. The seed has only 40 contacts per tenant.
4. Make the fake Twilio calls long enough (scripted call duration) that 200 overlap at the configured `CALLS_PER_MINUTE`.

Alternatively, run the profile against several demo tenants in parallel (`TENANT_ID=...`), each under the default platform cap of 50.

### Reaching 100k emails per day
Run `email_campaign.js` with `SOAK=24h`, `RATE_PER_MINUTE=70` and a segment of ≥ 100,000 test contacts. Alternatively, run a short burst at a higher rate and extrapolate. Raise the tenant `email_daily_quota` within the platform limit. Mailpit receives every message, so cap its storage.

## What to watch while it runs
- The Grafana "services RED" dashboard, plus RabbitMQ queue depth and `*.dlq` counts. A DLQ must stay at 0.
- `/api/v1/calls/live` never exceeding the cap. `nexus_concurrency_over_cap` must be 0, and the profile fails otherwise.
- PostgreSQL connections (the sum of the pool sizes) and Redis memory.
- After the run: no duplicate calls per campaign contact attempt, and no duplicate email per (campaign, contact). Check these in the campaign reports.
