# Runbook: dead-letter queue (DLQ) handling

**Topology** (ADR-0011; `infra/terraform/generated/topology.json`):
- Every Celery task queue (`calls.*`, `voice.postprocess`, `emails.*`, `ai.knowledge_ingest`, `integrations.webhooks`, `imports.process`, `analytics.aggregate`, `notifications.send`) is a quorum queue with `x-dead-letter-exchange = nexus.dlx` and `x-delivery-limit = 6`. The `task-dlq` policy (`TASK_DLQ_POLICY_PATTERN`) sets the same.
- A message lands in `<queue>.dlq`, bound to `nexus.dlx` with routing key `<queue>.dlq`, when:
  - it was redelivered more than 6 times (worker crashes or timeouts)
  - a task exhausted its retries (`TransientFailure` with backoff)
  - it was rejected as malformed
- Permanent business failures (opt-out, invalid recipient, no permission) are **not** dead-lettered. The task records the failure in the owning database and acks.

**Alert:** `NexusDeadLetterQueueNotEmpty` (any `*.dlq` > 0 for 5 min). `NexusTaskQueueBacklog` covers a growing live queue.

## Rules
- Inspect, replay and discard happen **only** through Platform → Infrastructure → Dead-letter jobs:
  - `GET /api/v1/platform/infra/dead-letters`
  - `POST /api/v1/platform/infra/dead-letters/replay`
  - `POST /api/v1/platform/infra/dead-letters/discard`

  They require `platform:queues:replay` and are audited. Replays are re-published with a new `causation_id`. The console uses the restricted `nexus-ops` broker user (read `*.dlq`, write `nexus.tasks` only).
- Never use the RabbitMQ management UI's "move messages" or shovel by hand in production: it is unaudited.
- Messages carry IDs only (no bodies, no secrets), so reading them is safe. Do not copy tenant ids into public channels.

## Triage (in this order)
1. **Which queue, how many, since when?** Check the Dead-letter jobs view, and Grafana for the queue-depth panel and the `x-death` reason counts.
2. **Is it one tenant, one provider, or everything?**
   - Everything in `calls.dispatch` / `emails.campaign_send` right after a deploy: suspect the release. Roll back (DEPLOYMENT §8). Do not replay into the broken version.
   - One provider: see [provider outage](provider-outage.md). Replay only after the circuit breaker closes.
   - One tenant: check its provider connection status (Integrations) and kill-switch state.
3. **Read the worker logs** for the correlation id shown in the DLQ entry: `kubectl -n nexuscall logs deploy/worker-calls | grep <correlation_id>`. Logs carry ids and error codes only.
4. **Classify:**

   | Cause | Action |
   |---|---|
   | Transient, fixed now (provider back, DB failover finished) | **Replay** |
   | Bug, fixed in a new release | Deploy the fix first, then **replay** |
   | Poison or malformed (schema mismatch, deleted resource) | **Discard**, with a reason. The owning row stays in its failed or queued state and is visible in the UI |
   | Side-effect tasks with unknown outcome (`calls.dispatch`, `emails.*send*`) | **Do not replay blindly.** Tasks reconcile before acting (call-service looks the provider call up by idempotency token; email sends carry provider idempotency keys). Confirm reconciliation is enabled for that path, then replay. Duplicate deliveries are no-ops by design (claim `UPDATE … WHERE status='queued'`) |

5. **Replay in small batches** (for example 50), and watch the error rate and the DLQ refilling. Stop if anything re-dead-letters.
6. **Afterwards:**
   - the DLQ is at 0
   - the business rows have moved on: campaign report, message states
   - the audit log has `platform.dlq.replayed` / `platform.dlq.discarded` with the operator and the count
   - for anything that reached customers twice, check the reconciliation records and open an incident

## Sweepers
Rows stuck in `queued` or `processing` (for example, a crash between DB commit and publish) are re-enqueued by each service's periodic sweeper (ADR-0014). Do not "fix" them by publishing tasks by hand.

## Drill (quarterly, staging)
1. Stop the fake provider. Launch a small campaign, and let `emails.campaign_send` tasks exhaust their retries.
2. Confirm the alert fires and the DLQ view lists the messages.
3. Restart the fake provider, then replay.
4. Confirm each recipient got **one** email (Mailpit) and that the replay is audited.
