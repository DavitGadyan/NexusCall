# Runbook: provider outage (Twilio, SES, Anthropic, Deepgram, ElevenLabs, Google Calendar)

The platform degrades by design (build.md §10.3):
- timeouts on every call
- per-provider circuit breakers in Redis (`nexus_core.circuit`)
- bounded retries with jitter
- reconcile-before-retry for side effects
- a fallback line on the voice path

Your job is to confirm the provider is the cause, limit the damage (spend, duplicate side effects, bad customer experience) and recover cleanly.

## 1. Detect and confirm
- Signals:
  - `NexusServiceHighErrorRate` on call-service, voice-service, email-service or ai-gateway
  - the provider error-rate and latency panels
  - circuit-breaker-open log events (`ai.provider.circuit_opened` for the AI gateway)
  - `NexusTaskQueueBacklog` on `calls.dispatch` or `emails.campaign_send`
- Check the provider status page and our egress:
  ```bash
  kubectl -n nexuscall exec deploy/call-service -c app -- python -c \
    "import httpx;print(httpx.get('https://api.twilio.com',timeout=5).status_code)"
  ```
  A timeout from inside but a healthy status page suggests our egress: the NetworkPolicy, DNS, or a firewall change.
- Scope: one tenant (bad credentials → [secret rotation](secret-rotation.md)), one region, or everyone.

## 2. Contain, per provider

| Provider | Customer impact | Action |
|---|---|---|
| **Twilio** (calls) | New calls fail; live calls may drop | Pause outbound with the **platform calls kill switch** ([kill switches](kill-switches.md)) if dial attempts fail en masse, to save attempts and keep campaigns resumable. In-flight ambiguous calls stay `initiating` and the reconcile job resolves them after recovery. **Never** bulk-retry dispatch |
| **Deepgram / ElevenLabs** (voice) | Callers hear the fallback line; calls end gracefully | Usually leave campaigns running only if failures are short and intermittent. Otherwise engage the calls kill switch. Inbound calls still answer with disclosure and fallback |
| **Anthropic** (AI) | Voice turns fall back; email drafts queue up; drafting retries later | Platform → AI Gateway: the circuit breaker is automatic. If prolonged, set the AI kill switch for the provider (`scope: provider, target: anthropic`) so callers fail fast. Email drafts wait for human handling: threads still appear without a draft |
| **SES** (email) | Sends retry, then dead-letter | Pause email campaigns (tenant or platform **emails** kill switch) to protect sender reputation and quotas. Transactional and reply mail goes on its own queue (`emails.transactional_send`); it retries and then reaches the DLQ ([DLQ handling](dlq-handling.md)) |
| **SNS** (inbound mail, delivery events) | Events delayed | Nothing to do. SNS retries delivery, and events are deduplicated on arrival |
| **Google Calendar** | Booking tools fail | Tentative bookings still work in our database. External writes reconcile on retry (deterministic event id) |

Failover to another provider happens only where tenant policy, data residency and contracts allow it (build.md §24.1). Do not change routing policies ad hoc during an incident. They are reviewed configuration.

## 3. Recover
1. The provider is healthy (status page, plus a manual test call, test email or test completion from Platform → Integrations → Test).
2. Circuit breakers half-open and close on their own. Confirm the error rate is back to baseline.
3. Release the kill switches in reverse order, one channel at a time.
4. Let reconciliation finish before replaying anything:
   - call-service reconcile job (calls in `initiating` get the provider truth)
   - email-service provider events
5. Handle the DLQs per the DLQ runbook. Sends and dials reconcile before acting.
6. Check that campaign pacing resumes inside calling and sending windows. Windows are re-checked at dispatch, so nothing is called after hours.

## 4. Afterwards
- Count the impact: failed calls, delayed emails, AI fallbacks, and extra spend from analytics cost tracking.
- Write a post-incident review. Tune the timeouts and breaker thresholds if they reacted too late or too early.
- **Drill:** twice a year in staging. Block egress to one provider with a temporary NetworkPolicy deny, or stop the fake provider in development, and walk through §1–§3.
